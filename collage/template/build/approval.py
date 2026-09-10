"""Publish a template after explicit human visual approval."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

from ...core.errors import CollageError
from ...core.io import (
    atomic_save_image,
    atomic_write_json,
    decode_image,
    read_json,
    sha256_file,
)
from ...core.state import WorkflowState
from ...schemas import validate_template_spec
from ..validation import validate_package
from .common import _utc_now

LOGGER = logging.getLogger(__name__)


def approve_template(
    template_dir: Path,
    evidence_paths: list[Path],
    *,
    reviewer: str,
    notes: str = "",
    allow_fixture: bool = False,
    work_dir: Path | None = None,
) -> Path:
    """记录人工验收证据并发布；不允许用“文件存在”替代视觉确认。"""

    root = template_dir.resolve()
    manifest_path = root / "template.json"
    template = validate_package(root, require_ready=False)
    if template["status"] != "needs_review":
        raise CollageError("INVALID_STATE", "只有 needs_review 模板可以批准")
    if template["build"]["fixture_used"] and not allow_fixture:
        raise CollageError(
            "FIXTURE_APPROVAL_BLOCKED",
            "模板含 fixture 生成素材；真实发布需重建，演示批准须显式 --allow-fixture",
        )
    if not evidence_paths:
        raise CollageError(
            "VISUAL_EVIDENCE_REQUIRED", "至少需要一张使用新客户素材生成的视觉验收图"
        )
    evidence_hashes: list[str] = []
    first_image: Image.Image | None = None
    canvas_size = (template["canvas"]["width"], template["canvas"]["height"])
    for path in evidence_paths:
        image = decode_image(path)
        if image.size != canvas_size:
            raise CollageError(
                "PREVIEW_SIZE_MISMATCH",
                f"验收图尺寸不匹配：{path}",
                details={"expected": canvas_size, "actual": image.size},
            )
        first_image = first_image or image.convert("RGBA")
        evidence_hashes.append(sha256_file(path))
    assert first_image is not None
    original = read_json(manifest_path)
    atomic_save_image(first_image, root / "preview.png")
    template["status"] = "ready"
    template["review"] = {
        "visual_approved": True,
        "reviewer": reviewer,
        "reviewed_at": _utc_now(),
        "notes": notes,
        "evidence_sha256": evidence_hashes,
    }
    validate_template_spec(template, require_ready=True)
    atomic_write_json(manifest_path, template)
    try:
        validate_package(root, require_ready=True)
    except Exception:
        atomic_write_json(manifest_path, original)
        raise
    resolved_work = (
        work_dir.resolve() if work_dir else root.parent / f"{root.name}.work"
    )
    state_path = resolved_work / "state.json"
    if state_path.is_file():
        WorkflowState(state_path).transition("ready")
    LOGGER.info("模板已通过人工验收并发布 | path=%s reviewer=%s", root, reviewer)
    return manifest_path
