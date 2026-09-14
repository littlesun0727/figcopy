"""Shared primitives for strict JSON specification validation."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from ..core.errors import ValidationIssue

IMAGE_MODES = {"photo", "photo_feather", "cutout"}
DRAFT_IMAGE_MODES = IMAGE_MODES | {"unknown"}
FIT_MODES = {"cover", "contain"}
OVERLAY_ACTIONS = {"reference_generate", "basic_shape"}
LAYER_TYPES = {"background", "slot", "overlay"}
TEMPLATE_LAYER_TYPES = {"asset", "slot"}
_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


def _issue(
    issues: list[ValidationIssue], path: str, message: str, code: str = "INVALID_VALUE"
) -> None:
    issues.append(ValidationIssue(path, message, code))


def _object(
    value: Any, path: str, issues: list[ValidationIssue]
) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        _issue(issues, path, "必须是 JSON object", "INVALID_TYPE")
        return None
    return value


def _list(value: Any, path: str, issues: list[ValidationIssue]) -> Sequence[Any] | None:
    if not isinstance(value, list):
        _issue(issues, path, "必须是 JSON array", "INVALID_TYPE")
        return None
    return value


def _keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    for name in sorted(required - value.keys()):
        _issue(issues, f"{path}.{name}", "缺少必填字段", "MISSING_FIELD")
    for name in sorted(value.keys() - required - optional):
        _issue(issues, f"{path}.{name}", "不允许的字段", "UNKNOWN_FIELD")


def _string(
    value: Any,
    path: str,
    issues: list[ValidationIssue],
    *,
    allow_empty: bool = False,
    nullable: bool = False,
) -> bool:
    if nullable and value is None:
        return True
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _issue(
            issues,
            path,
            "必须是非空字符串" if not allow_empty else "必须是字符串",
            "INVALID_TYPE",
        )
        return False
    return True


def _boolean(value: Any, path: str, issues: list[ValidationIssue]) -> bool:
    if not isinstance(value, bool):
        _issue(issues, path, "必须是 boolean", "INVALID_TYPE")
        return False
    return True


def _number(
    value: Any,
    path: str,
    issues: list[ValidationIssue],
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _issue(issues, path, "必须是有限数值", "INVALID_TYPE")
        return False
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        _issue(issues, path, "必须是有限数值", "INVALID_TYPE")
        return False
    if minimum is not None and value < minimum:
        _issue(issues, path, f"不能小于 {minimum}")
        return False
    if maximum is not None and value > maximum:
        _issue(issues, path, f"不能大于 {maximum}")
        return False
    return True


def _integer(
    value: Any,
    path: str,
    issues: list[ValidationIssue],
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        _issue(issues, path, "必须是整数", "INVALID_TYPE")
        return False
    return _number(value, path, issues, minimum=minimum, maximum=maximum)


def _identifier(value: Any, path: str, issues: list[ValidationIssue]) -> bool:
    if not _string(value, path, issues):
        return False
    if not _ID_PATTERN.fullmatch(value):
        _issue(
            issues, path, "ID 只能包含字母、数字、点、下划线和短横线，且必须以字母开头"
        )
        return False
    return True


def _enum(
    value: Any, allowed: set[str], path: str, issues: list[ValidationIssue]
) -> bool:
    if not isinstance(value, str) or value not in allowed:
        _issue(issues, path, f"必须是：{', '.join(sorted(allowed))}", "INVALID_ENUM")
        return False
    return True


def _rect(
    value: Any, path: str, issues: list[ValidationIssue], *, source: bool = False
) -> bool:
    items = _list(value, path, issues)
    if items is None:
        return False
    if len(items) != 4:
        _issue(issues, path, "rect 必须是 [x, y, width, height]")
        return False
    valid = all(
        _number(item, f"{path}[{index}]", issues) for index, item in enumerate(items)
    )
    if valid and (items[2] <= 0 or items[3] <= 0):
        _issue(issues, path, "rect 的 width/height 必须大于 0")
        valid = False
    if valid and source and (items[0] < 0 or items[1] < 0):
        _issue(issues, path, "source_rect 不允许从输入画布外开始")
        valid = False
    return valid


def _pair(
    value: Any,
    path: str,
    issues: list[ValidationIssue],
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> bool:
    items = _list(value, path, issues)
    if items is None:
        return False
    if len(items) != 2:
        _issue(issues, path, "必须包含两个数值")
        return False
    return all(
        _number(item, f"{path}[{index}]", issues, minimum=minimum, maximum=maximum)
        for index, item in enumerate(items)
    )


def _canvas(
    value: Any, path: str, issues: list[ValidationIssue]
) -> tuple[int, int] | None:
    canvas = _object(value, path, issues)
    if canvas is None:
        return None
    _keys(
        canvas,
        required={"width", "height", "coordinate_space", "rect_format"},
        optional=set(),
        path=path,
        issues=issues,
    )
    width_ok = _integer(
        canvas.get("width"), f"{path}.width", issues, minimum=1, maximum=32768
    )
    height_ok = _integer(
        canvas.get("height"), f"{path}.height", issues, minimum=1, maximum=32768
    )
    if canvas.get("coordinate_space") != "canvas_px":
        _issue(issues, f"{path}.coordinate_space", "必须固定为 canvas_px")
    if canvas.get("rect_format") != "xywh":
        _issue(issues, f"{path}.rect_format", "必须固定为 xywh")
    if width_ok and height_ok:
        return int(canvas["width"]), int(canvas["height"])
    return None


def _check_source_inside(
    rect: Any, canvas: tuple[int, int] | None, path: str, issues: list[ValidationIssue]
) -> None:
    if canvas is None or not isinstance(rect, list) or len(rect) != 4:
        return
    if not all(
        isinstance(item, (int, float)) and not isinstance(item, bool) for item in rect
    ):
        return
    x, y, width, height = rect
    if x < 0 or y < 0 or x + width > canvas[0] or y + height > canvas[1]:
        _issue(
            issues,
            path,
            "source_rect 必须完全位于规范化输入画布内",
            "RECT_OUT_OF_BOUNDS",
        )


def _unique_ids(items: Any, path: str, issues: list[ValidationIssue]) -> set[str]:
    result: list[str] = []
    if isinstance(items, list):
        result = [
            item.get("id")
            for item in items
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        ]
    duplicates = sorted(name for name, count in Counter(result).items() if count > 1)
    for name in duplicates:
        _issue(issues, path, f"ID 重复：{name}", "DUPLICATE_ID")
    return set(result)
