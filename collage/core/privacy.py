"""Sanitize diagnostic evidence without exposing credentials, image bytes or local paths."""

from __future__ import annotations

import os
import re
from typing import Any

_PRIVATE_KEY = re.compile(
    r"api[_-]?key|authorization|password|secret|access[_-]?token|image_base64|b64_json",
    re.I,
)
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:[\\/][^\r\n，。；|\"'<>]*")
_POSIX_PATH = re.compile(r"(?<![:\w])/(?:[^ \t\r\n，。；|\"'<>]+)")


def safe_text(
    value: str, *, secrets: tuple[str, ...] = (), limit: int | None = None
) -> str:
    """Keep useful error text; remove known secrets before pattern-based redaction."""
    known = (*secrets, *(v for k, v in os.environ.items() if _PRIVATE_KEY.search(k)))
    for secret in sorted(
        {v for v in known if isinstance(v, str)}, key=len, reverse=True
    ):
        if secret:
            value = value.replace(secret, "<redacted>")
    value = re.sub(
        r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=\s]+", "<image-data>", value
    )
    value = re.sub(r"(?i)(bearer\s+)[^\s,;\"']+", r"\1<redacted>", value)
    value = re.sub(
        r"(?i)((?:api[_-]?key|token|password|secret|image_base64|b64_json)\s*[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'}]+",
        r"\1<redacted>",
        value,
    )
    value = re.sub(r"https?://[^\s\"'<>]+", "<service-url>", value)
    value = re.sub(r"[A-Za-z0-9+/=]{128,}", "<encoded-data>", value)
    value = _WINDOWS_PATH.sub("<path>", value)
    value = _POSIX_PATH.sub("<path>", value)
    return (
        value
        if limit is None or len(value) <= limit
        else value[:limit] + "…[truncated]"
    )


def safe_value(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    """Preserve JSON types, field paths and all issues while sanitizing their contents."""
    if isinstance(value, dict):
        return {
            safe_text(str(key), secrets=secrets): (
                "<redacted>"
                if _PRIVATE_KEY.search(str(key))
                else safe_value(item, secrets=secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [safe_value(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        return safe_text(value, secrets=secrets)
    return value
