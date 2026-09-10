"""通过本机 yibu 审计代理调用 VLM 与 Gemini 图片编辑模型。"""

from __future__ import annotations

import base64
import binascii
import importlib.util
import io
import ipaddress
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote, urlsplit

from PIL import Image, UnidentifiedImageError

from ..errors import CollageError
from .base import GeneratedImage, ImageCapabilities, ProviderAudit

LOGGER = logging.getLogger(__name__)

DEFAULT_AUDIT_BASE_URL = "http://127.0.0.1:17860"
DEFAULT_VLM_MODEL = "claude-opus-4-8"
DEFAULT_IMAGE_MODEL = "gemini-3-pro-image-preview"
DEFAULT_VLM_MAX_TOKENS = 8192
KIMI_K3_MAX_TOKENS = 16384
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_DATA_URL_RE = re.compile(
    r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_-]+", re.IGNORECASE)
_VLM_MODEL_ALIASES = {"opus-4.8": "claude-opus-4-8"}
_REASONING_EFFORTS = {"low", "high", "max"}
_DRAFT_REQUIRED_FIELDS = frozenset(
    {"slots", "overlays", "background", "layer_order", "questions"}
)

_DRAFT_CONTRACT = """只输出以下结构的 JSON，不要 Markdown：
{
  "slots": [{
    "id": "slot_id", "label": "槽位名称", "type": "image或text",
    "mode": "photo、photo_feather、cutout、unknown或null",
    "source_rect": [x,y,width,height], "target_rect": [x,y,width,height],
    "upload_hint": "上传提示", "review_notes": "复核说明"
  }],
  "overlays": [{
    "id": "overlay_id", "label": "装饰名称",
    "source_rect": [x,y,width,height], "target_rect": [x,y,width,height],
    "action": "reference_generate或basic_shape",
    "generation_brief": "制作说明", "requires_exact_content": false,
    "review_notes": "复核说明"
  }],
  "background": {"background_brief": "清版说明", "review_notes": "复核说明"},
  "layer_order": [{"type": "background"}, {"type": "slot", "id": "slot_id"}],
  "questions": []
}
不要输出 version、status、source、canvas、provider、created_at 或 prompt_version，应用程序会补齐。"""


def _safe_error_text(value: object, limit: int = 400) -> str:
    """清除错误中的密钥和 data URL，避免日志泄露输入。"""

    text = str(value or "provider request failed")
    text = _SECRET_RE.sub("<api-key>", text)
    text = _DATA_URL_RE.sub("<image-data>", text)
    return " ".join(text.split())[:limit]


def _load_shared_module(path: Path) -> ModuleType:
    """按明确文件路径加载旧项目 shared.py，不修改 sys.path。"""

    if not path.is_file():
        raise CollageError("YIBU_SHARED_NOT_FOUND", f"找不到 YIBU_SHARED_PATH：{path}")
    module_spec = importlib.util.spec_from_file_location("_figcopy_yibu_shared", path)
    if module_spec is None or module_spec.loader is None:
        raise CollageError("YIBU_SHARED_LOAD_FAILED", "无法创建 shared.py 加载器")
    module = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:
        raise CollageError(
            "YIBU_SHARED_LOAD_FAILED",
            f"加载 shared.py 失败：{type(exc).__name__}",
        ) from exc
    return module


def _resolve_api_key() -> str:
    """优先读取环境变量，否则从用户明确指定的 shared.py 取 Key。"""

    environment_key = os.environ.get("YIBU_API_KEY", "").strip()
    if environment_key:
        return environment_key
    shared_value = os.environ.get("YIBU_SHARED_PATH", "").strip()
    if not shared_value:
        raise CollageError(
            "YIBU_CREDENTIAL_MISSING",
            "请设置 YIBU_API_KEY，或设置 YIBU_SHARED_PATH 指向已有 shared.py",
        )
    module = _load_shared_module(Path(shared_value).expanduser().resolve())
    getter = getattr(module, "get_api_key", None)
    if not callable(getter):
        raise CollageError(
            "YIBU_SHARED_LOAD_FAILED", "shared.py 没有可调用的 get_api_key()"
        )
    key = getter()
    if not isinstance(key, str) or not key.strip():
        raise CollageError("YIBU_CREDENTIAL_MISSING", "shared.py 返回了空 API Key")
    return key.strip()


