"""Orchestrate construction of a reviewable template package."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

from ... import __version__
from ...core.errors import CollageError
from ...core.io import (
    atomic_save_image,
    atomic_write_json,
    decode_image,
    read_json,
    resolve_input_path,
    sha256_file,
)
from ...core.state import NodeCache, WorkflowState
from ...imaging.operations import crop_source
from ...providers import ImageProvider
from ...schemas import validate_build_spec, validate_template_spec
from ...schemas.background import background_slot_id
from ..validation import validate_package
from .background import _build_background
from .common import _utc_now
from .overlays import _build_overlay
from .asset_validation import overlay_warning
from .package import _package_slots, _package_overlays
from .report import _write_inspection_report

LOGGER = logging.getLogger(__name__)


def build_template(
    spec_path: Path,
    output_dir: Path,
    *,
    work_dir: Path | None = None,
    image_provider: ImageProvider | None = None,
    force: bool = False,
) -> Path:
    """构建待验收模板；自动来源与人工确认来源分别记录。"""

    spec_path = spec_path.resolve()
    output_dir = output_dir.resolve()
    work_dir = (
        work_dir.resolve()
        if work_dir
        else output_dir.parent / f"{output_dir.name}.work"
    )
    manifest_path = output_dir / "template.json"
    if manifest_path.is_file():
        existing = read_json(manifest_path)
        if existing.get("status") == "ready":
            raise CollageError(
                "READY_TEMPLATE_PROTECTED", "已发布模板不能被 build 无声覆盖"
            )
        if not force:
            raise CollageError(
                "OUTPUT_EXISTS", "模板构建结果已存在；断点重建请显式使用 --force"
            )
    spec = validate_build_spec(read_json(spec_path))
    has_provenance = "provenance" in spec
    photo_background = background_slot_id(spec)
    pending_status = (
        "needs_validation"
        if has_provenance and spec["provenance"]["kind"] != "human"
        else "needs_review"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "assets").mkdir(parents=True, exist_ok=True)
    (output_dir / "masks").mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "crops").mkdir(parents=True, exist_ok=True)
    (work_dir / "previews").mkdir(parents=True, exist_ok=True)
    state = WorkflowState(work_dir / "state.json")
    cache = NodeCache(work_dir / "cache")
    state.data["last_error"] = None
    state.transition("building")
    LOGGER.info("开始构建模板 | version=%s", spec["version"])
    try:
        reference_path = resolve_input_path(spec_path, spec["reference"]["path"])
        if sha256_file(reference_path) != spec["reference"]["sha256"]:
            raise CollageError(
                "SOURCE_HASH_MISMATCH", "参考工作图哈希与 ReviewedSpec 不一致"
            )
        reference = decode_image(reference_path).convert("RGBA")
        expected_size = (spec["canvas"]["width"], spec["canvas"]["height"])
        if reference.size != expected_size:
            raise CollageError(
                "SOURCE_SIZE_MISMATCH", "参考工作图尺寸与 ReviewedSpec canvas 不一致"
            )

        # 必须先从原参考图保存所有复杂装饰 crop，再开始任何背景清版。
        crops: dict[str, Image.Image] = {}
        for overlay in spec["overlays"]:
            if overlay["action"] == "reference_generate":
                x, y, width, height = overlay["source_rect"]
                padding = max(2, round(min(width, height) * 0.15))
                left, top = max(0, x - padding), max(0, y - padding)
                right, bottom = (
                    min(reference.width, x + width + padding),
                    min(reference.height, y + height + padding),
                )
                crop = crop_source(reference, [left, top, right - left, bottom - top])
                crops[overlay["id"]] = crop
                atomic_save_image(crop, work_dir / "crops" / f"{overlay['id']}.png")
        state.node("overlay_crops", "complete", count=len(crops))
        LOGGER.info("原图 overlay crop 已保存 | count=%s", len(crops))

        assets = []
        audits = []
        if photo_background:
            # The mandatory customer photo supplies the entire base; no hidden image is reconstructed.
            state.node(
                "background",
                "skipped",
                code="BACKGROUND_PROVIDED_BY_SLOT",
                slot_id=photo_background,
                outputs=[],
                audit=None,
            )
            LOGGER.info(
                "跳过固定背景制作 | code=BACKGROUND_PROVIDED_BY_SLOT slot=%s",
                photo_background,
            )
        else:
            state.node(
                "background", "running", input_sha256=spec["reference"]["sha256"]
            )
            background_asset, background_audit = _build_background(
                spec, spec_path, reference, output_dir, work_dir, cache, image_provider
            )
            audits.append(background_audit.as_dict(node="background"))
            state.node(
                "background",
                "complete",
                audit=audits[-1],
                outputs=[
                    "background_candidate.png",
                    "background.png",
                    "remove_mask.png",
                    "blend_mask.png",
                ],
            )
            assets.append(background_asset)
        warnings: list[dict] = []
        visible_regions: dict[str, list[int] | None] = {}
        for overlay in spec["overlays"]:
            state.node(
                f"overlay:{overlay['id']}",
                "running",
                source_rect=overlay["source_rect"],
            )
            try:
                asset, audit, visible_bbox, findings = _build_overlay(
                    overlay,
                    spec,
                    spec_path,
                    crops.get(overlay["id"]),
                    output_dir,
                    work_dir,
                    cache,
                    image_provider,
                )
            except CollageError as exc:
                finding = overlay_warning(overlay, exc.code, skipped=True)
                warnings.append(finding)
                state.node(
                    f"overlay:{overlay['id']}",
                    "skipped",
                    error={"code": exc.code, "message": finding["message"]},
                )
                LOGGER.warning(
                    "跳过不可用装饰，继续构建 | id=%s code=%s", overlay["id"], exc.code
                )
                continue
            warnings.extend(findings)
            assets.append(asset)
            audits.append(audit.as_dict(node=f"overlay:{overlay['id']}"))
            visible_regions[overlay["id"]] = (
                list(visible_bbox) if visible_bbox else None
            )
            state.node(
                f"overlay:{overlay['id']}",
                "complete",
                audit=audits[-1],
                visible_bbox=visible_regions[overlay["id"]],
                output=asset["path"],
                warnings=findings,
                error=None,
            )
        slots = _package_slots(spec, spec_path, output_dir)
        template = {
            "version": "collage-template/4",
            "status": pending_status,
            "canvas": spec["canvas"],
            "assets": assets,
            "slots": slots,
            "overlays": _package_overlays(spec),
            "layer_order": spec["layer_order"],
            "build": {
                "source_sha256": spec["reference"]["sha256"],
                "created_at": _utc_now(),
                "tool_version": __version__,
                "fixture_used": any(item["fixture"] for item in audits)
                or spec.get("audit", {})
                .get("analysis_provider", {})
                .get("fixture", False)
                or (has_provenance and spec["provenance"]["kind"] == "fixture"),
                "providers": audits,
                "warnings": warnings,
            },
            "review": {
                "visual_approved": False,
                "reviewer": None,
                "reviewed_at": None,
                "notes": "",
                "evidence_sha256": [],
            },
        }
        if has_provenance:
            template["provenance"] = spec["provenance"]
        if photo_background:
            template["background"] = {"mode": "slot", "slot_id": photo_background}
        validate_template_spec(template, require_ready=False)
        atomic_write_json(manifest_path, template)
        validate_package(output_dir, require_ready=False)
        _write_inspection_report(work_dir, output_dir, spec, audits, warnings)
        state.transition(pending_status)
        LOGGER.info("模板构建完成，等待验收 | status=%s", pending_status)
        return manifest_path
    except CollageError as exc:
        blocked_codes = {
            "IMAGE_PROVIDER_UNAVAILABLE",
            "CHROMA_DEPENDENCY_MISSING",
            "CHROMA_FILTER_UNAVAILABLE",
            "IMAGE_PROVIDER_CAPABILITY_MISSING",
            "EXACT_CONTENT_REQUIRED",
        }
        node_status = "blocked" if exc.code in blocked_codes else "failed"
        for name, record in list(state.data["nodes"].items()):
            if record.get("status") == "running":
                state.node(
                    name, node_status, error={"code": exc.code, "message": exc.message}
                )
        state.fail(exc.code, exc.message, blocked=exc.code in blocked_codes)
        LOGGER.error("模板构建中止 | code=%s message=%s", exc.code, exc.message)
        raise
    except Exception as exc:
        for name, record in list(state.data["nodes"].items()):
            if record.get("status") == "running":
                state.node(
                    name,
                    "failed",
                    error={"code": "UNEXPECTED_ERROR", "message": type(exc).__name__},
                )
        state.fail("UNEXPECTED_ERROR", type(exc).__name__, blocked=False)
        LOGGER.exception("模板构建发生未预期错误")
        raise
