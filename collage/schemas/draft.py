"""Validate VLM and manually authored draft specifications."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core.errors import SpecValidationError, ValidationIssue
from .background import background_slot_id, validate_slot_background
from .attachments import validate_attachments
from .shapes import validate_shape
from .common import (
    DRAFT_IMAGE_MODES,
    LAYER_TYPES,
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
    _object,
    _rect,
    _string,
    _unique_ids,
)


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
            "attachment",
            "generation_brief",
            "requires_exact_content",
            "review_notes",
        },
        optional={"text_content", "shape"},
        path=path,
        issues=issues,
    )
    if "text_content" in overlay:
        _string(overlay["text_content"], f"{path}.text_content", issues, nullable=True)
    if overlay.get("shape") is not None:
        if overlay.get("action") == "basic_shape":
            validate_shape(overlay["shape"], f"{path}.shape", issues)
        else:
            _issue(issues, f"{path}.shape", "reference_generate 的 shape 必须为 null")
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
    background_slot: str | None = None,
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
    if background_slot is not None:
        if not layers or layers[0] != {"type": "slot", "id": background_slot}:
            _issue(
                issues, path, "第一层必须引用指定的背景照片槽", "INVALID_LAYER_ORDER"
            )
        if background_count:
            _issue(
                issues,
                path,
                "照片槽提供背景时不能再引用固定背景层",
                "INVALID_LAYER_ORDER",
            )
    elif (
        not layers
        or not isinstance(layers[0], Mapping)
        or layers[0].get("type") != "background"
    ):
        _issue(issues, path, "第一层必须是 background", "INVALID_LAYER_ORDER")
    if background_slot is None and background_count != 1:
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

    slot_background = background_slot_id(draft) is not None
    background_slot = None
    if slot_background:
        background_slot = validate_slot_background(
            draft.get("background"), slots, canvas, issues, draft=True
        )
    background = _object(draft.get("background"), "$.background", issues)
    if background is not None and not slot_background:
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
        draft.get("layer_order"),
        "$.layer_order",
        issues,
        slot_ids,
        validate_attachments(overlays, slots, background_slot, issues),
        background_slot,
    )

    questions = _list(draft.get("questions"), "$.questions", issues)
    if questions is not None:
        for index, question in enumerate(questions):
            _string(question, f"$.questions[{index}]", issues)

    if require_metadata:
        if draft.get("version") != "collage-draft/3":
            _issue(
                issues,
                "$.version",
                "需要 collage-draft/3，请新建项目",
                "UNSUPPORTED_SPEC_VERSION",
            )
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