def _validated_audit_base_url(value: str) -> str:
    """只接受回环地址，防止 Provider 绕过本机审计代理。"""

    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise CollageError(
            "YIBU_AUDIT_PROXY_REQUIRED",
            "YIBU_AUDIT_BASE_URL 必须是本机 HTTP 回环地址，例如 http://127.0.0.1:17860",
        )
    return normalized


def _positive_int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise CollageError("YIBU_CONFIG_INVALID", f"{name} 必须是整数") from exc
    if parsed <= 0:
        raise CollageError("YIBU_CONFIG_INVALID", f"{name} 必须大于 0")
    return parsed


def _vlm_model_from_env() -> str:
    """把用户常用名称映射成 yibu `/v1/models` 返回的实际模型 ID。"""

    configured = os.environ.get("YIBU_VLM_MODEL", DEFAULT_VLM_MODEL).strip()
    selected = configured or DEFAULT_VLM_MODEL
    return _VLM_MODEL_ALIASES.get(selected.lower(), selected)


def _vlm_max_tokens_from_env(model: str) -> int:
    """为 Kimi K3 留出思考和完整 Draft 共用的充足输出预算。"""

    default = (
        KIMI_K3_MAX_TOKENS if model.lower() == "kimi-k3" else DEFAULT_VLM_MAX_TOKENS
    )
    return _positive_int_env("YIBU_VLM_MAX_TOKENS", default)


def _vlm_reasoning_effort_from_env(model: str) -> str | None:
    """读取推理强度；Kimi K3 默认使用用户要求的 max 档位。"""

    configured = os.environ.get("YIBU_VLM_REASONING_EFFORT")
    if configured is None:
        return "max" if model.lower() == "kimi-k3" else None
    selected = configured.strip().lower()
    if not selected:
        return None
    if selected not in _REASONING_EFFORTS:
        allowed = "、".join(sorted(_REASONING_EFFORTS))
        raise CollageError(
            "YIBU_CONFIG_INVALID",
            f"YIBU_VLM_REASONING_EFFORT 必须是 {allowed}，或留空",
        )
    return selected


def _timeout_seconds_from_env(model: str) -> int:
    """Kimi K3 max 推理可能较慢，为其提供更长的默认请求时间。"""

    default = 900 if model.lower() == "kimi-k3" else 600
    return _positive_int_env("YIBU_TIMEOUT_SECONDS", default)


@dataclass(frozen=True, slots=True)
class YibuSettings:
    """yibu Provider 配置；API Key 不出现在 repr 和日志中。"""

    api_key: str = field(repr=False)
    audit_base_url: str = DEFAULT_AUDIT_BASE_URL
    vlm_model: str = DEFAULT_VLM_MODEL
    image_model: str = DEFAULT_IMAGE_MODEL
    vlm_max_tokens: int = DEFAULT_VLM_MAX_TOKENS
    vlm_reasoning_effort: str | None = None
    timeout_seconds: int = 600
    image_size: str = "1K"

    @classmethod
    def from_env(cls) -> YibuSettings:
        vlm_model = _vlm_model_from_env()
        image_size = os.environ.get("YIBU_IMAGE_SIZE", "1K").strip().upper()
        if image_size not in {"1K", "2K", "4K"}:
            raise CollageError(
                "YIBU_CONFIG_INVALID", "YIBU_IMAGE_SIZE 必须是 1K、2K 或 4K"
            )
        return cls(
            api_key=_resolve_api_key(),
            audit_base_url=_validated_audit_base_url(
                os.environ.get("YIBU_AUDIT_BASE_URL", DEFAULT_AUDIT_BASE_URL)
            ),
            vlm_model=vlm_model,
            image_model=os.environ.get("YIBU_IMAGE_MODEL", DEFAULT_IMAGE_MODEL).strip()
            or DEFAULT_IMAGE_MODEL,
            vlm_max_tokens=_vlm_max_tokens_from_env(vlm_model),
            vlm_reasoning_effort=_vlm_reasoning_effort_from_env(vlm_model),
            timeout_seconds=_timeout_seconds_from_env(vlm_model),
            image_size=image_size,
        )


