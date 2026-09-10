"""通过 yibu Gemini 图片模型制作背景和装饰素材。"""

from __future__ import annotations

import base64
import binascii
import io
import ipaddress
import logging
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlsplit

from PIL import Image, UnidentifiedImageError

from ...core.errors import CollageError
from ..base import GeneratedImage, ImageCapabilities, ProviderAudit
from .client import _request_id, _YibuAuditClient
from .constants import _DATA_URL_RE, MAX_RESPONSE_BYTES
from .settings import YibuSettings, _safe_error_text

LOGGER = logging.getLogger(__name__)


_ASPECT_RATIOS: tuple[tuple[str, float], ...] = (
    ("1:1", 1.0),
    ("2:3", 2 / 3),
    ("3:2", 3 / 2),
    ("3:4", 3 / 4),
    ("4:3", 4 / 3),
    ("4:5", 4 / 5),
    ("5:4", 5 / 4),
    ("9:16", 9 / 16),
    ("16:9", 16 / 9),
    ("21:9", 21 / 9),
)


def _nearest_aspect_ratio(size: tuple[int, int]) -> str:
    ratio = size[0] / size[1]
    return min(_ASPECT_RATIOS, key=lambda item: abs(item[1] - ratio))[0]


def _image_part(image: Image.Image) -> dict[str, Any]:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return {
        "inline_data": {
            "mime_type": "image/png",
            "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
        }
    }


def _decode_image_bytes(data: bytes) -> Image.Image | None:
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            return source.copy()
    except (UnidentifiedImageError, OSError):
        return None


def _decode_base64_image(value: str) -> Image.Image | None:
    try:
        decoded = base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError):
        return None
    return _decode_image_bytes(decoded)


