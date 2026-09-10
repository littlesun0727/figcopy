"""通过 yibu VLM 把参考图分析为候选 Draft。"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

from ...core.errors import CollageError
from ..base import ProviderAudit
from .client import _request_id, _YibuAuditClient
from .constants import _DRAFT_CONTRACT, _DRAFT_REQUIRED_FIELDS
from .settings import YibuSettings

LOGGER = logging.getLogger(__name__)


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
