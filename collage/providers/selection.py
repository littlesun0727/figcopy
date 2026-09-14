"""Resolve independent service choices while preserving Yibu defaults and plugins."""

import os

from ..core.errors import CollageError

YIBU_VISION = "collage.providers.yibu:YibuVisionProvider"
YIBU_IMAGE = "collage.providers.yibu:YibuImageProvider"
INTRANET_VISION = "collage.providers.intranet:IntranetVisionProvider"
QWEN_IMAGE = "collage.providers.qwen:QwenImageProvider"

CHOICES = {
    "vision": {YIBU_VISION: "Yibu VLM", INTRANET_VISION: "内网 Flash-Next"},
    "image": {YIBU_IMAGE: "Yibu 图片", QWEN_IMAGE: "A100 Qwen-Image-Edit"},
}


def default_provider(kind: str) -> str:
    return os.environ.get(f"COLLAGE_{kind.upper()}_PROVIDER", "").strip() or next(
        iter(CHOICES[kind])
    )


def cache_configuration(provider) -> dict:
    """Add new adapter configuration to cache keys without changing any Yibu key."""
    identity = getattr(provider, "cache_identity", None)
    return {"provider_configuration": identity} if identity is not None else {}


def provider_overrides(payload: dict) -> dict[str, str]:
    """Accept existing module:object choices, including explicitly configured plugins."""
    result = {}
    for kind in CHOICES:
        name = kind + "_provider"
        value = payload.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 300:
            raise CollageError("INVALID_REQUEST", f"{name} 格式不正确")
        if value.strip():
            result[name] = value.strip()
    return result
