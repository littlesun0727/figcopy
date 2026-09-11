"""Use the existing audited Yibu client for structured automatic visual inspections."""

from __future__ import annotations

import base64
import io
import json
import time
from typing import Any

from PIL import Image

from ...core.errors import CollageError
from ..base import ProviderAudit
from .client import _request_id, _YibuAuditClient
from .settings import YibuSettings
from .vision import _assistant_text, _chat_finish_reason


def parse_inspection(text: str) -> dict[str, Any]:
    """Accept one JSON object, optionally enclosed in a Markdown code fence."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        if "\n" not in stripped:
            raise CollageError("AUTO_INSPECTION_INVALID", "视觉检查 JSON 围栏不完整")
        stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(stripped)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CollageError(
            "AUTO_INSPECTION_INVALID", "视觉检查未返回完整 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise CollageError("AUTO_INSPECTION_INVALID", "视觉检查必须返回 JSON object")
    return value


class YibuInspectionProvider:
    """Inspect structure or results without changing the legacy Draft contract."""

    fixture = False

    def __init__(self, settings: YibuSettings | None = None) -> None:
        self.settings = settings or YibuSettings.from_env()
        self._client = _YibuAuditClient(self.settings)

    def inspect(
        self,
        images: list[tuple[str, Image.Image]],
        *,
        prompt: str,
        contract: str,
        operation: str,
    ) -> tuple[dict[str, Any], ProviderAudit, dict[str, Any]]:
        started = time.monotonic()
        content: list[dict[str, Any]] = [
            {"type": "text", "text": prompt + "\n\n" + contract}
        ]
        for label, image in images:
            stream = io.BytesIO()
            image.convert("RGB").save(stream, format="PNG")
            encoded = base64.b64encode(stream.getvalue()).decode("ascii")
            content.extend(
                [
                    {"type": "text", "text": label},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + encoded},
                    },
                ]
            )
        payload: dict[str, Any] = {
            "model": self.settings.vlm_model,
            "max_tokens": self.settings.vlm_max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if self.settings.vlm_reasoning_effort is not None:
            payload["reasoning_effort"] = self.settings.vlm_reasoning_effort
        response, headers = self._client.post_json(
            "/v1/chat/completions",
            payload,
            model=self.settings.vlm_model,
            operation=operation,
            auth_style="bearer",
        )
        if (_chat_finish_reason(response) or "").lower() in {
            "length",
            "max_tokens",
            "max_output_tokens",
        }:
            raise CollageError("PROVIDER_OUTPUT_TRUNCATED", "视觉检查输出被截断")
        result = parse_inspection(_assistant_text(response))
        audit = ProviderAudit(
            "yibu-audit-inspection",
            self.settings.vlm_model,
            response.get("model") if isinstance(response.get("model"), str) else None,
            _request_id(response, headers),
            False,
            round((time.monotonic() - started) * 1000),
        )
        # Only numeric usage metadata is retained; neither credentials nor encoded images enter evidence.
        raw_usage = response.get("usage")
        usage = {
            k: v
            for k, v in (raw_usage.items() if isinstance(raw_usage, dict) else [])
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        return result, audit, usage
