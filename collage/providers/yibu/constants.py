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
    "action": "从 product_policy.overlay_actions 中选择",
    "generation_brief": "制作说明", "requires_exact_content": false,
    "review_notes": "复核说明"
  }],
  "background": {"background_brief": "清版说明", "review_notes": "复核说明"},
  "layer_order": [{"type": "background"}, {"type": "slot", "id": "slot_id"}],
  "questions": []
}
背景有两种来源，必须按产品需求选择：
1. 固定背景沿用上面的 background 对象，layer_order 第一项为 {"type":"background"}，且只能出现一次。
2. 如果客户上传的一张普通照片铺满整个画布作为背景，将 background 改为
   {"mode":"slot","slot_id":"实际的全屏图片槽ID","review_notes":"背景由客户照片提供，无需制作固定背景"}。
   对应槽位必须为 image/photo，target_rect 为 [0,0,画布宽,画布高]；
   layer_order 第一项直接引用这个 slot，不能再包含 {"type":"background"}。
   不清版旧街景、不生成固定背景、不恢复旧照片作为默认图。
所有 slot 和 overlay 都必须在 layer_order 中各引用一次，顺序从底到顶。
不要输出 version、status、source、canvas、provider、created_at 或 prompt_version，应用程序会补齐。"""
