"""Verify the resumable project workflow and its explicit human gates."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from collage.cli import app as cli
from collage.core.errors import CollageError
from collage.core.io import atomic_save_image, atomic_write_json, read_json
from collage.projects import DataPaths, ProjectStore
from collage.providers import ProviderAudit
from collage.template.review import confirm_draft
from collage.template.validation import validate_package
from collage.workflows import WorkflowService


def _manual_draft(*, with_photo: bool = False, photo_mode: str = "photo") -> dict:
    slots = []
    layers = [{"type": "background"}]
    if with_photo:
        slots.append(
            {
                "id": "photo",
                "label": "客户照片",
                "type": "image",
                "mode": photo_mode,
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
    tmp_path: Path,
    *,
    with_photo: bool = False,
    photo_mode: str = "photo",
) -> tuple[Path, Path, Path]:
    reference = tmp_path / "outside" / "reference.jpg"
    manual = tmp_path / "outside" / "manual.json"
    candidate = tmp_path / "outside" / "background.png"
    reference.parent.mkdir()
    Image.new("RGB", (24, 16), "#CC8844").save(reference)
    Image.new("RGB", (24, 16), "#DDBB88").save(candidate)
    atomic_write_json(
        manual,
        _manual_draft(with_photo=with_photo, photo_mode=photo_mode),
    )
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
    external_bindings = customer_dir / "bindings.json"
    atomic_write_json(
        external_bindings,
        {
            "version": "collage-bindings/1",
            "slots": {
                "photo": {
                    "path": "missing.jpg",
                    "scale": 1.0,
                    "offset_px": [0, 0],
                }
            },
        },
    )
    with pytest.raises(CollageError) as caught:
        workflow.resume(
            "binding-flow",
            bindings_path=external_bindings,
            open_review=False,
        )
    assert caught.value.code == "FILE_NOT_FOUND"
    project = store.open("binding-flow")
    assert str(customer_dir.resolve()) not in project.manifest.read_text(
        encoding="utf-8"
    )

    Image.new("RGB", (20, 20), "#2288CC").save(customer_dir / "photo.jpg")
    bindings_payload = read_json(external_bindings)
    bindings_payload["slots"]["photo"]["path"] = "photo.jpg"
    atomic_write_json(external_bindings, bindings_payload)

    rendered = workflow.resume(
        "binding-flow",
        bindings_path=external_bindings,
        open_review=False,
    )

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


def test_workflow_automatically_prepares_opaque_cutout_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference, manual, candidate = _source_files(
        tmp_path,
        with_photo=True,
        photo_mode="cutout",
    )
    store = ProjectStore(DataPaths.resolve(tmp_path / "data"))
    workflow = WorkflowService(store)
    workflow.start(
        "cutout-flow",
        reference,
        reviewer="tester",
        manual_draft_path=manual,
        background_candidate_path=candidate,
        open_review=False,
    )
    _confirm_project(store, "cutout-flow")
    assert workflow.resume("cutout-flow", open_review=False)["stage"] == (
        "awaiting_bindings"
    )

    customer_dir = tmp_path / "cutout-customer"
    customer_dir.mkdir()
    Image.new("RGB", (20, 20), "#2288CC").save(customer_dir / "person.jpg")
    bindings_path = customer_dir / "bindings.json"
    atomic_write_json(
        bindings_path,
        {
            "version": "collage-bindings/1",
            "slots": {"photo": {"path": "person.jpg"}},
        },
    )

    class LocalCutout:
        name = "local-cutout-test"
        local_only = True

        def cutout(self, customer_image: Image.Image):
            alpha = Image.new("L", customer_image.size, 0)
            ImageDraw.Draw(alpha).ellipse((2, 2, 17, 17), fill=255)
            return alpha, ProviderAudit(
                self.name,
                "test-model",
                "test-model",
                "cutout-request",
                False,
                1,
            )

    provider = LocalCutout()

    def fake_load(spec: str, expected_protocol: object) -> LocalCutout:
        assert spec == "example:cutout"
        return provider

    monkeypatch.setattr("collage.workflows.bindings.load_provider", fake_load)
    rendered = workflow.resume(
        "cutout-flow",
        bindings_path=bindings_path,
        cutout_provider_spec="example:cutout",
        open_review=False,
    )

    project = store.open("cutout-flow")
    imported = read_json(project.renders / "bindings.json")
    prepared = project.inputs / "prepared" / "photo.png"
    assert rendered["stage"] == "awaiting_approval"
    assert imported["slots"]["photo"]["path"] == "../inputs/prepared/photo.png"
    assert Image.open(prepared).getchannel("A").getextrema() == (0, 255)
    assert prepared.with_suffix(".png.audit.json").is_file()


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


def test_cli_run_and_status_share_persisted_workflow(tmp_path: Path) -> None:
    reference, manual, _candidate = _source_files(tmp_path)
    data_root = tmp_path / "cli-data"
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "collage",
            "run",
            "--project",
            "cli-flow",
            "--data-dir",
            str(data_root),
            "--reference",
            str(reference),
            "--manual-draft",
            str(manual),
            "--reviewer",
            "tester",
            "--no-review-ui",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["stage"] == "awaiting_review"

    status = subprocess.run(
        [
            sys.executable,
            "-m",
            "collage",
            "status",
            "--project",
            "cli-flow",
            "--data-dir",
            str(data_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["stage"] == "awaiting_review"
    assert payload["artifacts"]["draft"]["exists"] is True
