"""Verify the resumable project workflow and its explicit human gates."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from collage.cli import app as cli
from collage.core.errors import CollageError
from collage.core.io import atomic_save_image, atomic_write_json, read_json
from collage.projects import DataPaths, ProjectStore
from collage.template.review import confirm_draft
from collage.template.validation import validate_package
from collage.workflows import WorkflowService


def _manual_draft(*, with_photo: bool = False) -> dict:
    slots = []
    layers = [{"type": "background"}]
    if with_photo:
        slots.append(
            {
                "id": "photo",
                "label": "客户照片",
                "type": "image",
                "mode": "photo",
                "source_rect": [2, 2, 12, 10],
                "target_rect": [2, 2, 12, 10],
                "upload_hint": "上传一张照片",
                "review_notes": "工作流测试",
            }
        )
        layers.append({"type": "slot", "id": "photo"})
    return {
        "slots": slots,
        "overlays": [],
        "background": {
            "background_brief": "延续纯色背景",
            "review_notes": "工作流测试",
        },
        "layer_order": layers,
        "questions": [],
    }


def _source_files(
    tmp_path: Path, *, with_photo: bool = False
) -> tuple[Path, Path, Path]:
    reference = tmp_path / "outside" / "reference.jpg"
    manual = tmp_path / "outside" / "manual.json"
    candidate = tmp_path / "outside" / "background.png"
    reference.parent.mkdir()
    Image.new("RGB", (24, 16), "#CC8844").save(reference)
    Image.new("RGB", (24, 16), "#DDBB88").save(candidate)
    atomic_write_json(manual, _manual_draft(with_photo=with_photo))
    return reference, manual, candidate


def _confirm_project(store: ProjectStore, project_id: str) -> None:
    project = store.open(project_id)
    mask_path = project.review / "human_remove_mask.png"
    mask = Image.new("L", (24, 16), 0)
    for y in range(2, 14):
        for x in range(2, 22):
            mask.putpixel((x, y), 255)
    atomic_save_image(mask, mask_path)
    confirm_draft(
        project.analysis / "draft.json",
        project.review / "reviewed.json",
        remove_mask_path=mask_path,
        background_candidate_path=project.inputs / "background_candidate.png",
        reviewer="tester",
    )


def test_workflow_runs_to_preview_then_requires_explicit_approval(
    tmp_path: Path,
) -> None:
    reference, manual, candidate = _source_files(tmp_path)
    store = ProjectStore(DataPaths.resolve(tmp_path / "data"))
    workflow = WorkflowService(store)

    started = workflow.start(
        "complete-flow",
        reference,
        reviewer="tester",
        manual_draft_path=manual,
        background_candidate_path=candidate,
        open_review=False,
    )

    assert started["stage"] == "awaiting_review"
    project = store.open("complete-flow")
    manifest_text = project.manifest.read_text(encoding="utf-8")
    assert str(reference.resolve()) not in manifest_text
    assert str(manual.resolve()) not in manifest_text
    assert (project.inputs / "reference.png").is_file()
    assert (project.analysis / "draft.json").is_file()

    _confirm_project(store, "complete-flow")
    preview = workflow.resume("complete-flow", open_review=False)

    assert preview["stage"] == "awaiting_approval"
    assert (project.reports / "upload_guide.html").is_file()
    assert (project.renders / "bindings.example.json").is_file()
    assert (project.renders / "result.png").is_file()
    assert validate_package(project.template, require_ready=False)["status"] == (
        "needs_review"
    )

    completed = workflow.resume(
        "complete-flow",
        open_review=False,
        approve=True,
        approval_notes="已检查自动化测试预览",
    )

    assert completed["stage"] == "complete"
    assert completed["status"] == "ready"
    assert validate_package(project.template, require_ready=True)["status"] == "ready"
    assert workflow.status("complete-flow")["stage"] == "complete"


def test_workflow_waits_for_bindings_and_imports_customer_images(
    tmp_path: Path,
) -> None:
    reference, manual, candidate = _source_files(tmp_path, with_photo=True)
    store = ProjectStore(DataPaths.resolve(tmp_path / "data"))
    workflow = WorkflowService(store)
    workflow.start(
        "binding-flow",
        reference,
        reviewer="tester",
        manual_draft_path=manual,
        background_candidate_path=candidate,
        open_review=False,
    )
    _confirm_project(store, "binding-flow")

    waiting = workflow.resume("binding-flow", open_review=False)

    assert waiting["stage"] == "awaiting_bindings"
    assert waiting["wait"]["code"] == "BINDING_FILES_REQUIRED"

    customer_dir = tmp_path / "customer-source"
    customer_dir.mkdir()
    Image.new("RGB", (20, 20), "#2288CC").save(customer_dir / "photo.jpg")
    external_bindings = customer_dir / "bindings.json"
    atomic_write_json(
        external_bindings,
        {
            "version": "collage-bindings/1",
            "slots": {
                "photo": {
                    "path": "photo.jpg",
                    "scale": 1.0,
                    "offset_px": [0, 0],
                }
            },
        },
    )

    rendered = workflow.resume(
        "binding-flow",
        bindings_path=external_bindings,
        open_review=False,
    )

    project = store.open("binding-flow")
    imported = read_json(project.renders / "bindings.json")
    assert rendered["stage"] == "awaiting_approval"
    assert imported["slots"]["photo"]["path"].startswith("../inputs/customer/")
    assert (project.inputs / "customer" / "001_image.png").is_file()
    assert (project.renders / "result.png").is_file()
    assert str(customer_dir.resolve()) not in project.manifest.read_text(
        encoding="utf-8"
    )


def test_blocked_build_can_resume_with_a_different_provider(tmp_path: Path) -> None:
    reference, manual, _candidate = _source_files(tmp_path)
    store = ProjectStore(DataPaths.resolve(tmp_path / "data"))
    workflow = WorkflowService(store)
    workflow.start(
        "recover-flow",
        reference,
        reviewer="tester",
        manual_draft_path=manual,
        image_provider_spec="missing.module:provider",
        open_review=False,
    )
    project = store.open("recover-flow")
    mask_path = project.review / "human_remove_mask.png"
    atomic_save_image(Image.new("L", (24, 16), 255), mask_path)
    confirm_draft(
        project.analysis / "draft.json",
        project.review / "reviewed.json",
        remove_mask_path=mask_path,
        reviewer="tester",
    )

    with pytest.raises(CollageError) as caught:
        workflow.resume("recover-flow", open_review=False)
    assert caught.value.code == "PROVIDER_LOAD_FAILED"
    blocked = workflow.status("recover-flow")
    assert blocked["stage"] == "blocked"
    assert blocked["last_error"]["code"] == "PROVIDER_LOAD_FAILED"

    recovered = workflow.resume(
        "recover-flow",
        fixture_provider=True,
        open_review=False,
    )
    assert recovered["stage"] == "awaiting_approval"
    assert (project.renders / "result.png").is_file()


def test_cli_exposes_run_resume_and_status_commands(tmp_path: Path) -> None:
    run = cli._parser().parse_args(
        [
            "run",
            "--project",
            "sample",
            "--reference",
            str(tmp_path / "reference.png"),
            "--reviewer",
            "tester",
            "--no-review-ui",
        ]
    )
    resume = cli._parser().parse_args(
        ["resume", "--project", "sample", "--approve", "--no-review-ui"]
    )
    status = cli._parser().parse_args(["status", "--project", "sample"])

    assert run.command == "run" and run.no_review_ui is True
    assert resume.command == "resume" and resume.approve is True
    assert status.command == "status"
