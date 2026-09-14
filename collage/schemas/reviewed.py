"""Validate human-reviewed template construction specifications."""

from __future__ import annotations

from typing import Any

from ..core.errors import SpecValidationError, ValidationIssue
from .common import (
    FIT_MODES,
    IMAGE_MODES,
    OVERLAY_ACTIONS,
    _boolean,
    _canvas,
    _check_source_inside,
    _enum,
    _identifier,
    _integer,
    _issue,
    _keys,
    _list,
    _number,
    _object,
    _pair,
    _rect,
    _string,
    _unique_ids,
)
from .draft import _draft_layers
from .background import background_slot_id, validate_slot_background
from .attachments import validate_attachments
from .shapes import validate_shape
from .provenance import validate_provenance


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


def validate_build_spec(value: Any) -> dict[str, Any]:
    """校验统一制作规格及其人工或自动来源。"""

    issues: list[ValidationIssue] = []
    spec = _object(value, "$", issues)
    if spec is None:
        raise SpecValidationError(issues)
    slot_background = background_slot_id(spec) is not None
    planned = "provenance" in spec
    provenance = spec.get("provenance")
    human = not planned or (
        isinstance(provenance, dict) and provenance.get("kind") == "human"
    )
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
            *({"review"} if human else set()),
            *({"provenance"} if planned else set()),
        },
        optional={"audit"},
        path="$",
        issues=issues,
    )
    if spec.get("version") != "collage-build/4":
        _issue(
            issues,
            "$.version",
            "需要 collage-build/4，请新建项目",
            "UNSUPPORTED_SPEC_VERSION",
        )
    expected_status = "planned"
    if spec.get("status") != expected_status:
        _issue(issues, "$.status", f"制作规格状态必须是 {expected_status}")
    if planned:
        validate_provenance(provenance, "$.provenance", issues)
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
                    "attachment",
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
                optional={"text_content", "text_confirmed"},
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
            _enum(
                action,
                OVERLAY_ACTIONS | ({"preserve"} if planned else set()),
                f"{path}.action",
                issues,
            )
            if action == "preserve" and overlay.get("prepared_asset") is None:
                _issue(
                    issues,
                    path,
                    "保留动作必须有已分离的 prepared_asset",
                    "PRESERVED_ASSET_REQUIRED",
                )
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
                validate_shape(
                    overlay.get("shape"), f"{path}.shape", issues, executable=True
                )
            elif overlay.get("shape") is not None:
                _issue(
                    issues, f"{path}.shape", "reference_generate 的 shape 必须为 null"
                )
            if "text_content" in overlay:
                _string(
                    overlay["text_content"],
                    f"{path}.text_content",
                    issues,
                    nullable=True,
                )
            if "text_confirmed" in overlay:
                _boolean(overlay["text_confirmed"], f"{path}.text_confirmed", issues)
            if (
                overlay.get("text_content")
                and overlay.get("text_confirmed") is not True
            ):
                _issue(
                    issues,
                    path,
                    "生成前必须人工确认完整文字",
                    "TEXT_CONFIRMATION_REQUIRED",
                )
            if (
                overlay.get("requires_exact_content") is True
                and overlay.get("prepared_asset") is None
                and not (
                    overlay.get("text_content")
                    and overlay.get("text_confirmed") is True
                )
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
    background_slot = (
        validate_slot_background(spec.get("background"), slots, canvas, issues)
        if slot_background
        else None
    )
    _draft_layers(
        spec.get("layer_order"),
        "$.layer_order",
        issues,
        slot_ids,
        validate_attachments(overlays, slots, background_slot, issues),
        background_slot,
    )

    background = _object(spec.get("background"), "$.background", issues)
    if background is not None and not slot_background:
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
            optional={"composition_mode"},
            path="$.background",
            issues=issues,
        )
        _enum(
            background.get("composition_mode", "protected"),
            {"protected", "full_candidate"},
            "$.background.composition_mode",
            issues,
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

    review = _object(spec.get("review"), "$.review", issues) if human else None
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


def validate_reviewed_spec(value: Any) -> dict[str, Any]:
    """人工确认入口不接受自动执行规格。"""

    spec = validate_build_spec(value)
    if spec.get("provenance", {}).get("kind", "human") != "human":
        raise SpecValidationError(
            [ValidationIssue("$.version", "人工确认入口只接受人工确认的制作规格")]
        )
    return spec
