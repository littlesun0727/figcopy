"""Analyze reference layouts through the company's Chat Completions VLM service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..core.errors import CollageError
from ..core.io import stable_hash
from .chat import ChatVisionProvider
from .http import ServiceClient, positive_env, service_url

DEFAULT_MODEL = "Qwen/Qwen3.8-Flash-Next"


@dataclass(frozen=True)
class IntranetSettings:
    base_url: str
    api_key: str = field(repr=False)
    vlm_model: str = DEFAULT_MODEL
    vlm_max_tokens: int = 16384
    timeout_seconds: int = 900
    vlm_reasoning_effort: None = None

    @classmethod
    def from_env(cls, *, required: bool = True):
        base = os.environ.get("COLLAGE_INTRANET_VLM_BASE_URL", "").strip()
        key = os.environ.get("COLLAGE_INTRANET_VLM_API_KEY", "").strip()
        if required and not (base and key):
            raise CollageError(
                "PROVIDER_CONFIG_MISSING", "请配置内网 VLM 地址和 API Key"
            )
        # Accept a server origin or the conventional /v1 base, without duplicating /v1.
        base = service_url(base).removesuffix("/v1") if base else ""
        return cls(
            base,
            key,
            os.environ.get("COLLAGE_INTRANET_VLM_MODEL", "").strip() or DEFAULT_MODEL,
            positive_env("COLLAGE_INTRANET_VLM_MAX_TOKENS", 16384),
            positive_env("COLLAGE_INTRANET_VLM_TIMEOUT", 900),
        )


class IntranetVisionProvider(ChatVisionProvider):
    """Use the same semantic Draft prompt with an independent direct transport."""

    name = "intranet-chat-vlm"

    def __init__(self, settings: IntranetSettings | None = None):
        self.settings = settings or IntranetSettings.from_env()
        self.requested_model = self.settings.vlm_model
        self._client = ServiceClient(
            self.settings.base_url, self.settings.timeout_seconds, self.settings.api_key
        )

    @property
    def cache_identity(self):
        # Store only a digest of configuration; endpoints and secrets stay out of artifacts.
        return stable_hash(
            {
                "endpoint": self.settings.base_url,
                "max_tokens": self.settings.vlm_max_tokens,
                "adapter": 1,
            }
        )
