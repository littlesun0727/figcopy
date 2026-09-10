"""Validate reusable template package manifests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core.errors import SpecValidationError, ValidationIssue
from .common import (
    FIT_MODES,
    IMAGE_MODES,
    TEMPLATE_LAYER_TYPES,
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
