"""Exercise candidate output, foreground sizing and explicit single-asset regeneration."""

import io
import json

import pytest
from PIL import Image, ImageDraw

from collage.core.errors import CollageError
from collage.core.io import read_json, sha256_file
from collage.projects import DataPaths
from collage.providers import GeneratedImage, ImageCapabilities, ProviderAudit
from collage.studio.workbench.application import WorkbenchApplication
from collage.studio.workbench.multipart import UploadedFile
from collage.template.validation import validate_package
from collage.template.layout import compile_layers
from test_workbench import _create_form, _review_payload, _wait_for_job


class CandidateProvider:
    """Offline artwork with generous margins; any VLM inspection is a test failure."""

    capabilities = ImageCapabilities(
        "candidate-fixture", "fixture", True, False, True, "white_edit", fixture=True
    )

    def __init__(self, broken=False):
        self.calls = []
        self.broken = broken

    def make_overlay(self, image, *, brief, **kwargs):
        self.calls.append(brief)
        if self.broken and "first" in brief:
            if self.broken == "empty":
                raw = Image.new("RGBA", (1000, 400))
                return GeneratedImage(
                    raw, ProviderAudit("fixture", "fixture", "fixture", None, True, 0)
                )
            raise CollageError("PROVIDER_TIMEOUT", "fixture timeout")
        raw = Image.new("RGBA", (1000, 400))
        ImageDraw.Draw(raw).rectangle((200, 150, 799, 249), fill="white")
        return GeneratedImage(
            raw,
            ProviderAudit("fixture", "fixture", "fixture", None, True, 0),
            raw_image=raw,
        )

    def inspect_overlay(self, *args, **kwargs):
        raise AssertionError("candidate pipeline must not inspect individual assets")


def _project(tmp_path, monkeypatch, *, broken=False, photo=False):
    app = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
    provider = CandidateProvider(broken)
    monkeypatch.setattr(app.workflow.stages, "_image_provider", lambda *_: provider)
    form = _create_form("candidate", with_photo=photo)
    draft = json.loads(form.files["manual_draft"].data)
    draft["overlays"] = [
        {
            "id": name,
            "label": name,
            "source_rect": [2, 2, 12, 10],
            "target_rect": [2, 2, 12, 10],
            "attachment": None,
            "action": "reference_generate",
            "generation_brief": name,
            "requires_exact_content": False,
            "review_notes": "",
        }
        for name in ("first", "second")
    ]
    draft["layer_order"] += [
        {"type": "overlay", "id": name} for name in ("first", "second")
    ]
    form.files["manual_draft"] = UploadedFile(
        "draft.json", "application/json", json.dumps(draft).encode()
    )
    app.start_project(form)
    _wait_for_job(app, "candidate")
    app.save_review("candidate", _review_payload(app, "candidate"))
    _wait_for_job(app, "candidate")
    return app, provider, app.store.open("candidate")


@pytest.mark.parametrize("failure", ["timeout", "empty"])
def test_failed_piece_is_skipped_and_workflow_still_renders(
    tmp_path, monkeypatch, failure
):
    app, provider, project = _project(tmp_path, monkeypatch, broken=failure)
    status = app.project_status("candidate")
    assert status["stage"] == "awaiting_approval"
    assert len(provider.calls) == 2
    template = validate_package(project.template, require_ready=False)
    assert "first" not in {a["id"] for a in template["assets"]}
    assert "second" in {a["id"] for a in template["assets"]}
    assert status["warnings"][0]["skipped"] is True
    assert status["warnings"][0]["code"] == (
        "EMPTY_OVERLAY" if failure == "empty" else "PROVIDER_TIMEOUT"
    )
    assert (project.renders / "result.png").is_file()
    assert "first" in (project.workspace / "inspection.html").read_text(
        encoding="utf-8"
    )
    app.workflow.resume("candidate", open_review=False)
    assert len(provider.calls) == 2


