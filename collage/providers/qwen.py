"""Generate background candidates and independent artwork via a resident Qwen service."""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import secrets
import threading
import time
from dataclasses import dataclass

from PIL import Image

from ..core.errors import CollageError
from ..core.io import stable_hash
from ..imaging.geometry import pad_for_model, restore_from_model
from .base import GeneratedImage, ImageCapabilities, ProviderAudit
from .http import ServiceClient, positive_env, service_url

LOGGER = logging.getLogger(__name__)
MODEL = "Qwen-Image-Edit-2511"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class QwenSettings:
    base_url: str
    timeout_seconds: int = 900
    seed: int | None = None

    @classmethod
    def from_env(cls, *, required: bool = True):
        base = os.environ.get("COLLAGE_QWEN_IMAGE_BASE_URL", "").strip()
        if required and not base:
            raise CollageError("PROVIDER_CONFIG_MISSING", "请配置 A100 图片服务地址")
        seed = os.environ.get("COLLAGE_QWEN_IMAGE_SEED", "").strip()
        try:
            parsed_seed = int(seed) if seed else None
            if parsed_seed is not None and not 0 <= parsed_seed < 2**63:
                raise ValueError()
        except ValueError as exc:
            raise CollageError(
                "PROVIDER_CONFIG_INVALID", "图片 seed 必须为 0 到 2^63-1 的整数"
            ) from exc
        return cls(
            service_url(base) if base else "",
            positive_env("COLLAGE_QWEN_IMAGE_TIMEOUT", 900),
            parsed_seed,
        )


def _model_size(size):
    """Keep roughly one megapixel and align latent dimensions, preserving aspect ratio."""
    scale = min(math.sqrt(1024**2 / (size[0] * size[1])), 2048 / max(size))
    return tuple(max(32, round(value * scale / 32) * 32) for value in size)


class QwenImageProvider:
    """Adapt reference editing plus local mask composition to the existing build contract."""

    def __init__(self, settings: QwenSettings | None = None):
        self.settings = settings or QwenSettings.from_env()
        self._client = ServiceClient(
            self.settings.base_url, self.settings.timeout_seconds
        )
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(self._client.base_url, threading.Lock())

    @property
    def capabilities(self):
        return ImageCapabilities(
            name="qwen-http-image",
            requested_model=MODEL,
            supports_reference_image=True,
            # This is the adapter/build capability: local composition protects the mask.
            # /edit itself accepts only a reference image, not a native inpainting mask.
            supports_mask_edit=True,
            supports_transparency=False,
            mask_polarity="white_edit",
        )

    @property
    def cache_identity(self):
        return stable_hash(
            {
                "endpoint": self.settings.base_url,
                "seed": self.settings.seed,
                "adapter": 2,
                "pixels": 1024**2,
                "steps": 40,
                "cfg": 4.0,
            }
        )

    def _generate(self, reference, prompt, *, operation, fill=(255, 255, 255, 255)):
        started = time.monotonic()
        deadline = started + self.settings.timeout_seconds
        target_size = _model_size(reference.size)
        prepared, input_transform = pad_for_model(reference, target_size, fill=fill)
        encoded = io.BytesIO()
        prepared.convert("RGB").save(encoded, format="PNG")
        # A new explicit generation gets a new seed; cached outputs remain reusable.
        seed = (
            self.settings.seed
            if self.settings.seed is not None
            else secrets.randbits(32)
        )
        payload = {
            "image_base64": base64.b64encode(encoded.getvalue()).decode("ascii"),
            "prompt": prompt,
            "seed": seed,
            # /edit accepts three fields. Prepared pixels carry the input size;
            # the service determines the output dimensions.
        }
        LOGGER.info("等待图片服务 | operation=%s model=%s", operation, MODEL)
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise CollageError(
                "PROVIDER_BUSY",
                "等待图片服务超时",
                details={"request_state": "not_started"},
            )
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CollageError(
                        "PROVIDER_BUSY",
                        "等待图片服务超时",
                        details={"request_state": "not_started"},
                    )
                try:
                    raw, _headers = self._client.request(
                        "/edit", payload, timeout=remaining, operation=operation
                    )
                    break
                except CollageError as exc:
                    if exc.code != "PROVIDER_BUSY":
                        raise
                    LOGGER.info("图片服务繁忙，等待空闲 | operation=%s", operation)
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
        finally:
            self._lock.release()
        try:
            with Image.open(io.BytesIO(raw)) as source:
                if source.format != "PNG":
                    raise ValueError("not PNG")
                source.load()
                generated = source.copy()
        except (ValueError, OSError) as exc:
            raise CollageError(
                "PROVIDER_INVALID_RESPONSE",
                "图片服务未返回可解码的 PNG",
                details={"request_state": "failed"},
            ) from exc
        transform = {
            "seed": seed,
            "input": input_transform.as_dict(),
            "raw_size": list(generated.size),
        }
        image = generated
        if operation == "edit-background":
            # Undo input padding at the original canvas size; never independently stretch axes.
            fitted, output_transform = pad_for_model(generated, target_size, fill=fill)
            image = restore_from_model(fitted, input_transform)
            transform["output"] = output_transform.as_dict()
        LOGGER.info(
            "图片生成完成 | operation=%s elapsed=%.1fs",
            operation,
            time.monotonic() - started,
        )
        audit = ProviderAudit(
            self.capabilities.name,
            MODEL,
            None,
            None,
            False,
            round((time.monotonic() - started) * 1000),
        )
        return GeneratedImage(image, audit, transform, raw_image=generated)

    def edit_background(self, reference, provider_mask, *, brief):
        if provider_mask.size != reference.size:
            raise CollageError("MASK_SIZE_MISMATCH", "背景蒙版与参考图尺寸不一致")
        prompt = (
            "清理这张拼贴图的背景。移除旧照片、旧文字和独立装饰，"
            "依据周围纸张、纹理和场景补全背景。"
            f"具体要求：{brief}。保持原有方向和背景构图，"
            "不添加人物、文字、水印或黑白蒙版，只输出完整的清版背景。"
        )
        return self._generate(reference, prompt, operation="edit-background")

    def make_overlay(self, reference_crop, *, brief, background_mode, chroma_key):
        key = chroma_key or (0, 255, 0)
        prompt = (
            "将参考图中的目标制作成一件完整独立装饰，保留其轮廓、笔画、颜色和风格。"
            f"目标要求：{brief}。补全被遮挡或截断的部分，移除照片、相邻元素和原背景。"
            f"背景改为完全均匀的纯色 RGB{key}，无阴影、渐变和纹理。"
            "所有细线完整，四周至少留 8% 空白，不加水印。"
        )
        return self._generate(
            reference_crop, prompt, operation="make-overlay", fill=(*key, 255)
        )

    def health(self):
        raw, _ = self._client.request(
            "/health", timeout=min(2, self.settings.timeout_seconds)
        )
        try:
            value = json.loads(raw)
            if isinstance(value, dict) and value.get("status") == "ok":
                return {
                    "state": "online",
                    "message": "服务繁忙" if value.get("busy") else "服务在线",
                }
        except ValueError:
            pass
        raise CollageError("PROVIDER_INVALID_RESPONSE", "图片服务健康检查响应无效")
