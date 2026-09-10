"""Shared retry, audit, sizing, and timestamp helpers for template builds."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image

from ...core.errors import CollageError
from ...core.io import atomic_write_bytes
from ...imaging.geometry import AffineRectTransform
from ...providers import GeneratedImage, ProviderAudit

LOGGER = logging.getLogger(__name__)
TRANSIENT_PROVIDER_CODES = {"RATE_LIMITED", "TEMPORARY_NETWORK_ERROR"}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _copy_atomic(source: Path, target: Path) -> None:
    atomic_write_bytes(target, source.read_bytes())


def _import_audit(name: str) -> ProviderAudit:
    return ProviderAudit(name, None, None, None, False, 0)


def _audit_from_cache(metadata: dict[str, Any]) -> ProviderAudit:
    raw = metadata["audit"]
    return ProviderAudit(
        name=raw["name"],
        requested_model=raw.get("requested_model"),
        actual_model=raw.get("actual_model"),
        request_id=raw.get("request_id"),
        fixture=bool(raw.get("fixture", False)),
        elapsed_ms=int(raw.get("elapsed_ms", 0)),
        cache_hit=True,
    )


def _call_with_retry(
    call: Any, *, operation: str, max_attempts: int = 3
) -> GeneratedImage:
    """只对明确的临时错误有限重试；超时、权限和参数错误不盲目重发。"""

    started = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except CollageError as exc:
            if exc.code not in TRANSIENT_PROVIDER_CODES or attempt == max_attempts:
                raise
            if time.monotonic() - started > 20:
                raise CollageError(
                    "PROVIDER_RETRY_BUDGET_EXHAUSTED", f"{operation} 已超过重试总时长"
                ) from exc
            delay = 0.25 * (2 ** (attempt - 1))
            LOGGER.warning(
                "provider 临时失败，准备有限重试 | operation=%s attempt=%s/%s",
                operation,
                attempt,
                max_attempts,
            )
            time.sleep(delay)
    raise AssertionError("retry loop must return or raise")


def _choose_provider_size(
    source_size: tuple[int, int],
    supported_sizes: tuple[tuple[int, int], ...],
) -> tuple[int, int] | None:
    """选择宽高比最接近的 provider 尺寸；返回 None 表示无需补边。"""

    if not supported_sizes or source_size in supported_sizes:
        return None
    if any(min(size) <= 0 for size in supported_sizes):
        raise CollageError(
            "INVALID_PROVIDER_CAPABILITIES", "provider 声明了非法 output_sizes"
        )
    source_ratio = source_size[0] / source_size[1]
    return min(
        supported_sizes, key=lambda size: abs((size[0] / size[1]) - source_ratio)
    )


def _pad_mask(mask: Image.Image, transform: AffineRectTransform) -> Image.Image:
    """使用与参考图相同的几何变换补边，新增区域保持内部语义的黑=不编辑。"""

    resized = mask.convert("L").resize(transform.content_size, Image.Resampling.LANCZOS)
    output = Image.new("L", transform.target_size, 0)
    output.paste(resized, (round(transform.offset_x), round(transform.offset_y)))
    return output


def _identity_transform(
    size: tuple[int, int], *, kind: str = "identity"
) -> dict[str, Any]:
    return {
        "kind": kind,
        "source_size": list(size),
        "target_size": list(size),
        "scale": [1.0, 1.0],
        "offset": [0.0, 0.0],
        "content_size": list(size),
    }