def _safe_remote_image(url: str, timeout: int) -> Image.Image | None:
    """只下载模型响应中的公网 HTTPS 图片，拒绝回环和私网地址。"""

    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local
    ):
        return None
    LOGGER.info("下载 yibu 返回的图片文件 | host=%s", parsed.hostname)
    try:
        request = urllib.request.Request(url, headers={"Accept": "image/*"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if len(body) > MAX_RESPONSE_BYTES:
        return None
    return _decode_image_bytes(body)


def _extract_generated_image(response: dict[str, Any], timeout: int) -> Image.Image:
    """兼容 Gemini inlineData、OpenAI b64_json 和常见 image_url 响应。"""

    candidates: list[tuple[str, str]] = []

    def visit(value: Any, parent_key: str = "") -> None:
        if isinstance(value, dict):
            inline = value.get("inlineData") or value.get("inline_data")
            if isinstance(inline, dict):
                mime = inline.get("mimeType") or inline.get("mime_type")
                data = inline.get("data")
                if (
                    isinstance(mime, str)
                    and mime.startswith("image/")
                    and isinstance(data, str)
                ):
                    candidates.append(("base64", data))
            encoded = value.get("b64_json")
            if isinstance(encoded, str):
                candidates.append(("base64", encoded))
            image_url = value.get("image_url")
            if isinstance(image_url, str):
                candidates.append(("url", image_url))
            elif isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                candidates.append(("url", image_url["url"]))
            for key, child in value.items():
                if key not in {"inlineData", "inline_data", "b64_json", "image_url"}:
                    visit(child, key)
        elif isinstance(value, list):
            for child in value:
                visit(child, parent_key)
        elif isinstance(value, str):
            for match in _DATA_URL_RE.finditer(value):
                candidates.append(("base64", match.group(2)))
            if parent_key.lower() in {"url", "uri"} and value.startswith("https://"):
                candidates.append(("url", value))

    visit(response)
    for kind, value in candidates:
        if kind == "base64":
            image = _decode_base64_image(value)
        else:
            data_match = _DATA_URL_RE.fullmatch(value.strip())
            image = (
                _decode_base64_image(data_match.group(2))
                if data_match
                else _safe_remote_image(value, timeout)
            )
        if image is not None:
            return image
    diagnostic = _response_diagnostic(response)
    finish_reason = _first_finish_reason(response)
    LOGGER.warning("yibu 图片响应未包含可解码图片 | response=%s", diagnostic)
    if finish_reason == "IMAGE_RECITATION":
        raise CollageError(
            "PROVIDER_IMAGE_RECITATION",
            "Gemini 因图片复现/版权相似性限制拒绝生成；请把要求改得更原创后重试",
            details={"finish_reason": finish_reason, "response_shape": diagnostic},
        )
    if finish_reason in {
        "SAFETY",
        "PROHIBITED_CONTENT",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "BLOCKLIST",
        "SPII",
    }:
        raise CollageError(
            "PROVIDER_CONTENT_BLOCKED",
            f"Gemini 内容策略阻止了图片生成：{finish_reason}",
            details={"finish_reason": finish_reason, "response_shape": diagnostic},
        )
    if finish_reason in {"NO_IMAGE", "IMAGE_OTHER"}:
        raise CollageError(
            "PROVIDER_NO_IMAGE",
            f"Gemini 未能生成图片：{finish_reason}",
            details={"finish_reason": finish_reason, "response_shape": diagnostic},
        )
    raise CollageError(
        "PROVIDER_INVALID_RESPONSE",
        "图片模型响应中没有可解码的图片",
        details={"response_shape": diagnostic},
    )


def _response_diagnostic(value: Any, depth: int = 0) -> Any:
    """保留响应结构和短文本，同时隐藏大块二进制、密钥与 data URL。"""

    if depth >= 5:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        summary: dict[str, Any] = {}
        for key, child in list(value.items())[:20]:
            lowered = key.lower()
            if lowered in {"data", "b64_json"} and isinstance(child, str):
                summary[key] = f"<encoded-data length={len(child)}>"
            else:
                summary[key] = _response_diagnostic(child, depth + 1)
        return summary
    if isinstance(value, list):
        return [_response_diagnostic(child, depth + 1) for child in value[:5]]
    if isinstance(value, str):
        return _safe_error_text(value, 300)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


def _first_finish_reason(response: dict[str, Any]) -> str | None:
    """提取 Gemini 首个候选的终止原因，供业务错误分类。"""

    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    if not isinstance(first, dict):
        return None
    reason = first.get("finishReason") or first.get("finish_reason")
    return reason.upper() if isinstance(reason, str) else None


def _fit_generated_image(
    image: Image.Image, target_size: tuple[int, int]
) -> tuple[Image.Image, dict[str, Any]]:
    """用中心裁切和统一缩放对齐模型输出；禁止非等比直接拉伸。"""

    if image.size == target_size:
        return image, {
            "kind": "identity",
            "source_size": list(image.size),
            "target_size": list(target_size),
        }
    source_width, source_height = image.size
    target_ratio = target_size[0] / target_size[1]
    source_ratio = source_width / source_height
    if source_ratio > target_ratio:
        crop_width = max(1, round(source_height * target_ratio))
        left = (source_width - crop_width) // 2
        box = (left, 0, left + crop_width, source_height)
    else:
        crop_height = max(1, round(source_width / target_ratio))
        top = (source_height - crop_height) // 2
        box = (0, top, source_width, top + crop_height)
    fitted = image.crop(box).resize(target_size, Image.Resampling.LANCZOS)
    return fitted, {
        "kind": "center_crop_uniform_resize",
        "source_size": [source_width, source_height],
        "crop_box": list(box),
        "target_size": list(target_size),
    }


class YibuImageProvider:
    """使用 gemini-3-pro-image-preview 制作清版背景和近似装饰。"""

    def __init__(self, settings: YibuSettings | None = None) -> None:
        self.settings = settings or YibuSettings.from_env()
        self._client = _YibuAuditClient(self.settings)

    @property
    def capabilities(self) -> ImageCapabilities:
        return ImageCapabilities(
            name="yibu-audit-gemini-image",
            requested_model=self.settings.image_model,
            supports_reference_image=True,
            supports_mask_edit=True,
            # Gemini 图片输出不保证 alpha；构建器会自动改用纯色键背景。
            supports_transparency=False,
            mask_polarity="white_edit",
            output_sizes=(),
            supports_request_status=False,
            fixture=False,
        )

    def _generate(
        self,
        parts: list[dict[str, Any]],
        target_size: tuple[int, int],
        *,
        operation: str,
    ) -> GeneratedImage:
        started = time.monotonic()
        model = self.settings.image_model
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                # 当前 yibu Gemini 渠道要求同时声明文本和图片；只声明 IMAGE
                # 可能正常计费却只返回空候选或说明文本。
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {
                    "aspectRatio": _nearest_aspect_ratio(target_size),
                    "imageSize": self.settings.image_size,
                },
            },
        }
        response, headers = self._client.post_json(
            f"/v1beta/models/{quote(model, safe='-._')}:generateContent",
            payload,
            model=model,
            operation=operation,
            auth_style="google",
        )
        generated = _extract_generated_image(response, self.settings.timeout_seconds)
        fitted, transform = _fit_generated_image(generated, target_size)
        actual_model = response.get("modelVersion") or response.get("model")
        audit = ProviderAudit(
            self.capabilities.name,
            model,
            actual_model if isinstance(actual_model, str) else None,
            _request_id(response, headers),
            False,
            round((time.monotonic() - started) * 1000),
        )
        return GeneratedImage(fitted, audit, transform)

    def edit_background(
        self,
        reference: Image.Image,
        provider_mask: Image.Image,
        *,
        brief: str,
    ) -> GeneratedImage:
        if provider_mask.size != reference.size:
            raise CollageError(
                "MASK_SIZE_MISMATCH", "传给 yibu 的背景 mask 与参考图尺寸不一致"
            )
        instruction = (
            "执行拼贴背景清版。第一张图只提供空间与边界上下文，第二张图是同尺寸二值蒙版。"
            "蒙版白色区域允许编辑，黑色区域必须保持；最终程序还会强制保护黑色区域。"
            f"编辑要求：{brief}。请原创地合成缺失内容，不要复刻任何已知作品或训练素材。"
            "只返回一张完成后的图片，不要解释，不要添加新人物、新文字、Logo、水印或"
            "无关装饰，并保持画布方向。"
        )
        parts = [
            {"text": instruction},
            {"text": "参考图："},
            _image_part(reference.convert("RGBA")),
            {"text": "白色为编辑区、黑色为保护区的蒙版："},
            _image_part(provider_mask.convert("L")),
        ]
        return self._generate(parts, reference.size, operation="edit-background")

    def make_overlay(
        self,
        reference_crop: Image.Image,
        *,
        brief: str,
        background_mode: str,
        chroma_key: tuple[int, int, int] | None,
    ) -> GeneratedImage:
        if background_mode == "chroma_key":
            key = chroma_key or (255, 0, 255)
            key_hex = "#" + "".join(f"{channel:02X}" for channel in key)
            background_instruction = (
                f"背景必须是完全均匀的纯色 RGB{key}（{key_hex}），主体外不得有阴影、"
                "渐变、纹理或其他颜色；后续程序会按此颜色生成透明度。"
            )
        else:
            background_instruction = "背景必须透明，输出带真实 alpha 的 PNG。"
        instruction = (
            "根据参考裁图制作一个独立拼贴装饰，保留主要轮廓、颜色和视觉风格，"
            f"但不要复制参考背景。制作要求：{brief}。{background_instruction}"
            "主体完整居中且不贴边。只返回一张图片，不要文字解释、Logo 或水印。"
        )
        return self._generate(
            [{"text": instruction}, _image_part(reference_crop.convert("RGBA"))],
            reference_crop.size,
            operation="make-overlay",
        )
