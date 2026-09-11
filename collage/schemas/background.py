"""Validate explicit customer-photo backgrounds without a fixed background asset."""

from collections.abc import Mapping
from typing import Any

from ..core.errors import ValidationIssue
from .common import _identifier, _issue, _keys, _object, _string


def background_slot_id(spec: Mapping[str, Any]) -> str | None:
    """Read the explicit background source; never infer it from labels or notes."""
    background = spec.get("background")
    if isinstance(background, Mapping) and background.get("mode") == "slot":
        value = background.get("slot_id")
        return value if isinstance(value, str) else None
    return None


def validate_slot_background(
    value: Any,
    slots: Any,
    canvas: tuple[int, int] | None,
    issues: list[ValidationIssue],
    *,
    draft: bool = False,
    template: bool = False,
) -> str | None:
    """Require one full-canvas photo; actual cropped alpha is checked at render time."""
    background = _object(value, "$.background", issues)
    if background is None:
        return None
    _keys(
        background,
        required={"mode", "slot_id"} | (set() if template else {"review_notes"}),
        optional=set(),
        path="$.background",
        issues=issues,
    )
    if background.get("mode") != "slot":
        _issue(issues, "$.background.mode", "此版本的背景必须来自客户照片槽")
    if not template:
        _string(
            background.get("review_notes"),
            "$.background.review_notes",
            issues,
            allow_empty=True,
        )
    slot_id = background.get("slot_id")
    if not _identifier(slot_id, "$.background.slot_id", issues):
        return None
    candidates = [
        (index, slot)
        for index, slot in enumerate(slots if isinstance(slots, list) else [])
        if isinstance(slot, Mapping) and slot.get("id") == slot_id
    ]
    if len(candidates) != 1:
        _issue(
            issues,
            "$.background.slot_id",
            "背景必须引用唯一的图片槽",
            "INVALID_BACKGROUND_SLOT",
        )
        return slot_id
    index, slot = candidates[0]
    path = f"$.slots[{index}]"
    if slot.get("type") != "image" or slot.get("mode") != "photo":
        _issue(
            issues,
            path,
            "背景槽必须是普通照片，不能抠图或羽化",
            "INVALID_BACKGROUND_SLOT",
        )
    rect_key = "rect" if template else "target_rect"
    # Exact full-canvas placement gives a checkable coverage contract, not a guessed tolerance.
    if canvas and slot.get(rect_key) != [0, 0, canvas[0], canvas[1]]:
        _issue(
            issues,
            f"{path}.{rect_key}",
            "背景照片槽必须覆盖整个画布",
            "INVALID_BACKGROUND_SLOT",
        )
    if not draft:
        expected = {
            "required": True,
            "fit": "cover",
            "rotation_deg": 0,
            "clip_mask": None,
            "edge_fade_px": 0,
        }
        for key, expected_value in expected.items():
            if slot.get(key) != expected_value:
                _issue(
                    issues,
                    f"{path}.{key}",
                    "背景槽必须必填、铺满且无旋转、开洞或羽化",
                    "INVALID_BACKGROUND_SLOT",
                )
    return slot_id
