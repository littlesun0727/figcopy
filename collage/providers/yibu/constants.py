"""yibu Provider 的固定协议值与响应匹配规则。"""

import re

DEFAULT_AUDIT_BASE_URL = "http://127.0.0.1:17860"
DEFAULT_VLM_MODEL = "kimi-k3"
DEFAULT_IMAGE_MODEL = "doubao-seedream-5-0-260128"
DEFAULT_IMAGE_SIZE = "2K"
DEFAULT_VLM_MAX_TOKENS = 8192
KIMI_K3_MAX_TOKENS = 16384
KIMI_K3_REASONING_EFFORT = "high"
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
