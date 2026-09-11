"""Manage non-persistent Provider credentials and runtime settings for the studio."""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ...core.errors import CollageError
from ...providers.birefnet import DEFAULT_MODEL_ID, BiRefNetSettings
from ...providers.yibu.constants import (
    DEFAULT_AUDIT_BASE_URL,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_IMAGE_SIZE,
    DEFAULT_VLM_MAX_TOKENS,
    DEFAULT_VLM_MODEL,
    KIMI_K3_MAX_TOKENS,
    KIMI_K3_REASONING_EFFORT,
)
from ...providers.yibu.settings import YibuSettings
from ...workflows import (
    DEFAULT_CUTOUT_PROVIDER,
    DEFAULT_IMAGE_PROVIDER,
    DEFAULT_VISION_PROVIDER,
)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_BIREFNET_DEPENDENCIES = (
    "torch",
    "torchvision",
    "transformers",
    "timm",
    "kornia",
    "einops",
    "numpy",
    "safetensors",
)
_YIBU_FIELDS = {
    "yibu_api_key",
    "shared_path",
    "audit_base_url",
    "vlm_model",
    "vlm_max_tokens",
    "vlm_reasoning_effort",
    "image_model",
    "image_size",
    "timeout_seconds",
}
_ALLOWED_FIELDS = _YIBU_FIELDS | {
    "birefnet_device",
    "birefnet_model_path",
    "clear_credentials",
}
_ENVIRONMENT_FIELDS = {
    "audit_base_url": "YIBU_AUDIT_BASE_URL",
    "vlm_model": "YIBU_VLM_MODEL",
    "vlm_max_tokens": "YIBU_VLM_MAX_TOKENS",
    "vlm_reasoning_effort": "YIBU_VLM_REASONING_EFFORT",
    "image_model": "YIBU_IMAGE_MODEL",
    "image_size": "YIBU_IMAGE_SIZE",
    "timeout_seconds": "YIBU_TIMEOUT_SECONDS",
    "birefnet_device": "COLLAGE_BIREFNET_DEVICE",
    "birefnet_model_path": "COLLAGE_BIREFNET_MODEL_PATH",
}


def _birefnet_model_cached(settings: BiRefNetSettings) -> bool:
    """Check the configured local or pinned Hugging Face model without loading it."""

    if settings.is_local_model:
        snapshot = Path(settings.model_source)
    else:
        if settings.model_revision is None:
            return False
        if settings.cache_dir is not None:
            cache_root = settings.cache_dir
        elif os.environ.get("HF_HUB_CACHE", "").strip():
            cache_root = Path(os.environ["HF_HUB_CACHE"]).expanduser()
        elif os.environ.get("HF_HOME", "").strip():
            cache_root = Path(os.environ["HF_HOME"]).expanduser() / "hub"
        elif os.environ.get("XDG_CACHE_HOME", "").strip():
            cache_root = (
                Path(os.environ["XDG_CACHE_HOME"]).expanduser() / "huggingface" / "hub"
            )
        else:
            cache_root = Path.home() / ".cache" / "huggingface" / "hub"
        repository = "models--" + settings.model_source.replace("/", "--")
        snapshot = cache_root / repository / "snapshots" / settings.model_revision
    try:
        has_config = (snapshot / "config.json").is_file()
        has_weights = any(
            path.is_file()
            for pattern in (
                "*.safetensors",
                "*.safetensors.index.json",
                "pytorch_model*.bin",
            )
            for path in snapshot.glob(pattern)
        )
    except OSError:
        return False
    return has_config and has_weights


def _bounded_string(
    payload: dict[str, Any],
    name: str,
    *,
    maximum: int,
) -> str | None:
    if name not in payload:
        return None
    value = payload[name]
    if not isinstance(value, str) or len(value) > maximum:
        raise CollageError("INVALID_PROVIDER_SETTINGS", f"字段 {name} 格式不正确")
    return value.strip()


def _audit_health(base_url: str) -> dict[str, Any]:
    """Probe only the required loopback audit endpoint without sending credentials."""

    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
        return {"state": "invalid", "message": "审计地址必须是本机 HTTP 回环地址"}
    request = urllib.request.Request(
        base_url.rstrip("/") + "/_yibu_audit/health",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=0.6) as response:
            raw = response.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("health response too large")
        payload = json.loads(raw)
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ):
        return {"state": "offline", "message": "本机审计代理未启动或无法访问"}
    if (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("upstream") == "https://yibuapi.com"
    ):
        return {"state": "online", "message": "审计代理在线"}
    return {"state": "invalid", "message": "审计代理返回了不受支持的上游"}