class _YibuAuditClient:
    """仅通过审计代理发送 JSON 请求，并限制响应体大小。"""

    def __init__(self, settings: YibuSettings) -> None:
        self.settings = settings
        self.base_url = _validated_audit_base_url(settings.audit_base_url)
        self._health_checked = False

    def _ensure_audit_proxy(self) -> None:
        if self._health_checked:
            return
        request = urllib.request.Request(
            f"{self.base_url}/_yibu_audit/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=min(10, self.settings.timeout_seconds)
            ) as response:
                payload = json.loads(response.read(1024 * 1024))
        except Exception as exc:
            raise CollageError(
                "YIBU_AUDIT_PROXY_UNAVAILABLE",
                "yibu 审计代理不可用；请先启动 D:\\codes\\yibu-audit-proxy",
            ) from exc
        upstream_host = urlsplit(str(payload.get("upstream", ""))).hostname
        if payload.get("status") != "ok" or not (
            upstream_host == "yibuapi.com"
            or (upstream_host and upstream_host.endswith(".yibuapi.com"))
        ):
            raise CollageError(
                "YIBU_AUDIT_PROXY_INVALID",
                "审计健康检查未指向 yibuapi.com，已拒绝模型调用",
            )
        self._health_checked = True
        LOGGER.info("yibu 审计代理健康检查通过 | endpoint=%s", self.base_url)

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        model: str,
        operation: str,
        auth_style: str,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        self._ensure_audit_proxy()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth_style == "google":
            headers["x-goog-api-key"] = self.settings.api_key
        else:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        LOGGER.info(
            "通过审计代理调用 yibu | operation=%s model=%s path=%s",
            operation,
            model,
            path,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise CollageError(
                        "PROVIDER_RESPONSE_TOO_LARGE", "yibu 响应超过 64 MiB 限制"
                    )
                response_headers = {
                    name.lower(): value for name, value in response.headers.items()
                }
        except urllib.error.HTTPError as exc:
            try:
                detail = _safe_error_text(exc.read(4096).decode("utf-8", "replace"))
            except OSError:
                detail = _safe_error_text(exc.reason)
            if exc.code == 429:
                code = "RATE_LIMITED"
            elif exc.code in {500, 502, 503, 504}:
                code = "TEMPORARY_NETWORK_ERROR"
            elif exc.code in {401, 403}:
                code = "PROVIDER_AUTH_FAILED"
            else:
                code = "PROVIDER_REQUEST_FAILED"
            raise CollageError(
                code,
                f"yibu 请求失败：HTTP {exc.code}",
                details={"operation": operation, "response": detail},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CollageError(
                "TEMPORARY_NETWORK_ERROR",
                f"yibu 审计请求失败：{type(exc).__name__}",
                details={"operation": operation},
            ) from exc
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise CollageError(
                "PROVIDER_INVALID_RESPONSE", "yibu 返回的不是有效 JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise CollageError(
                "PROVIDER_INVALID_RESPONSE", "yibu 顶层响应必须是 JSON object"
            )
        return decoded, response_headers


def _assistant_text(response: dict[str, Any]) -> str:
    """兼容 OpenAI chat 和少量代理变体，提取助手文本。"""

    content: Any = None
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content")
    if content is None:
        content = response.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                pieces.append(item["text"])
        if pieces:
            return "\n".join(pieces)
    raise CollageError("PROVIDER_INVALID_RESPONSE", "VLM 响应中没有助手文本")


def _chat_finish_reason(response: dict[str, Any]) -> str | None:
    """读取 OpenAI 或常见代理命名风格的结束原因。"""

    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    value = choices[0].get("finish_reason") or choices[0].get("finishReason")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_json_object(text: str) -> dict[str, Any]:
    """从纯 JSON 或 Markdown 围栏中提取完整 Draft object。"""

    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3].rstrip()
    try:
        value = json.loads(stripped)
        if isinstance(value, dict) and _DRAFT_REQUIRED_FIELDS.issubset(value):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(stripped):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and _DRAFT_REQUIRED_FIELDS.issubset(value):
            return value
    raise CollageError(
        "PROVIDER_DRAFT_INCOMPLETE",
        "VLM 没有返回包含全部顶层字段的完整 Draft JSON",
        details={"required_fields": sorted(_DRAFT_REQUIRED_FIELDS)},
    )


def _request_id(response: dict[str, Any], headers: dict[str, str]) -> str | None:
    for value in (
        response.get("responseId"),
        response.get("response_id"),
        response.get("id"),
        headers.get("x-request-id"),
        headers.get("request-id"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class YibuVisionProvider:
    """通过 yibu 的 OpenAI 兼容接口分析参考拼贴图并返回候选 Draft。"""

    name = "yibu-audit-vlm"
    fixture = False

    def __init__(self, settings: YibuSettings | None = None) -> None:
        self.settings = settings or YibuSettings.from_env()
        self.requested_model = self.settings.vlm_model
        self._client = _YibuAuditClient(self.settings)

    def analyze(
        self,
        reference_bytes: bytes,
        *,
        media_type: str,
        canvas: dict[str, Any],
        product_policy: dict[str, Any],
        prompt: str,
    ) -> tuple[dict[str, Any], ProviderAudit]:
        started = time.monotonic()
        encoded = base64.b64encode(reference_bytes).decode("ascii")
        instruction = (
            f"{prompt}\n\n画布：{json.dumps(canvas, ensure_ascii=False)}\n"
            f"产品策略：{json.dumps(product_policy, ensure_ascii=False)}\n\n"
            f"{_DRAFT_CONTRACT}"
        )
        payload = {
            "model": self.requested_model,
            "max_tokens": self.settings.vlm_max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                        },
                    ],
                }
            ],
        }
        if self.settings.vlm_reasoning_effort is not None:
            payload["reasoning_effort"] = self.settings.vlm_reasoning_effort
        LOGGER.info(
            "准备 VLM 请求 | model=%s max_tokens=%d reasoning_effort=%s",
            self.requested_model,
            self.settings.vlm_max_tokens,
            self.settings.vlm_reasoning_effort or "provider-default",
        )
        response, headers = self._client.post_json(
            "/v1/chat/completions",
            payload,
            model=self.requested_model,
            operation="analyze-reference",
            auth_style="bearer",
        )
        finish_reason = _chat_finish_reason(response)
        if finish_reason and finish_reason.lower() in {
            "length",
            "max_tokens",
            "max_output_tokens",
        }:
            raise CollageError(
                "PROVIDER_OUTPUT_TRUNCATED",
                "VLM 输出达到上限，完整 Draft 被截断；请调高输出预算或调低推理强度",
                details={
                    "model": self.requested_model,
                    "finish_reason": finish_reason,
                    "max_tokens": self.settings.vlm_max_tokens,
                },
            )
        draft = _parse_json_object(_assistant_text(response))
        actual_model = response.get("model")
        audit = ProviderAudit(
            self.name,
            self.requested_model,
            actual_model if isinstance(actual_model, str) else None,
            _request_id(response, headers),
            False,
            round((time.monotonic() - started) * 1000),
        )
        return draft, audit


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
