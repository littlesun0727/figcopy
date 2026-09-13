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

_DRAFT_CONTRACT = """只输出一个完整 JSON，不要 Markdown。必须有 slots、overlays、background、layer_order、questions。
先判断客户要替换什么，再明确背景来源；以下两种来源地位相同，不默认强制清版。
示例坐标仅说明格式（100×80 画布），实际必须按传入 canvas 和参考图识别，不照抄尺寸或槽数。

方案 A：客户只替换纸张/相册底板/固定场景中的照片，底板仍保留。
{"slots":[{"id":"photo","label":"内部照片","type":"image","mode":"photo",
"source_rect":[10,10,60,50],"target_rect":[10,10,60,50],"upload_hint":"上传内部照片","review_notes":"底板保留"}],
"overlays":[],"background":{"background_brief":"清除需要替换的旧内容，保留固定底板","review_notes":"固定背景"},
"layer_order":[{"type":"background"},{"type":"slot","id":"photo"}],"questions":[]}
固定背景必须且只能出现一次 type:background，并位于最底层。

方案 B：客户上传的一张普通照片将替换整个背景，其他元素独立叠加。
{"slots":[{"id":"base_photo","label":"背景照片","type":"image","mode":"photo",
"source_rect":[0,0,100,80],"target_rect":[0,0,100,80],"upload_hint":"上传不透明满版背景照片","review_notes":"背景可替换"},
{"id":"inset","label":"插图照片","type":"image","mode":"photo",
"source_rect":[10,10,30,20],"target_rect":[10,10,30,20],"upload_hint":"上传插图照片","review_notes":"独立照片"}],
"overlays":[],"background":{"mode":"slot","slot_id":"base_photo","review_notes":"由客户照片提供，无需制作固定背景"},
"layer_order":[{"type":"slot","id":"base_photo"},{"type":"slot","id":"inset"}],"questions":[]}
照片背景对应唯一 image/photo 槽，target_rect 必须为 [0,0,画布宽,画布高]；
最底层直接引用该槽，不能再包含 type:background。不清版旧场景，不恢复旧照片作默认图。
背景槽不能是抠图、羽化或局部窗口；但它上方可以有独立抠图、羽化或普通照片。
画面里有风景/人物，或发现满版槽，单独都不能证明客户要替换整个底板。
仅在 review_notes 写“无需清版”不够，必须用方案 B 的结构表达。
替换意图不明确时保留固定背景与 questions，等待客户复核；不要猜测省略底板。

slots 每项采用示例中的完整字段；type 可为 image 或 text，文字的 mode 为 null；
图片 mode 可为 photo、photo_feather、cutout、unknown。坐标格式均为 [x,y,width,height]。
每件固定装饰独立放入 overlays，字段为 id、label、source_rect、target_rect、
action（从 product_policy.overlay_actions 选择）、generation_brief、requires_exact_content（布尔）、review_notes；
可带 text_content 和合法 shape。示例没有装饰不意味着实际应该省略装饰。
所有 slot 和 overlay 都必须在 layer_order 中各引用一次，顺序从底到顶。
不要输出 version、status、source、canvas、provider、created_at 或 prompt_version，应用程序会补齐。"""
