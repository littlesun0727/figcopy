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
from ...schemas import validate_reviewed_spec, validate_template_spec
from ..validation import validate_package
from .background import _build_background
from .common import _utc_now
from .overlays import _build_overlay
from .package import _package_slots, _template_layers
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
    """构建 needs_review 模板包；只有 approve 能把状态改成 ready。"""

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
    spec = validate_reviewed_spec(read_json(spec_path))
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "assets").mkdir(parents=True, exist_ok=True)
    (output_dir / "masks").mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "crops").mkdir(parents=True, exist_ok=True)
    (work_dir / "previews").mkdir(parents=True, exist_ok=True)
    state = WorkflowState(work_dir / "state.json")
    cache = NodeCache(work_dir / "cache")
    state.transition("building")
    LOGGER.info("开始构建模板 | spec=%s out=%s", spec_path, output_dir)
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
                crop = crop_source(reference, overlay["source_rect"])
                crops[overlay["id"]] = crop
                atomic_save_image(crop, work_dir / "crops" / f"{overlay['id']}.png")
        state.node("overlay_crops", "complete", count=len(crops))
        LOGGER.info("原图 overlay crop 已保存 | count=%s", len(crops))

        state.node("background", "running", input_sha256=spec["reference"]["sha256"])
        background_asset, background_audit = _build_background(
            spec, spec_path, reference, output_dir, work_dir, cache, image_provider
        )
        audits = [background_audit.as_dict(node="background")]
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
        assets = [background_asset]
        visible_regions: dict[str, list[int] | None] = {}
        for overlay in spec["overlays"]:
            state.node(
                f"overlay:{overlay['id']}",
                "running",
                source_rect=overlay["source_rect"],
            )
            asset, audit, visible_bbox = _build_overlay(
                overlay,
                spec,
                spec_path,
                crops.get(overlay["id"]),
                output_dir,
                work_dir,
                cache,
                image_provider,
            )
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
            )
        slots = _package_slots(spec, spec_path, output_dir)
        template = {
            "version": "collage-template/1",
            "status": "needs_review",
            "canvas": spec["canvas"],
            "assets": assets,
            "slots": slots,
            "layers": _template_layers(spec),
            "build": {
                "source_sha256": spec["reference"]["sha256"],
                "created_at": _utc_now(),
                "tool_version": __version__,
                "fixture_used": any(item["fixture"] for item in audits),
                "providers": audits,
            },
            "review": {
                "visual_approved": False,
                "reviewer": None,
                "reviewed_at": None,
                "notes": "",
                "evidence_sha256": [],
            },
        }
        validate_template_spec(template, require_ready=False)
        atomic_write_json(manifest_path, template)
        validate_package(output_dir, require_ready=False)
        _write_inspection_report(work_dir, output_dir, spec, audits)
        state.transition("needs_review")
        LOGGER.info(
            "模板构建完成，等待人工视觉验收 | manifest=%s report=%s",
            manifest_path,
            work_dir / "inspection.html",
        )
        return manifest_path
    except CollageError as exc:
        blocked_codes = {
            "IMAGE_PROVIDER_UNAVAILABLE",
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
