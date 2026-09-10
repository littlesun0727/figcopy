"""yibu Provider 的固定协议值与响应匹配规则。"""

import re

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
