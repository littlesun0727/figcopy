"""Validate reusable template package manifests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core.errors import SpecValidationError, ValidationIssue
from .provenance import validate_provenance, validate_sha256
from .background import background_slot_id, validate_slot_background
from .attachments import validate_attachments
from .draft import _draft_layers
from .common import (
    FIT_MODES,
    IMAGE_MODES,
    _boolean,
    _canvas,
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


def _template_slot(slot_value: Any, path: str, issues: list[ValidationIssue]) -> None:
    slot = _object(slot_value, path, issues)
    if slot is None:
        return
    common = {"id", "type", "label", "required", "upload_hint", "rect", "rotation_deg"}
    image_fields = {"mode", "fit", "anchor", "clip_mask", "edge_fade_px"}
    image_fields.add("clip_mask_sha256")
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
            validate_sha256(
                slot.get("clip_mask_sha256"), f"{path}.clip_mask_sha256", issues
            )
        elif slot.get("clip_mask_sha256") is not None:
            _issue(issues, f"{path}.clip_mask_sha256", "无 clip mask 时哈希必须为 null")
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
    slot_background = background_slot_id(spec) is not None
    has_provenance = "provenance" in spec
    _keys(
        spec,
        required={
            "version",
            "status",
            "canvas",
            "assets",
            "slots",
            "overlays",
            "layer_order",
            "build",
            "review",
            *({"provenance"} if has_provenance else set()),
            *({"background"} if slot_background else set()),
        },
        optional=set(),
        path="$",
        issues=issues,
    )
    if spec.get("version") != "collage-template/4":
        _issue(
            issues,
            "$.version",
            "需要 collage-template/4，请新建项目",
            "UNSUPPORTED_SPEC_VERSION",
        )
    allowed_status = {"ready"} if require_ready else {"needs_review", "ready"}
    if has_provenance:
        validate_provenance(spec.get("provenance"), "$.provenance", issues)
        if not require_ready:
            allowed_status.add("needs_validation")
        provenance = spec.get("provenance")
        if isinstance(provenance, Mapping) and provenance.get("kind") != "human":
            # A1 的来源证据只证明如何制作，不是自动发布的质量验收。
            if spec.get("status") != "needs_validation":
                _issue(
                    issues,
                    "$.status",
                    "自动/fixture 制作仍需完整质量验收",
                    "AUTO_ACCEPTANCE_UNAVAILABLE",
                )
    _enum(spec.get("status"), allowed_status, "$.status", issues)
    manifest_claims_ready = spec.get("status") == "ready"
    canvas = _canvas(spec.get("canvas"), "$.canvas", issues)

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

    overlays = _list(spec.get("overlays"), "$.overlays", issues)
    for index, raw_overlay in enumerate(overlays or []):
        path = f"$.overlays[{index}]"
        overlay = _object(raw_overlay, path, issues)
        if overlay is None:
            continue
        _keys(
            overlay,
            required={"id", "attachment", "rect", "rotation_deg", "fit", "anchor"},
            optional=set(),
            path=path,
            issues=issues,
        )
        _identifier(overlay.get("id"), f"{path}.id", issues)
        _rect(overlay.get("rect"), f"{path}.rect", issues)
        _number(
            overlay.get("rotation_deg"),
            f"{path}.rotation_deg",
            issues,
            minimum=-3600,
            maximum=3600,
        )
        _enum(overlay.get("fit"), FIT_MODES, f"{path}.fit", issues)
        _pair(overlay.get("anchor"), f"{path}.anchor", issues, minimum=0, maximum=1)
    overlay_ids = _unique_ids(overlays, "$.overlays", issues)
    for identifier in sorted(overlay_ids & slot_ids):
        _issue(
            issues, "$.overlays", f"照片与装饰 ID 冲突：{identifier}", "DUPLICATE_ID"
        )
    background_slot = (
        validate_slot_background(
            spec.get("background"), slots, canvas, issues, template=True
        )
        if slot_background
        else None
    )
    independent = validate_attachments(overlays, slots, background_slot, issues)
    _draft_layers(
        spec.get("layer_order"),
        "$.layer_order",
        issues,
        slot_ids,
        independent,
        background_slot,
    )
    backgrounds = {
        asset["id"]
        for asset in assets or []
        if isinstance(asset, Mapping)
        and isinstance(asset.get("id"), str)
        and asset.get("role") == "background"
    }
    if len(backgrounds) != (0 if slot_background else 1):
        _issue(
            issues,
            "$.assets",
            "背景素材数量与背景来源不一致",
            "INVALID_BACKGROUND_COUNT",
        )
    if backgrounds & overlay_ids:
        _issue(issues, "$.overlays", "背景素材不能作为装饰引用", "INVALID_LAYER_ORDER")
    for identifier in sorted(asset_ids - backgrounds - overlay_ids):
        _issue(
            issues,
            "$.assets",
            f"素材缺少布局定义：{identifier}",
            "MISSING_LAYER_REFERENCE",
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
            optional={"warnings"},
            path="$.build",
            issues=issues,
        )
        _string(build.get("source_sha256"), "$.build.source_sha256", issues)
        _string(build.get("created_at"), "$.build.created_at", issues)
        _string(build.get("tool_version"), "$.build.tool_version", issues)
        _boolean(build.get("fixture_used"), "$.build.fixture_used", issues)
        warnings = _list(build.get("warnings", []), "$.build.warnings", issues)
        for index, warning in enumerate(warnings or []):
            path = f"$.build.warnings[{index}]"
            item = _object(warning, path, issues)
            if item is not None:
                for key in ("overlay_id", "label", "code", "message"):
                    _string(item.get(key), f"{path}.{key}", issues)
                _boolean(item.get("skipped"), f"{path}.skipped", issues)
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