def test_template_keeps_resolution_and_places_subject_at_target_size(
    tmp_path, monkeypatch
):
    app, provider, project = _project(tmp_path, monkeypatch)
    from collage.rendering.layout import _fit_to_rect

    asset = Image.open(project.template / "assets/overlay_first.png")
    assert asset.size == (602, 102)
    assert Image.open(project.workspace / "overlay_first_full.png").size == (1000, 400)
    placed = _fit_to_rect(asset, (464, 69), fit="contain", anchor=(0.5, 0.5))
    box = placed.getchannel("A").point(lambda v: 255 if v >= 128 else 0).getbbox()
    assert (
        box[2] - box[0] > 390
    )  # visible lettering, not its surrounding 1000x400 canvas
    assert app.project_status("candidate")["stage"] == "awaiting_approval"
    assert len(provider.calls) == 2


def test_manual_regeneration_only_calls_one_piece_and_preserves_old_revision(
    tmp_path, monkeypatch
):
    app, provider, source = _project(tmp_path, monkeypatch)
    before = sha256_file(source.template / "template.json")
    second = sha256_file(source.template / "assets/overlay_second.png")
    status = app.project_status("candidate")
    app.regenerate_overlay(
        "candidate", {"overlay_id": "first", "revision": status["template_revision"]}
    )
    job = _wait_for_job(app, "candidate")
    target = app.store.open(job["result"]["project_id"])
    assert len(provider.calls) == 3 and "first" in provider.calls[-1]
    assert sha256_file(source.template / "template.json") == before
    assert sha256_file(target.template / "assets/overlay_second.png") == second
    assert app.project_status(target.project_id)["stage"] == "awaiting_approval"
    assert (
        read_json(target.template / "template.json")["review"]["visual_approved"]
        is False
    )


def test_missing_piece_can_be_restored_without_rebuilding_other_assets(
    tmp_path, monkeypatch
):
    app, provider, source = _project(tmp_path, monkeypatch, broken=True)
    provider.broken = False
    status = app.project_status("candidate")
    app.regenerate_overlay(
        "candidate", {"overlay_id": "first", "revision": status["template_revision"]}
    )
    job = _wait_for_job(app, "candidate")
    target = app.store.open(job["result"]["project_id"])
    template = validate_package(target.template, require_ready=False)
    assert len(provider.calls) == 3
    assert template["build"]["warnings"] == []
    assert [layer.get("asset_id") for layer in compile_layers(template)] == [
        "bg",
        "first",
        "second",
    ]


def test_missing_customer_photos_have_layout_preview_and_can_regenerate(
    tmp_path, monkeypatch
):
    app, provider, project = _project(tmp_path, monkeypatch, photo=True)
    assert app.project_status("candidate")["stage"] == "awaiting_bindings"
    png = app.preview_layout("candidate", app.layout("candidate"))
    assert Image.open(io.BytesIO(png)).size == (24, 16)
    status = app.project_status("candidate")
    app.regenerate_overlay(
        "candidate", {"overlay_id": "first", "revision": status["template_revision"]}
    )
    job = _wait_for_job(app, "candidate")
    assert (
        app.project_status(job["result"]["project_id"])["stage"] == "awaiting_bindings"
    )
    assert len(provider.calls) == 3


def test_background_revision_preserves_missing_decoration_definition(
    tmp_path, monkeypatch
):
    app, _provider, _source = _project(tmp_path, monkeypatch, broken=True)
    result = app.fork_background("candidate", app.background_revision("candidate"))
    target = app.store.open(result["project_id"])
    draft = read_json(target.analysis / "draft.json")
    assert [item["id"] for item in draft["overlays"]] == ["first", "second"]
    assert draft["layer_order"][-2:] == [
        {"type": "overlay", "id": "first"},
        {"type": "overlay", "id": "second"},
    ]


def test_failed_manual_regeneration_keeps_previous_candidate(tmp_path, monkeypatch):
    app, provider, source = _project(tmp_path, monkeypatch)
    original = sha256_file(source.template / "assets/overlay_first.png")
    provider.broken = True
    status = app.project_status("candidate")
    app.regenerate_overlay(
        "candidate", {"overlay_id": "first", "revision": status["template_revision"]}
    )
    job = _wait_for_job(app, "candidate")
    target = app.store.open(job["result"]["project_id"])
    assert sha256_file(target.template / "assets/overlay_first.png") == original
    warning = app.project_status(target.project_id)["warnings"][0]
    assert warning["skipped"] is False and "沿用" in warning["message"]
    assert len(provider.calls) == 3
