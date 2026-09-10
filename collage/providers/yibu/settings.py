"""读取并校验 yibu Provider 配置与凭据。"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

from ...core.errors import CollageError
from .constants import (
    _DATA_URL_RE,
    _LOOPBACK_HOSTS,
    _REASONING_EFFORTS,
    _SECRET_RE,
    _VLM_MODEL_ALIASES,
    DEFAULT_AUDIT_BASE_URL,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_VLM_MAX_TOKENS,
    DEFAULT_VLM_MODEL,
    KIMI_K3_MAX_TOKENS,
)


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
