"""Define conditional shape fields, model instructions, and executable compilation."""

from __future__ import annotations

import json
import math
from typing import Any

from PIL import ImageColor

from ..core.errors import SpecValidationError, ValidationIssue
from .common import _integer, _issue, _keys, _number, _object, _string

SHAPE_KINDS = {"rectangle", "rounded_rectangle", "ellipse", "dashed_rectangle"}
SHAPE_FIELDS = {"kind", "fill", "outline", "width", "radius", "dash", "gap"}
SHAPE_EXAMPLES = (
    {"kind": "rectangle", "outline": "#FFFFFF", "width": 3},
    {"kind": "ellipse", "fill": "#FFFFFF", "outline": None, "width": 0},
    {"kind": "rounded_rectangle", "outline": "#FFFFFF", "width": 3, "radius": 8.5},
    {
        "kind": "dashed_rectangle",
        "outline": "#FFFFFF",
        "width": 3,
        "dash": 12,
        "gap": 8,
    },
)
SHAPE_PROMPT = """图形字段契约 shape-fields/2：
basic_shape 的 shape 按 kind 提供实际需要的视觉参数，不要求输出无关的执行字段。
共同必填字段是 kind、outline、width；fill 可省略，省略或 null 表示不填充。
kind 只能是 rectangle、rounded_rectangle、ellipse、dashed_rectangle。
fill、outline 使用可解析的颜色字符串或 null；width 是 0..1024 的整数像素。
rounded_rectangle 必须提供 radius：0..8192 的有限数值，允许 8.5 这类小数，不能是 null。
dashed_rectangle 必须提供 dash（1..8192 整数）和 gap（0..8192 整数），单位为像素。
rectangle、ellipse 不需要 radius、dash、gap；rounded_rectangle 不需要 dash、gap；
dashed_rectangle 不需要 radius。无关参数请省略，不要为了凑字段编造值。
不要把小数半径取整，不要用字符串代替数值；必需的视觉参数由参考图识别。
reference_generate 的 shape 必须省略或为 null。
以下只是各类 shape 的合法格式示例，具体颜色、尺寸和间距须依据参考图：
""" + "\n".join(json.dumps(item, ensure_ascii=False) for item in SHAPE_EXAMPLES)


def _shape_integer(
    value: Any,
    path: str,
    issues: list[ValidationIssue],
    *,
    minimum: int,
    maximum: int,
    executable: bool,
) -> None:
    # JSON may encode an exact integer as 2.0. Drafts can retain that value;
    # compilation converts it before Pillow/range consume an actual Python int.
    if (
        not executable
        and isinstance(value, float)
        and math.isfinite(value)
        and value.is_integer()
    ):
        value = int(value)
    _integer(value, path, issues, minimum=minimum, maximum=maximum)


def validate_shape(
    value: Any,
    path: str,
    issues: list[ValidationIssue],
    *,
    executable: bool = False,
) -> None:
    """Validate visual fields by kind, without modifying source or guessing geometry."""
    shape = _object(value, path, issues)
    if shape is None:
        return
    kind = shape.get("kind")
    valid_kind = isinstance(kind, str) and kind in SHAPE_KINDS
    if not valid_kind:
        _issue(issues, f"{path}.kind", "不支持的图形类型", "INVALID_ENUM")
    required = {"kind", "outline", "width"}
    if kind == "rounded_rectangle":
        required.add("radius")
    elif kind == "dashed_rectangle":
        required.update({"dash", "gap"})
    if executable:
        required = SHAPE_FIELDS
    _keys(
        shape,
        required=required,
        optional=SHAPE_FIELDS - required,
        path=path,
        issues=issues,
    )
    for field in ("fill", "outline"):
        color = shape.get(field)
        if color is not None and _string(color, f"{path}.{field}", issues):
            try:
                ImageColor.getcolor(color, "RGBA")
            except (ValueError, TypeError):
                _issue(issues, f"{path}.{field}", "颜色无法解析", "INVALID_COLOR")
    if "width" in shape:
        _shape_integer(
            shape["width"],
            f"{path}.width",
            issues,
            minimum=0,
            maximum=1024,
            executable=executable,
        )
    # Unused parameters never reach drawing. Drafts may omit them or retain
    # legacy placeholders; compilation supplies canonical non-visual defaults.
    if (kind == "rounded_rectangle" or executable) and "radius" in shape:
        _number(shape["radius"], f"{path}.radius", issues, minimum=0, maximum=8192)
    if kind == "dashed_rectangle" or executable:
        for field, minimum in (("dash", 1), ("gap", 0)):
            if field in shape:
                _shape_integer(
                    shape[field],
                    f"{path}.{field}",
                    issues,
                    minimum=minimum,
                    maximum=8192,
                    executable=executable,
                )


def compile_shape(value: Any, *, path: str = "$.shape") -> dict[str, Any]:
    """Fill only non-visual defaults and preserve all participating visual values."""
    issues: list[ValidationIssue] = []
    validate_shape(value, path, issues)
    if issues:
        raise SpecValidationError(issues, "图形参数校验失败")
    shape = dict(value)
    shape.setdefault("fill", None)
    shape["width"] = int(shape["width"])
    if shape["kind"] != "rounded_rectangle":
        shape["radius"] = 0
    if shape["kind"] != "dashed_rectangle":
        shape["dash"], shape["gap"] = 1, 0
    else:
        shape["dash"], shape["gap"] = int(shape["dash"]), int(shape["gap"])
    return shape
