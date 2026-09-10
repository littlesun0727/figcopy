"""定义 Draft、ReviewedSpec、TemplateSpec 与 Bindings 的严格结构校验。"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .core.errors import SpecValidationError, ValidationIssue

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
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
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
    if value not in allowed:
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


def _draft_slot(
    slot_value: Any,
    path: str,
    issues: list[ValidationIssue],
    canvas: tuple[int, int] | None,
) -> None:
    slot = _object(slot_value, path, issues)
    if slot is None:
        return
    _keys(
        slot,
        required={
            "id",
            "label",
            "type",
            "mode",
            "source_rect",
            "target_rect",
            "upload_hint",
            "review_notes",
        },
        optional={"default_text"},
        path=path,
        issues=issues,
    )
    _identifier(slot.get("id"), f"{path}.id", issues)
    _string(slot.get("label"), f"{path}.label", issues)
    slot_type = slot.get("type")
    _enum(slot_type, {"image", "text"}, f"{path}.type", issues)
    if slot_type == "image":
        _enum(slot.get("mode"), DRAFT_IMAGE_MODES, f"{path}.mode", issues)
    elif slot.get("mode") is not None:
        _issue(issues, f"{path}.mode", "文字槽的 mode 必须为 null")
    if _rect(slot.get("source_rect"), f"{path}.source_rect", issues, source=True):
        _check_source_inside(
            slot.get("source_rect"), canvas, f"{path}.source_rect", issues
        )
    _rect(slot.get("target_rect"), f"{path}.target_rect", issues)
    _string(slot.get("upload_hint"), f"{path}.upload_hint", issues, allow_empty=True)
    _string(slot.get("review_notes"), f"{path}.review_notes", issues, allow_empty=True)
    if "default_text" in slot:
        _string(
            slot.get("default_text"),
            f"{path}.default_text",
            issues,
            allow_empty=True,
            nullable=True,
        )


def _draft_overlay(
    overlay_value: Any,
    path: str,
    issues: list[ValidationIssue],
    canvas: tuple[int, int] | None,
) -> None:
    overlay = _object(overlay_value, path, issues)
    if overlay is None:
        return
    _keys(
        overlay,
        required={
            "id",
            "label",
            "source_rect",
            "target_rect",
            "action",
            "generation_brief",
            "requires_exact_content",
            "review_notes",
        },
        optional=set(),
        path=path,
        issues=issues,
    )
    _identifier(overlay.get("id"), f"{path}.id", issues)
    _string(overlay.get("label"), f"{path}.label", issues)
    if _rect(overlay.get("source_rect"), f"{path}.source_rect", issues, source=True):
        _check_source_inside(
            overlay.get("source_rect"), canvas, f"{path}.source_rect", issues
        )
    _rect(overlay.get("target_rect"), f"{path}.target_rect", issues)
    _enum(overlay.get("action"), OVERLAY_ACTIONS, f"{path}.action", issues)
    _string(
        overlay.get("generation_brief"),
        f"{path}.generation_brief",
        issues,
        allow_empty=True,
    )
    _boolean(
        overlay.get("requires_exact_content"), f"{path}.requires_exact_content", issues
    )
    _string(
        overlay.get("review_notes"), f"{path}.review_notes", issues, allow_empty=True
    )


def _draft_layers(
    value: Any,
    path: str,
    issues: list[ValidationIssue],
    slot_ids: set[str],
    overlay_ids: set[str],
) -> None:
    layers = _list(value, path, issues)
    if layers is None:
        return
    references: list[tuple[str, str]] = []
    background_count = 0
    for index, raw_layer in enumerate(layers):
        layer_path = f"{path}[{index}]"
        layer = _object(raw_layer, layer_path, issues)
        if layer is None:
            continue
        layer_type = layer.get("type")
        if layer_type == "background":
            _keys(
                layer, required={"type"}, optional=set(), path=layer_path, issues=issues
            )
            background_count += 1
        elif layer_type in {"slot", "overlay"}:
            _keys(
                layer,
                required={"type", "id"},
                optional=set(),
                path=layer_path,
                issues=issues,
            )
            _identifier(layer.get("id"), f"{layer_path}.id", issues)
            if isinstance(layer.get("id"), str):
                references.append((layer_type, layer["id"]))
        else:
            _enum(layer_type, LAYER_TYPES, f"{layer_path}.type", issues)
    if (
        not layers
        or not isinstance(layers[0], Mapping)
        or layers[0].get("type") != "background"
    ):
        _issue(issues, path, "第一层必须是 background", "INVALID_LAYER_ORDER")
    if background_count != 1:
        _issue(issues, path, "background 必须且只能出现一次", "INVALID_LAYER_ORDER")
    expected = {
        *(("slot", name) for name in slot_ids),
        *(("overlay", name) for name in overlay_ids),
    }
    actual = set(references)
    if len(references) != len(actual):
        _issue(
            issues, path, "slot/overlay 图层引用不能重复", "DUPLICATE_LAYER_REFERENCE"
        )
    for missing in sorted(expected - actual):
        _issue(
            issues,
            path,
            f"缺少图层引用：{missing[0]}:{missing[1]}",
            "MISSING_LAYER_REFERENCE",
        )
    for unknown in sorted(actual - expected):
        _issue(
            issues,
            path,
            f"悬空图层引用：{unknown[0]}:{unknown[1]}",
            "DANGLING_LAYER_REFERENCE",
        )


def validate_draft(value: Any, *, require_metadata: bool = True) -> dict[str, Any]:
    """校验 VLM/人工草稿；不会从 description 反推任何机器字段。"""

    issues: list[ValidationIssue] = []
    draft = _object(value, "$", issues)
    if draft is None:
        raise SpecValidationError(issues)
    metadata = {
        "version",
        "status",
        "source",
        "canvas",
        "prompt_version",
        "provider",
        "created_at",
    }
    required = {"slots", "overlays", "background", "layer_order", "questions"}
    if require_metadata:
        required |= metadata
        optional: set[str] = set()
    else:
        optional = metadata
    _keys(draft, required=required, optional=optional, path="$", issues=issues)
    canvas = (
        _canvas(draft.get("canvas"), "$.canvas", issues) if "canvas" in draft else None
    )

    slots = _list(draft.get("slots"), "$.slots", issues)
    if slots is not None:
        for index, slot in enumerate(slots):
            _draft_slot(slot, f"$.slots[{index}]", issues, canvas)
    overlays = _list(draft.get("overlays"), "$.overlays", issues)
    if overlays is not None:
        for index, overlay in enumerate(overlays):
            _draft_overlay(overlay, f"$.overlays[{index}]", issues, canvas)
    slot_ids = _unique_ids(slots, "$.slots", issues)
    overlay_ids = _unique_ids(overlays, "$.overlays", issues)
    for collision in sorted(slot_ids & overlay_ids):
        _issue(issues, "$", f"slot 与 overlay 的 ID 冲突：{collision}", "DUPLICATE_ID")

    background = _object(draft.get("background"), "$.background", issues)
    if background is not None:
        _keys(
            background,
            required={"background_brief", "review_notes"},
            optional=set(),
            path="$.background",
            issues=issues,
        )
        _string(
            background.get("background_brief"),
            "$.background.background_brief",
            issues,
            allow_empty=True,
        )
        _string(
            background.get("review_notes"),
            "$.background.review_notes",
            issues,
            allow_empty=True,
        )
    _draft_layers(
        draft.get("layer_order"), "$.layer_order", issues, slot_ids, overlay_ids
    )

    questions = _list(draft.get("questions"), "$.questions", issues)
    if questions is not None:
        for index, question in enumerate(questions):
            _string(question, f"$.questions[{index}]", issues)

    if require_metadata:
        if draft.get("version") != "collage-draft/1":
            _issue(issues, "$.version", "必须是 collage-draft/1")
        if draft.get("status") != "draft":
            _issue(issues, "$.status", "草稿状态必须是 draft")
        source = _object(draft.get("source"), "$.source", issues)
        if source is not None:
            _keys(
                source,
                required={"path", "sha256", "width", "height"},
                optional=set(),
                path="$.source",
                issues=issues,
            )
            _string(source.get("path"), "$.source.path", issues)
            _string(source.get("sha256"), "$.source.sha256", issues)
            _integer(source.get("width"), "$.source.width", issues, minimum=1)
            _integer(source.get("height"), "$.source.height", issues, minimum=1)
            if canvas and (source.get("width"), source.get("height")) != canvas:
                _issue(issues, "$.source", "source 尺寸必须与 canvas 一致")
        _string(draft.get("prompt_version"), "$.prompt_version", issues)
        provider = _object(draft.get("provider"), "$.provider", issues)
        if provider is not None:
            _keys(
                provider,
                required={
                    "name",
                    "requested_model",
                    "actual_model",
                    "fixture",
                    "request_id",
                    "elapsed_ms",
                },
                optional={"cache_hit"},
                path="$.provider",
                issues=issues,
            )
            _string(provider.get("name"), "$.provider.name", issues)
            _string(
                provider.get("requested_model"),
                "$.provider.requested_model",
                issues,
                nullable=True,
            )
            _string(
                provider.get("actual_model"),
                "$.provider.actual_model",
                issues,
                nullable=True,
            )
            _boolean(provider.get("fixture"), "$.provider.fixture", issues)
            _string(
                provider.get("request_id"),
                "$.provider.request_id",
                issues,
                nullable=True,
            )
            _integer(
                provider.get("elapsed_ms"), "$.provider.elapsed_ms", issues, minimum=0
            )
        _string(draft.get("created_at"), "$.created_at", issues)

    if issues:
        raise SpecValidationError(issues, "Draft 校验失败")
    return dict(draft)


def draft_has_release_blockers(draft: Mapping[str, Any]) -> list[str]:
    """返回必须由人工解决后才能确认的语义阻塞项。"""

    blockers = [
        f"未确认图片模式：{slot['id']}"
        for slot in draft["slots"]
        if slot.get("mode") == "unknown"
    ]
    blockers.extend(
        f"待确认问题：{question}" for question in draft.get("questions", [])
    )
    return blockers


def _reviewed_slot(
    slot_value: Any,
    path: str,
    issues: list[ValidationIssue],
    canvas: tuple[int, int] | None,
) -> None:
    slot = _object(slot_value, path, issues)
    if slot is None:
        return
    common_required = {
        "id",
        "label",
        "type",
        "required",
        "mode",
        "source_rect",
        "target_rect",
        "upload_hint",
        "review_notes",
        "rotation_deg",
    }
    image_fields = {"fit", "anchor", "clip_mask", "edge_fade_px"}
    text_fields = {
        "default_text",
        "font_path",
        "font_size",
        "fallback_approved",
        "color",
        "align",
        "max_lines",
        "line_spacing",
    }
    slot_type = slot.get("type")
    required = common_required | (
        image_fields
        if slot_type == "image"
        else text_fields
        if slot_type == "text"
        else set()
    )
    _keys(slot, required=required, optional=set(), path=path, issues=issues)
    _identifier(slot.get("id"), f"{path}.id", issues)
    _string(slot.get("label"), f"{path}.label", issues)
    _enum(slot_type, {"image", "text"}, f"{path}.type", issues)
    _boolean(slot.get("required"), f"{path}.required", issues)
    if _rect(slot.get("source_rect"), f"{path}.source_rect", issues, source=True):
        _check_source_inside(
            slot.get("source_rect"), canvas, f"{path}.source_rect", issues
        )
    _rect(slot.get("target_rect"), f"{path}.target_rect", issues)
    _string(slot.get("upload_hint"), f"{path}.upload_hint", issues, allow_empty=True)
    _string(slot.get("review_notes"), f"{path}.review_notes", issues, allow_empty=True)
    _number(
        slot.get("rotation_deg"),
        f"{path}.rotation_deg",
        issues,
        minimum=-3600,
        maximum=3600,
    )
    if slot_type == "image":
        _enum(slot.get("mode"), IMAGE_MODES, f"{path}.mode", issues)
        _enum(slot.get("fit"), FIT_MODES, f"{path}.fit", issues)
        _pair(slot.get("anchor"), f"{path}.anchor", issues, minimum=0, maximum=1)
        if slot.get("clip_mask") is not None:
            _string(slot.get("clip_mask"), f"{path}.clip_mask", issues)
        _integer(
            slot.get("edge_fade_px"),
            f"{path}.edge_fade_px",
            issues,
            minimum=0,
            maximum=4096,
        )
        if slot.get("mode") != "photo_feather" and slot.get("edge_fade_px") not in {
            0,
            None,
        }:
            _issue(
                issues, f"{path}.edge_fade_px", "只有 photo_feather 可以设置边缘渐隐"
            )
        if (
            slot.get("mode") == "photo_feather"
            and slot.get("edge_fade_px") == 0
            and slot.get("clip_mask") is None
        ):
            _issue(issues, path, "photo_feather 必须设置 edge_fade_px 或槽位 clip_mask")
    elif slot_type == "text":
        if slot.get("mode") is not None:
            _issue(issues, f"{path}.mode", "文字槽的 mode 必须为 null")
        _string(
            slot.get("default_text"),
            f"{path}.default_text",
            issues,
            allow_empty=True,
            nullable=True,
        )
        _string(slot.get("font_path"), f"{path}.font_path", issues, nullable=True)
        _integer(
            slot.get("font_size"), f"{path}.font_size", issues, minimum=1, maximum=2048
        )
        _boolean(slot.get("fallback_approved"), f"{path}.fallback_approved", issues)
        _string(slot.get("color"), f"{path}.color", issues)
        _enum(slot.get("align"), {"left", "center", "right"}, f"{path}.align", issues)
        _integer(
            slot.get("max_lines"), f"{path}.max_lines", issues, minimum=1, maximum=100
        )
        _integer(
            slot.get("line_spacing"),
            f"{path}.line_spacing",
            issues,
            minimum=0,
            maximum=1024,
        )
        if slot.get("font_path") is None and slot.get("fallback_approved") is not True:
            _issue(issues, path, "缺少字体时必须由模板作者明确批准 fallback")


def validate_reviewed_spec(value: Any) -> dict[str, Any]:
    """校验人工确认后的制作规格。"""

    issues: list[ValidationIssue] = []
    spec = _object(value, "$", issues)
    if spec is None:
        raise SpecValidationError(issues)
    _keys(
        spec,
        required={
            "version",
            "status",
            "reference",
            "canvas",
            "slots",
            "overlays",
            "background",
            "layer_order",
            "review",
        },
        optional={"audit"},
        path="$",
        issues=issues,
    )
    if spec.get("version") != "collage-reviewed/1":
        _issue(issues, "$.version", "必须是 collage-reviewed/1")
    if spec.get("status") != "reviewed":
        _issue(issues, "$.status", "确认稿状态必须是 reviewed")
    canvas = _canvas(spec.get("canvas"), "$.canvas", issues)
    reference = _object(spec.get("reference"), "$.reference", issues)
    if reference is not None:
        _keys(
            reference,
            required={"path", "sha256"},
            optional=set(),
            path="$.reference",
            issues=issues,
        )
        _string(reference.get("path"), "$.reference.path", issues)
        _string(reference.get("sha256"), "$.reference.sha256", issues)

    slots = _list(spec.get("slots"), "$.slots", issues)
    if slots is not None:
        for index, slot in enumerate(slots):
            _reviewed_slot(slot, f"$.slots[{index}]", issues, canvas)
    overlays = _list(spec.get("overlays"), "$.overlays", issues)
    if overlays is not None:
        for index, raw_overlay in enumerate(overlays):
            path = f"$.overlays[{index}]"
            overlay = _object(raw_overlay, path, issues)
            if overlay is None:
                continue
            _keys(
                overlay,
                required={
                    "id",
                    "label",
                    "source_rect",
                    "target_rect",
                    "action",
                    "generation_brief",
                    "requires_exact_content",
                    "review_notes",
                    "rotation_deg",
                    "prepared_asset",
                    "background_mode",
                    "chroma_key",
                    "chroma_tolerance",
                    "shape",
                },
                optional=set(),
                path=path,
                issues=issues,
            )
            _identifier(overlay.get("id"), f"{path}.id", issues)
            _string(overlay.get("label"), f"{path}.label", issues)
            if _rect(
                overlay.get("source_rect"), f"{path}.source_rect", issues, source=True
            ):
                _check_source_inside(
                    overlay.get("source_rect"), canvas, f"{path}.source_rect", issues
                )
            _rect(overlay.get("target_rect"), f"{path}.target_rect", issues)
            action = overlay.get("action")
            _enum(action, OVERLAY_ACTIONS, f"{path}.action", issues)
            _string(
                overlay.get("generation_brief"),
                f"{path}.generation_brief",
                issues,
                allow_empty=True,
            )
            _boolean(
                overlay.get("requires_exact_content"),
                f"{path}.requires_exact_content",
                issues,
            )
            _string(
                overlay.get("review_notes"),
                f"{path}.review_notes",
                issues,
                allow_empty=True,
            )
            _number(
                overlay.get("rotation_deg"),
                f"{path}.rotation_deg",
                issues,
                minimum=-3600,
                maximum=3600,
            )
            if overlay.get("prepared_asset") is not None:
                _string(overlay.get("prepared_asset"), f"{path}.prepared_asset", issues)
            _enum(
                overlay.get("background_mode"),
                {"alpha", "chroma_key"},
                f"{path}.background_mode",
                issues,
            )
            if overlay.get("chroma_key") is not None:
                color = overlay.get("chroma_key")
                if not isinstance(color, list) or len(color) != 3:
                    _issue(issues, f"{path}.chroma_key", "必须是 [r,g,b] 或 null")
                else:
                    for channel_index, channel in enumerate(color):
                        _integer(
                            channel,
                            f"{path}.chroma_key[{channel_index}]",
                            issues,
                            minimum=0,
                            maximum=255,
                        )
            _integer(
                overlay.get("chroma_tolerance"),
                f"{path}.chroma_tolerance",
                issues,
                minimum=0,
                maximum=441,
            )
            if action == "basic_shape":
                _validate_shape(overlay.get("shape"), f"{path}.shape", issues)
            elif overlay.get("shape") is not None:
                _issue(
                    issues, f"{path}.shape", "reference_generate 的 shape 必须为 null"
                )
            if (
                overlay.get("requires_exact_content") is True
                and overlay.get("prepared_asset") is None
            ):
                _issue(
                    issues,
                    path,
                    "精确内容必须提供人工/原始 prepared_asset",
                    "EXACT_CONTENT_REQUIRED",
                )

    slot_ids = _unique_ids(slots, "$.slots", issues)
    overlay_ids = _unique_ids(overlays, "$.overlays", issues)
    for collision in sorted(slot_ids & overlay_ids):
        _issue(issues, "$", f"slot 与 overlay 的 ID 冲突：{collision}", "DUPLICATE_ID")
    _draft_layers(
        spec.get("layer_order"), "$.layer_order", issues, slot_ids, overlay_ids
    )

    background = _object(spec.get("background"), "$.background", issues)
    if background is not None:
        _keys(
            background,
            required={
                "background_brief",
                "review_notes",
                "remove_mask",
                "allowed_mask",
                "candidate_path",
                "expand_px",
                "feather_px",
            },
            optional=set(),
            path="$.background",
            issues=issues,
        )
        _string(
            background.get("background_brief"),
            "$.background.background_brief",
            issues,
            allow_empty=True,
        )
        _string(
            background.get("review_notes"),
            "$.background.review_notes",
            issues,
            allow_empty=True,
        )
        _string(background.get("remove_mask"), "$.background.remove_mask", issues)
        for key in ("allowed_mask", "candidate_path"):
            if background.get(key) is not None:
                _string(background.get(key), f"$.background.{key}", issues)
        _integer(
            background.get("expand_px"),
            "$.background.expand_px",
            issues,
            minimum=0,
            maximum=4096,
        )
        _integer(
            background.get("feather_px"),
            "$.background.feather_px",
            issues,
            minimum=0,
            maximum=4096,
        )

    review = _object(spec.get("review"), "$.review", issues)
    if review is not None:
        _keys(
            review,
            required={"reviewer", "reviewed_at", "notes", "questions_resolved"},
            optional=set(),
            path="$.review",
            issues=issues,
        )
        _string(review.get("reviewer"), "$.review.reviewer", issues)
        _string(review.get("reviewed_at"), "$.review.reviewed_at", issues)
        _string(review.get("notes"), "$.review.notes", issues, allow_empty=True)
        _boolean(
            review.get("questions_resolved"), "$.review.questions_resolved", issues
        )
        if review.get("questions_resolved") is not True:
            _issue(issues, "$.review.questions_resolved", "所有关键问题解决后才能构建")
    if issues:
        raise SpecValidationError(issues, "ReviewedSpec 校验失败")
    return dict(spec)


def _validate_shape(value: Any, path: str, issues: list[ValidationIssue]) -> None:
    shape = _object(value, path, issues)
    if shape is None:
        return
    _keys(
        shape,
        required={"kind", "fill", "outline", "width", "radius", "dash", "gap"},
        optional=set(),
        path=path,
        issues=issues,
    )
    kind = shape.get("kind")
    _enum(
        kind,
        {"rectangle", "rounded_rectangle", "ellipse", "dashed_rectangle"},
        f"{path}.kind",
        issues,
    )
    for key in ("fill", "outline"):
        if shape.get(key) is not None:
            _string(shape.get(key), f"{path}.{key}", issues)
    _integer(shape.get("width"), f"{path}.width", issues, minimum=0, maximum=1024)
    _integer(shape.get("radius"), f"{path}.radius", issues, minimum=0, maximum=8192)
    _integer(shape.get("dash"), f"{path}.dash", issues, minimum=1, maximum=8192)
    _integer(shape.get("gap"), f"{path}.gap", issues, minimum=0, maximum=8192)


def _template_slot(slot_value: Any, path: str, issues: list[ValidationIssue]) -> None:
    slot = _object(slot_value, path, issues)
    if slot is None:
        return
    common = {"id", "type", "label", "required", "upload_hint", "rect", "rotation_deg"}
    image_fields = {"mode", "fit", "anchor", "clip_mask", "edge_fade_px"}
    text_fields = {
        "default_text",
        "font_path",
        "font_size",
        "fallback_approved",
        "color",
        "align",
        "max_lines",
        "line_spacing",
    }
    slot_type = slot.get("type")
    required = common | (
        image_fields
        if slot_type == "image"
        else text_fields
        if slot_type == "text"
        else set()
    )
    _keys(slot, required=required, optional=set(), path=path, issues=issues)
    _identifier(slot.get("id"), f"{path}.id", issues)
    _enum(slot_type, {"image", "text"}, f"{path}.type", issues)
    _string(slot.get("label"), f"{path}.label", issues)
    _boolean(slot.get("required"), f"{path}.required", issues)
    _string(slot.get("upload_hint"), f"{path}.upload_hint", issues, allow_empty=True)
    _rect(slot.get("rect"), f"{path}.rect", issues)
    _number(
        slot.get("rotation_deg"),
        f"{path}.rotation_deg",
        issues,
        minimum=-3600,
        maximum=3600,
    )
    if slot_type == "image":
        _enum(slot.get("mode"), IMAGE_MODES, f"{path}.mode", issues)
        _enum(slot.get("fit"), FIT_MODES, f"{path}.fit", issues)
        _pair(slot.get("anchor"), f"{path}.anchor", issues, minimum=0, maximum=1)
        if slot.get("clip_mask") is not None:
            _string(slot.get("clip_mask"), f"{path}.clip_mask", issues)
        _integer(
            slot.get("edge_fade_px"),
            f"{path}.edge_fade_px",
            issues,
            minimum=0,
            maximum=4096,
        )
        if (
            slot.get("mode") == "photo_feather"
            and slot.get("edge_fade_px") == 0
            and slot.get("clip_mask") is None
        ):
            _issue(issues, path, "photo_feather 必须设置 edge_fade_px 或槽位 clip_mask")
    elif slot_type == "text":
        _string(
            slot.get("default_text"),
            f"{path}.default_text",
            issues,
            allow_empty=True,
            nullable=True,
        )
        if slot.get("font_path") is not None:
            _string(slot.get("font_path"), f"{path}.font_path", issues)
        _integer(
            slot.get("font_size"), f"{path}.font_size", issues, minimum=1, maximum=2048
        )
        _boolean(slot.get("fallback_approved"), f"{path}.fallback_approved", issues)
        _string(slot.get("color"), f"{path}.color", issues)
        _enum(slot.get("align"), {"left", "center", "right"}, f"{path}.align", issues)
        _integer(
            slot.get("max_lines"), f"{path}.max_lines", issues, minimum=1, maximum=100
        )
        _integer(
            slot.get("line_spacing"),
            f"{path}.line_spacing",
            issues,
            minimum=0,
            maximum=1024,
        )


def validate_template_spec(value: Any, *, require_ready: bool = True) -> dict[str, Any]:
    """校验模板清单本身；文件存在性由 validate 模块继续检查。"""

    issues: list[ValidationIssue] = []
    spec = _object(value, "$", issues)
    if spec is None:
        raise SpecValidationError(issues)
    _keys(
        spec,
        required={
            "version",
            "status",
            "canvas",
            "assets",
            "slots",
            "layers",
            "build",
            "review",
        },
        optional=set(),
        path="$",
        issues=issues,
    )
    if spec.get("version") != "collage-template/1":
        _issue(issues, "$.version", "必须是 collage-template/1")
    allowed_status = {"ready"} if require_ready else {"needs_review", "ready"}
    _enum(spec.get("status"), allowed_status, "$.status", issues)
    manifest_claims_ready = spec.get("status") == "ready"
    _canvas(spec.get("canvas"), "$.canvas", issues)

    assets = _list(spec.get("assets"), "$.assets", issues)
    if assets is not None:
        for index, raw_asset in enumerate(assets):
            path = f"$.assets[{index}]"
            asset = _object(raw_asset, path, issues)
            if asset is None:
                continue
            _keys(
                asset,
                required={"id", "path", "role", "requires_alpha", "sha256"},
                optional=set(),
                path=path,
                issues=issues,
            )
            _identifier(asset.get("id"), f"{path}.id", issues)
            _string(asset.get("path"), f"{path}.path", issues)
            _enum(asset.get("role"), {"background", "overlay"}, f"{path}.role", issues)
            _boolean(asset.get("requires_alpha"), f"{path}.requires_alpha", issues)
            _string(asset.get("sha256"), f"{path}.sha256", issues)
    slots = _list(spec.get("slots"), "$.slots", issues)
    if slots is not None:
        for index, slot in enumerate(slots):
            _template_slot(slot, f"$.slots[{index}]", issues)
    asset_ids = _unique_ids(assets, "$.assets", issues)
    slot_ids = _unique_ids(slots, "$.slots", issues)
    for collision in sorted(asset_ids & slot_ids):
        _issue(issues, "$", f"asset 与 slot 的 ID 冲突：{collision}", "DUPLICATE_ID")

    layers = _list(spec.get("layers"), "$.layers", issues)
    references: list[tuple[str, str]] = []
    if layers is not None:
        for index, raw_layer in enumerate(layers):
            path = f"$.layers[{index}]"
            layer = _object(raw_layer, path, issues)
            if layer is None:
                continue
            layer_type = layer.get("type")
            if layer_type == "asset":
                _keys(
                    layer,
                    required={
                        "type",
                        "asset_id",
                        "rect",
                        "rotation_deg",
                        "fit",
                        "anchor",
                    },
                    optional=set(),
                    path=path,
                    issues=issues,
                )
                _identifier(layer.get("asset_id"), f"{path}.asset_id", issues)
                if isinstance(layer.get("asset_id"), str):
                    references.append(("asset", layer["asset_id"]))
                _rect(layer.get("rect"), f"{path}.rect", issues)
                _number(
                    layer.get("rotation_deg"),
                    f"{path}.rotation_deg",
                    issues,
                    minimum=-3600,
                    maximum=3600,
                )
                _enum(layer.get("fit"), FIT_MODES, f"{path}.fit", issues)
                _pair(
                    layer.get("anchor"), f"{path}.anchor", issues, minimum=0, maximum=1
                )
            elif layer_type == "slot":
                _keys(
                    layer,
                    required={"type", "slot_id"},
                    optional=set(),
                    path=path,
                    issues=issues,
                )
                _identifier(layer.get("slot_id"), f"{path}.slot_id", issues)
                if isinstance(layer.get("slot_id"), str):
                    references.append(("slot", layer["slot_id"]))
            else:
                _enum(layer_type, TEMPLATE_LAYER_TYPES, f"{path}.type", issues)
        if not layers:
            _issue(issues, "$.layers", "至少需要一个背景层")
        elif isinstance(layers[0], Mapping):
            first_id = layers[0].get("asset_id")
            first_asset = next(
                (
                    item
                    for item in assets or []
                    if isinstance(item, Mapping) and item.get("id") == first_id
                ),
                None,
            )
            if (
                layers[0].get("type") != "asset"
                or not first_asset
                or first_asset.get("role") != "background"
            ):
                _issue(
                    issues,
                    "$.layers[0]",
                    "第一层必须引用 background asset",
                    "INVALID_LAYER_ORDER",
                )
    expected = {
        *(("asset", name) for name in asset_ids),
        *(("slot", name) for name in slot_ids),
    }
    actual = set(references)
    if len(references) != len(actual):
        _issue(
            issues,
            "$.layers",
            "每个 asset/slot 只能出现一次",
            "DUPLICATE_LAYER_REFERENCE",
        )
    for missing in sorted(expected - actual):
        _issue(
            issues,
            "$.layers",
            f"缺少图层引用：{missing[0]}:{missing[1]}",
            "MISSING_LAYER_REFERENCE",
        )
    for dangling in sorted(actual - expected):
        _issue(
            issues,
            "$.layers",
            f"悬空图层引用：{dangling[0]}:{dangling[1]}",
            "DANGLING_LAYER_REFERENCE",
        )

    build = _object(spec.get("build"), "$.build", issues)
    if build is not None:
        _keys(
            build,
            required={
                "source_sha256",
                "created_at",
                "tool_version",
                "fixture_used",
                "providers",
            },
            optional=set(),
            path="$.build",
            issues=issues,
        )
        _string(build.get("source_sha256"), "$.build.source_sha256", issues)
        _string(build.get("created_at"), "$.build.created_at", issues)
        _string(build.get("tool_version"), "$.build.tool_version", issues)
        _boolean(build.get("fixture_used"), "$.build.fixture_used", issues)
        providers = _list(build.get("providers"), "$.build.providers", issues)
        if providers is not None:
            for index, provider in enumerate(providers):
                if not isinstance(provider, Mapping):
                    _issue(issues, f"$.build.providers[{index}]", "必须是 object")
                    continue
                allowed = {
                    "node",
                    "name",
                    "requested_model",
                    "actual_model",
                    "request_id",
                    "fixture",
                    "cache_hit",
                    "elapsed_ms",
                }
                _keys(
                    provider,
                    required=allowed,
                    optional=set(),
                    path=f"$.build.providers[{index}]",
                    issues=issues,
                )

    review = _object(spec.get("review"), "$.review", issues)
    if review is not None:
        _keys(
            review,
            required={
                "visual_approved",
                "reviewer",
                "reviewed_at",
                "notes",
                "evidence_sha256",
            },
            optional=set(),
            path="$.review",
            issues=issues,
        )
        _boolean(review.get("visual_approved"), "$.review.visual_approved", issues)
        for key in ("reviewer", "reviewed_at"):
            _string(
                review.get(key),
                f"$.review.{key}",
                issues,
                nullable=not manifest_claims_ready,
            )
        _string(review.get("notes"), "$.review.notes", issues, allow_empty=True)
        evidence = _list(
            review.get("evidence_sha256"), "$.review.evidence_sha256", issues
        )
        if evidence is not None:
            for index, item in enumerate(evidence):
                _string(item, f"$.review.evidence_sha256[{index}]", issues)
        if manifest_claims_ready and (
            review.get("visual_approved") is not True or not evidence
        ):
            _issue(issues, "$.review", "ready 模板必须有人工作出的视觉验收与证据")
    if issues:
        raise SpecValidationError(issues, "TemplateSpec 校验失败")
    return dict(spec)


def validate_bindings(value: Any, template: Mapping[str, Any]) -> dict[str, Any]:
    """校验客户输入绑定，不触碰文件系统或调用 provider。"""

    issues: list[ValidationIssue] = []
    bindings = _object(value, "$", issues)
    if bindings is None:
        raise SpecValidationError(issues)
    _keys(
        bindings, required={"version", "slots"}, optional=set(), path="$", issues=issues
    )
    if bindings.get("version") != "collage-bindings/1":
        _issue(issues, "$.version", "必须是 collage-bindings/1")
    values = _object(bindings.get("slots"), "$.slots", issues)
    template_slots = {
        slot["id"]: slot
        for slot in template.get("slots", [])
        if isinstance(slot, Mapping) and "id" in slot
    }
    if values is not None:
        for slot_id in sorted(values):
            path = f"$.slots.{slot_id}"
            if slot_id not in template_slots:
                _issue(issues, path, "模板中不存在该槽位", "UNKNOWN_SLOT")
                continue
            binding = _object(values[slot_id], path, issues)
            if binding is None:
                continue
            slot = template_slots[slot_id]
            if slot.get("type") == "image":
                _keys(
                    binding,
                    required={"path"},
                    optional={"subject_alpha", "scale", "offset_px"},
                    path=path,
                    issues=issues,
                )
                _string(binding.get("path"), f"{path}.path", issues)
                if binding.get("subject_alpha") is not None:
                    _string(
                        binding.get("subject_alpha"), f"{path}.subject_alpha", issues
                    )
                if "scale" in binding:
                    _number(
                        binding.get("scale"),
                        f"{path}.scale",
                        issues,
                        minimum=0.05,
                        maximum=20,
                    )
                if "offset_px" in binding:
                    _pair(
                        binding.get("offset_px"),
                        f"{path}.offset_px",
                        issues,
                        minimum=-100000,
                        maximum=100000,
                    )
            else:
                _keys(
                    binding, required=set(), optional={"text"}, path=path, issues=issues
                )
                if "text" in binding:
                    _string(
                        binding.get("text"), f"{path}.text", issues, allow_empty=True
                    )
        for slot_id, slot in template_slots.items():
            has_confirmed_default = (
                slot.get("type") == "text" and slot.get("default_text") is not None
            )
            if (
                slot.get("required")
                and slot_id not in values
                and not has_confirmed_default
            ):
                _issue(issues, f"$.slots.{slot_id}", "缺少必填槽位", "MISSING_BINDING")
    if issues:
        raise SpecValidationError(issues, "Bindings 校验失败")
    return dict(bindings)