class ProviderRuntimeSettings:
    """Apply Provider settings to this process without persisting secrets to disk."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session_key_configured = False

    def status(self, *, probe_audit: bool = False) -> dict[str, Any]:
        """Return public readiness information without returning credentials or paths."""

        with self._lock:
            environment_key = bool(os.environ.get("YIBU_API_KEY", "").strip())
            shared_value = os.environ.get("YIBU_SHARED_PATH", "").strip()
            shared_exists = bool(
                shared_value and Path(shared_value).expanduser().is_file()
            )
            file_value = os.environ.get("YIBU_CREDENTIALS_FILE", "").strip()
            file_exists = bool(file_value and Path(file_value).expanduser().is_file())
            credential_configured = environment_key or (
                file_exists if file_value else shared_exists
            )
            if self._session_key_configured and environment_key:
                credential_source = "当前工作台内存 Key"
            elif environment_key:
                credential_source = "启动环境中的 Key"
            elif file_exists:
                credential_source = "启动环境中的 JSON 凭据文件"
            elif file_value:
                credential_source = "JSON 凭据文件路径无效"
            elif shared_exists:
                credential_source = "shared.py"
            elif shared_value:
                credential_source = "shared.py 路径无效"
            else:
                credential_source = "未配置"

            audit_url = (
                os.environ.get("YIBU_AUDIT_BASE_URL", DEFAULT_AUDIT_BASE_URL).strip()
                or DEFAULT_AUDIT_BASE_URL
            )
            audit = (
                _audit_health(audit_url)
                if probe_audit
                else {"state": "not_checked", "message": "尚未检测"}
            )
            vision_model = (
                os.environ.get("YIBU_VLM_MODEL", DEFAULT_VLM_MODEL).strip()
                or DEFAULT_VLM_MODEL
            )
            default_tokens = (
                KIMI_K3_MAX_TOKENS
                if vision_model.lower() == "kimi-k3"
                else DEFAULT_VLM_MAX_TOKENS
            )
            vision_max_tokens = os.environ.get(
                "YIBU_VLM_MAX_TOKENS", str(default_tokens)
            ).strip() or str(default_tokens)
            reasoning_effort = os.environ.get("YIBU_VLM_REASONING_EFFORT")
            if reasoning_effort is None:
                reasoning_effort = (
                    KIMI_K3_REASONING_EFFORT
                    if vision_model.lower() == "kimi-k3"
                    else ""
                )
            timeout_seconds = os.environ.get(
                "YIBU_TIMEOUT_SECONDS",
                "900" if vision_model.lower() == "kimi-k3" else "600",
            ).strip() or ("900" if vision_model.lower() == "kimi-k3" else "600")
            missing_dependencies = [
                name
                for name in _BIREFNET_DEPENDENCIES
                if importlib.util.find_spec(name) is None
            ]
            try:
                birefnet = BiRefNetSettings.from_env()
                birefnet_error = None
                model_label = birefnet.model_label
                device = birefnet.device
                model_cached = _birefnet_model_cached(birefnet)
            except CollageError as exc:
                birefnet_error = exc.as_dict()
                model_label = DEFAULT_MODEL_ID
                device = os.environ.get("COLLAGE_BIREFNET_DEVICE", "auto")
                model_cached = False

            return {
                "credential": {
                    "configured": credential_configured,
                    "source": credential_source,
                    "shared_path_configured": bool(shared_value),
                },
                "audit": {
                    "url": audit_url,
                    **audit,
                },
                "vision": {
                    "name": "Yibu VLM",
                    "provider": DEFAULT_VISION_PROVIDER,
                    "model": os.environ.get("YIBU_VLM_MODEL", DEFAULT_VLM_MODEL)
                    or DEFAULT_VLM_MODEL,
                    "max_tokens": vision_max_tokens,
                    "reasoning_effort": reasoning_effort,
                    "timeout_seconds": timeout_seconds,
                    "configured": credential_configured,
                },
                "image": {
                    "name": "Yibu 图片编辑",
                    "provider": DEFAULT_IMAGE_PROVIDER,
                    "model": os.environ.get("YIBU_IMAGE_MODEL", DEFAULT_IMAGE_MODEL)
                    or DEFAULT_IMAGE_MODEL,
                    "image_size": os.environ.get("YIBU_IMAGE_SIZE", DEFAULT_IMAGE_SIZE)
                    or DEFAULT_IMAGE_SIZE,
                    "configured": credential_configured,
                },
                "cutout": {
                    "name": "本地 BiRefNet 抠图",
                    "provider": DEFAULT_CUTOUT_PROVIDER,
                    "model": model_label,
                    "device": device,
                    "model_cached": model_cached,
                    "configured": not missing_dependencies and birefnet_error is None,
                    "missing_dependencies": missing_dependencies,
                    "error": birefnet_error,
                    "local_only": True,
                },
                "defaults": {
                    "audit_base_url": DEFAULT_AUDIT_BASE_URL,
                    "vlm_model": DEFAULT_VLM_MODEL,
                    "vlm_max_tokens": default_tokens,
                    "image_model": DEFAULT_IMAGE_MODEL,
                },
                "secret_persistence": "memory_only",
            }

    def configure(self, payload: Any) -> dict[str, Any]:
        """Atomically validate and apply settings, never serializing the API key."""

        if not isinstance(payload, dict):
            raise CollageError(
                "INVALID_PROVIDER_SETTINGS", "Provider 设置必须是 JSON object"
            )
        unknown = set(payload) - _ALLOWED_FIELDS
        if unknown:
            raise CollageError(
                "INVALID_PROVIDER_SETTINGS",
                "Provider 设置包含未知字段",
                details={"fields": sorted(unknown)},
            )
        clear_credentials = payload.get("clear_credentials", False)
        if not isinstance(clear_credentials, bool):
            raise CollageError(
                "INVALID_PROVIDER_SETTINGS", "clear_credentials 必须是 boolean"
            )
        api_key = _bounded_string(payload, "yibu_api_key", maximum=4096)
        shared_path = _bounded_string(payload, "shared_path", maximum=1000)
        if clear_credentials and (api_key or shared_path):
            raise CollageError(
                "INVALID_PROVIDER_SETTINGS",
                "清除凭据不能与新 Key 或 shared.py 同时提交",
            )
        values = {
            name: _bounded_string(payload, name, maximum=500)
            for name in _ENVIRONMENT_FIELDS
        }
        touched_environment_names = {
            "YIBU_API_KEY",
            "YIBU_SHARED_PATH",
            "YIBU_CREDENTIALS_FILE",
            *_ENVIRONMENT_FIELDS.values(),
        }

        with self._lock:
            previous = {
                name: os.environ.get(name) for name in touched_environment_names
            }
            previous_session_flag = self._session_key_configured
            try:
                if clear_credentials:
                    os.environ.pop("YIBU_API_KEY", None)
                    os.environ.pop("YIBU_SHARED_PATH", None)
                    os.environ.pop("YIBU_CREDENTIALS_FILE", None)
                    self._session_key_configured = False
                if api_key:
                    os.environ["YIBU_API_KEY"] = api_key
                    self._session_key_configured = True
                if shared_path:
                    resolved_shared = Path(shared_path).expanduser().resolve()
                    if not resolved_shared.is_file():
                        raise CollageError(
                            "YIBU_SHARED_NOT_FOUND", "指定的 shared.py 文件不存在"
                        )
                    os.environ["YIBU_SHARED_PATH"] = str(resolved_shared)
                if shared_path:
                    os.environ.pop("YIBU_CREDENTIALS_FILE", None)
                if shared_path and not api_key:
                    os.environ.pop("YIBU_API_KEY", None)
                    self._session_key_configured = False
                for field, environment_name in _ENVIRONMENT_FIELDS.items():
                    value = values[field]
                    if value is None:
                        continue
                    if value:
                        os.environ[environment_name] = value
                    else:
                        os.environ.pop(environment_name, None)

                yibu_changed = bool(set(payload) & _YIBU_FIELDS)
                credential_available = bool(
                    os.environ.get("YIBU_API_KEY", "").strip()
                    or os.environ.get("YIBU_SHARED_PATH", "").strip()
                    or os.environ.get("YIBU_CREDENTIALS_FILE", "").strip()
                )
                if yibu_changed and credential_available:
                    # This resolves shared.py and validates all Yibu settings, but the
                    # returned secret-bearing object is deliberately not retained.
                    YibuSettings.from_env()
                elif yibu_changed and not clear_credentials:
                    raise CollageError(
                        "YIBU_CREDENTIAL_MISSING",
                        "请输入 Yibu API Key，或指定已有 shared.py",
                    )
                if set(payload) & {"birefnet_device", "birefnet_model_path"}:
                    BiRefNetSettings.from_env()
            except Exception:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
                self._session_key_configured = previous_session_flag
                raise
        return self.status(probe_audit=True)
