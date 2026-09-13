"""Verify explicit background conversion and immutable forks using synthetic media."""

from __future__ import annotations

import base64
import copy
import io
import threading

import pytest
from PIL import Image
from test_build_cache import ChromaOnlyOverlayProvider, CountingImageProvider
from test_photo_background import FixtureVision, draft_file, raw_photo

from collage.core.errors import CollageError, SpecValidationError
from collage.core.io import atomic_write_json, read_json, sha256_file
from collage.projects import DataPaths
from collage.schemas import validate_draft
from collage.studio.review_session import ReviewSession
from collage.studio.workbench.application import WorkbenchApplication
from collage.studio.workbench.layout import fork_layout, layout_document
from collage.template.build import build_template
from collage.template.review.background_source import edited_review_draft
from collage.template.review.feedback import revise_draft, validate_feedback
from collage.workflows.stages import WorkflowStages


def fixed_raw(*, decoration=True):
    """A full-canvas slot alone does not express intent to remove the fixed base."""
    raw = raw_photo(decoration=decoration)
    raw["background"] = {
        "background_brief": "保留纸张底板",
        "review_notes": "无自动切换",
    }
    raw["layer_order"].insert(0, {"type": "background"})
    return raw


def save_payload(session, selection=None):
    """Translate the server's grayscale mask into the browser alpha convention."""
    with Image.open(io.BytesIO(session.fixed_mask_png)) as source:
        mask = Image.new("RGBA", source.size, "white")
        mask.putalpha(source.convert("L"))
    buffer = io.BytesIO()
    mask.save(buffer, format="PNG")
    return {
        "draft": copy.deepcopy(session.draft),
        "revision": session.review_options["revision"],
        "background_selection": selection,
        "final_confirmed": True,
        "overlay_text_review_opened": False,
        "mask_png": "data:image/png;base64,"
        + base64.b64encode(buffer.getvalue()).decode("ascii"),
    }


def background_project(tmp_path, *, photo=False, decoration=True):
    """Create an ordinary awaiting-review project with a manual synthetic draft."""
    reference = tmp_path / "source.png"
    Image.new("RGB", (40, 30), "orange").save(reference)
    manual = tmp_path / "manual.json"
    atomic_write_json(
        manual,
        raw_photo(decoration=decoration) if photo else fixed_raw(decoration=decoration),
    )
    app = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
    app.workflow.start(
        "source",
        reference,
        reviewer="fixture-tester",
        manual_draft_path=manual,
        fixture_provider=True,
        open_review=False,
    )
    return app, app.store.open("source")


def tree_hashes(root):
    return {
        p.relative_to(root).as_posix(): sha256_file(p)
        for p in root.rglob("*")
        if p.is_file()
    }


def test_old_full_slot_stays_fixed_and_calls_background(tmp_path):
    path = draft_file(tmp_path, fixed_raw(decoration=False))
    output = tmp_path / "review" / "reviewed.json"
    session = ReviewSession(path, output, reviewer="fixture")
    session.save(save_payload(session))
    spec = read_json(output)
    assert spec["version"] == "collage-reviewed/1"
    assert spec["layer_order"][0] == {"type": "background"}
    provider = CountingImageProvider()
    build_template(output, tmp_path / "template", image_provider=provider)
    assert provider.calls == 1


def test_explicit_switch_converts_save_and_feedback_without_changing_evidence(tmp_path):
    path = draft_file(tmp_path, fixed_raw())
    session = ReviewSession(
        path, tmp_path / "review" / "reviewed.json", reviewer="fixture"
    )
    original = path.read_bytes()
    payload = save_payload(session, {"mode": "slot", "slot_id": "cover_photo"})
    payload["draft"]["source"]["sha256"] = "tampered"
    payload["draft"]["version"] = "malicious/999"
    feedback = validate_feedback(
        session.draft, {**payload, "other_feedback": "保留装饰"}
    )
    assert feedback["draft"]["version"] == "collage-draft/2"
    assert feedback["draft"]["source"] == session.draft["source"]
    session.save(payload)
    assert path.read_bytes() == original
    reviewed = read_json(session.output_path)
    assert reviewed["version"] == "collage-build/3"
    assert reviewed["layer_order"] == raw_photo(decoration=True)["layer_order"]
    assert not (session.output_path.parent / "remove_mask.png").exists()
    decision = read_json(session.output_path.parent / "confirmation.json")[
        "background_decision"
    ]
    assert decision["source"] == "human_selection"
    assert decision["before"] == {"mode": "fixed"}
    assert decision["after"]["slot_id"] == "cover_photo"


@pytest.mark.parametrize(
    "problem",
    [
        "duplicate",
        "missing",
        "injected",
        "wrong_slot",
        "partial",
        "cutout",
        "bad_selection",
    ],
)
def test_conversion_does_not_repair_unsafe_structure(tmp_path, problem):
    current = read_json(draft_file(tmp_path, fixed_raw()))
    payload = {
        "draft": copy.deepcopy(current),
        "background_selection": {"mode": "slot", "slot_id": "cover_photo"},
    }
    edited = payload["draft"]
    if problem == "duplicate":
        edited["layer_order"].append(edited["layer_order"][-1])
    elif problem == "missing":
        edited["layer_order"].pop()
    elif problem == "injected":
        edited["layer_order"].append({"type": "slot", "id": "fake"})
    elif problem == "wrong_slot":
        payload["background_selection"]["slot_id"] = "fake"
    elif problem == "partial":
        edited["slots"][0]["target_rect"] = [0, 0, 39, 30]
    elif problem == "cutout":
        edited["slots"][0]["mode"] = "cutout"
    else:
        payload["background_selection"]["mode"] = []
    with pytest.raises(CollageError):
        edited_review_draft(current, payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"required": False},
        {"fit": "contain"},
        {"rotation_deg": 2},
        {"edge_fade_px": 2},
        {"clip_mask": "mask.png"},
    ],
)
def test_save_rejects_effective_background_overrides_before_writing(
    tmp_path, overrides
):
    path = draft_file(tmp_path, fixed_raw())
    session = ReviewSession(path, tmp_path / "reviewed.json", reviewer="fixture")
    payload = save_payload(session, {"mode": "slot", "slot_id": "cover_photo"})
    payload["slot_overrides"] = {"cover_photo": overrides}
    with pytest.raises(SpecValidationError):
        session.save(payload)
    assert not session.output_path.exists()
    assert not (path.parent / "ui_confirmed_draft.json").exists()


def test_photo_back_to_fixed_requires_mask_and_builds_original_branch(tmp_path):
    path = draft_file(tmp_path)
    session = ReviewSession(path, tmp_path / "reviewed.json", reviewer="fixture")
    payload = save_payload(session, {"mode": "fixed"})
    payload["draft"]["background"] = {
        "background_brief": "纸纹",
        "review_notes": "用户切回固定底板",
    }
    payload["draft"]["layer_order"].insert(0, {"type": "background"})
    assert (
        validate_draft(edited_review_draft(session.draft, payload))["version"]
        == "collage-draft/1"
    )
    without_mask = {**payload, "mask_png": ""}
    with pytest.raises(CollageError, match="mask"):
        session.save(without_mask)
    session.save(payload)
    provider = CountingImageProvider()
    build_template(session.output_path, tmp_path / "template", image_provider=provider)
    assert provider.calls == 1


def test_selected_source_is_sent_to_vlm_and_recorded(tmp_path):
    path = draft_file(tmp_path, fixed_raw())
    session = ReviewSession(path, tmp_path / "reviewed.json", reviewer="fixture")
    provider = FixtureVision(raw_photo(decoration=True))
    payload = save_payload(session, {"mode": "slot", "slot_id": "cover_photo"})
    revise_draft(path, {**payload, "other_feedback": "保留边框"}, provider=provider)
    request = read_json(path.parent / "review_feedback.json")
    assert request["background_decision"]["after"]["mode"] == "slot"
    assert read_json(path)["version"] == "collage-draft/2"
    assert not session.output_path.exists()


@pytest.mark.parametrize("built", [False, True])
def test_fork_preserves_confirmed_values_source_and_later_layout(tmp_path, built):
    app, source = background_project(tmp_path)
    session = app.review_session("source")
    payload = save_payload(session)
    payload["draft"]["overlays"][0]["text_content"] = "fixture text"
    payload["overlay_overrides"] = {
        "frame": {
            "text_content": "fixture text",
            "text_confirmed": True,
            "rotation_deg": 12,
        }
    }
    payload["background_expand_px"] = 2
    session.save(payload)
    if built:
        app.workflow.resume("source", open_review=False)
        layout = layout_document(source)
        for item in layout["items"]:
            if item["editable"]:
                item["rect"] = [3, 4, 14, 11]
                item["rotation_deg"] = 17
        result = fork_layout(app.store, "source", layout)
        source = app.store.open(result["project_id"])
    before = tree_hashes(source.root)
    token = app.background_revision(source.project_id)
    result = app.fork_background(source.project_id, token)
    target = app.store.open(result["project_id"])
    assert tree_hashes(source.root) == before
    assert app.workflow.status(target.project_id)["stage"] == "awaiting_review"
    assert not (target.review / "reviewed.json").exists()
    assert not (target.template / "template.json").exists()
    restored = app.review_session(target.project_id)
    assert restored.draft["overlays"][0]["text_content"] == "fixture text"
    assert restored.review_options["overlays"]["frame"]["text_confirmed"] is True
    assert restored.review_options["overlays"]["frame"]["rotation_deg"] == (
        17 if built else 12
    )
    assert restored.background_expand_px == 2
    if built:
        assert restored.draft["overlays"][0]["target_rect"] == [3, 4, 14, 11]
    restored.save(save_payload(restored, {"mode": "slot", "slot_id": "cover_photo"}))
    app.workflow.resume(target.project_id, open_review=False)
    assert (
        read_json(target.workspace / "state.json")["nodes"]["background"]["status"]
        == "skipped"
    )
    assert tree_hashes(source.root) == before


def test_fork_refuses_stale_and_busy_projects(tmp_path):
    app, source = background_project(tmp_path)
    session = app.review_session("source")
    session.save(save_payload(session))
    token = app.background_revision("source")
    edited = read_json(source.review / "reviewed.json")
    edited["background"]["background_brief"] += " changed"
    atomic_write_json(source.review / "reviewed.json", edited)
    with pytest.raises(CollageError) as caught:
        app.fork_background("source", token)
    assert caught.value.code == "REVIEW_REVISION_CONFLICT"
    held = threading.Event()
    done = threading.Event()
    app.jobs.submit("source", "test", lambda: (held.set(), done.wait(5)))
    held.wait(2)
    try:
        with pytest.raises(CollageError) as caught:
            app.fork_background("source", app.background_revision("source"))
        assert caught.value.code == "PROJECT_BUSY"
    finally:
        done.set()
    assert len(app.store.list()) == 1


def test_photo_background_still_generates_and_reuses_validated_overlay_cache(tmp_path):
    # Keep synthetic project paths below Windows MAX_PATH despite pytest's long
    # generated test-directory names; production data roots are independent.
    tmp_path = tmp_path.parent / "cache-case"
    tmp_path.mkdir()
    app, source = background_project(tmp_path)
    session = app.review_session("source")
    payload = save_payload(session)
    overlay = payload["draft"]["overlays"][0]
    overlay.update(
        action="reference_generate", shape=None, generation_brief="fixture ellipse"
    )
    session.save(payload)
    reviewed = read_json(source.review / "reviewed.json")
    Image.new("RGB", (40, 30), "white").save(source.review / "candidate.png")
    reviewed["background"]["candidate_path"] = "candidate.png"
    atomic_write_json(source.review / "reviewed.json", reviewed)
    provider = ChromaOnlyOverlayProvider()
    build_template(
        source.review / "reviewed.json",
        source.template,
        work_dir=source.workspace,
        image_provider=provider,
    )
    assert provider.calls == 1
    result = app.fork_background("source", app.background_revision("source"))
    target = app.store.open(result["project_id"])
    restored = app.review_session(target.project_id)
    restored.save(save_payload(restored, {"mode": "slot", "slot_id": "cover_photo"}))
    build_template(
        target.review / "reviewed.json",
        target.template,
        work_dir=target.workspace,
        image_provider=provider,
    )
    assert provider.calls == 1
    report = read_json(target.template / "template.json")
    assert report["build"]["providers"][0]["cache_hit"] is True
    assert all(item["node"] != "background" for item in report["build"]["providers"])
    spec = read_json(target.review / "reviewed.json")
    # A photo background does not remove the need for an ornament provider.
    with pytest.raises(CollageError) as caught:
        WorkflowStages._image_provider(
            {"options": {"fixture_provider": False, "image_provider": None}}, spec
        )
    assert caught.value.code == "IMAGE_PROVIDER_UNAVAILABLE"

    # Changing a semantic cache input must not reuse the copied success.
    spec["overlays"][0]["generation_brief"] += " revised"
    atomic_write_json(target.review / "reviewed.json", spec)
    build_template(
        target.review / "reviewed.json",
        target.template,
        work_dir=target.workspace,
        image_provider=provider,
        force=True,
    )
    assert provider.calls == 2

    # Corrupt cache evidence must not mask an uncertain original request, nor
    # should a background-only fork reset that request's no-replay boundary.
    cache_path = next((source.workspace / "cache" / "overlay").glob("*.json"))
    metadata = read_json(cache_path)
    metadata["image_fingerprint"] = "invalid"
    atomic_write_json(cache_path, metadata)
    record_path = next((source.workspace / "overlay_attempts").glob("*/0.json"))
    record = read_json(record_path)
    record["status"] = "pending"
    atomic_write_json(record_path, record)
    result = app.fork_background("source", app.background_revision("source"))
    pending_target = app.store.open(result["project_id"])
    pending_review = app.review_session(pending_target.project_id)
    pending_review.save(
        save_payload(pending_review, {"mode": "slot", "slot_id": "cover_photo"})
    )
    with pytest.raises(CollageError) as caught:
        build_template(
            pending_target.review / "reviewed.json",
            pending_target.template,
            work_dir=pending_target.workspace,
            image_provider=provider,
        )
    assert caught.value.code == "OVERLAY_REQUEST_UNCERTAIN"
    assert provider.calls == 2
    assert (
        read_json(pending_target.reports / "background_revision.json")[
            "copied_overlay_cache"
        ]
        == 0
    )


def test_backend_converts_photo_to_fixed_with_explicit_brief(tmp_path):
    current = read_json(draft_file(tmp_path))
    edited = edited_review_draft(
        current,
        {
            "background_selection": {"mode": "fixed"},
            "fixed_background": {
                "background_brief": "纸张底板",
                "review_notes": "用户要求",
            },
        },
    )
    assert edited["version"] == "collage-draft/1"
    assert edited["layer_order"][0] == {"type": "background"}
    assert current["background"]["mode"] == "slot"


@pytest.mark.parametrize("phase", ["copy", "transition"])
def test_incomplete_fork_is_not_resumable(tmp_path, monkeypatch, phase):
    app, source = background_project(tmp_path)
    session = app.review_session("source")
    session.save(save_payload(session))
    original = tree_hashes(source.root)

    def fail(*args, **kwargs):
        raise OSError("synthetic copy failure")

    monkeypatch.setattr(
        "collage.studio.workbench.background_revision."
        + ("_copy_resource" if phase == "copy" else "transition"),
        fail,
    )
    with pytest.raises(CollageError) as caught:
        app.fork_background("source", app.background_revision("source"))
    assert caught.value.code == "BACKGROUND_REVISION_INCOMPLETE"
    target = app.store.open(caught.value.details["project_id"])
    assert app.store.get_manifest(target.project_id)["status"] == "failed"
    with pytest.raises(CollageError) as caught:
        app.workflow.resume(target.project_id, open_review=False)
    assert caught.value.code == "BACKGROUND_REVISION_INCOMPLETE"
    assert tree_hashes(source.root) == original


def test_fork_imports_fixed_masks_prepared_alpha_and_customer_binding(tmp_path):
    app, source = background_project(tmp_path)
    session = app.review_session("source")
    payload = save_payload(session)
    payload["draft"]["overlays"][0].update(action="reference_generate", shape=None)
    artwork = Image.new("RGBA", (15, 10), (0, 0, 0, 0))
    artwork.putpixel((7, 5), (20, 30, 40, 128))
    artwork.save(source.review / "prepared.png")
    payload["overlay_overrides"] = {"frame": {"prepared_asset": "prepared.png"}}
    session.save(payload)
    expected_mask = sha256_file(source.review / "remove_mask.png")
    Image.new("RGB", (80, 60), "blue").save(source.inputs / "customer.png")
    atomic_write_json(
        source.renders / "bindings.json",
        {
            "version": "collage-bindings/1",
            "slots": {"cover_photo": {"path": "../inputs/customer.png"}},
        },
    )
    result = app.fork_background("source", app.background_revision("source"))
    target = app.store.open(result["project_id"])
    restored = app.review_session(target.project_id)
    assert sha256_file(target.inputs / "initial_remove_mask.png") == expected_mask
    relative = restored.review_options["overlays"]["frame"]["prepared_asset"]
    assert relative.startswith("resources/")
    assert sha256_file(target.review / relative) == sha256_file(
        source.review / "prepared.png"
    )
    restored.save(save_payload(restored, {"mode": "slot", "slot_id": "cover_photo"}))
    app.workflow.resume(target.project_id, open_review=False)
    assert app.workflow.status(target.project_id)["stage"] == "awaiting_approval"
    with Image.open(target.renders / "result.png") as rendered:
        assert rendered.convert("RGBA").getpixel((0, 0)) == (0, 0, 255, 255)


@pytest.mark.parametrize("mode", ["cutout", "photo_feather"])
def test_foreground_cutout_or_feather_does_not_disqualify_photo_base(tmp_path, mode):
    from collage.rendering.model import PreparedBinding
    from collage.rendering.service import render_template

    raw = raw_photo()
    foreground = copy.deepcopy(raw["slots"][0])
    foreground.update(
        id="foreground",
        mode=mode,
        source_rect=[5, 5, 12, 10],
        target_rect=[5, 5, 12, 10],
    )
    raw["slots"].append(foreground)
    raw["layer_order"].append({"type": "slot", "id": "foreground"})
    path = draft_file(tmp_path, raw)
    session = ReviewSession(path, tmp_path / "reviewed.json", reviewer="fixture")
    session.save(save_payload(session))
    build_template(session.output_path, tmp_path / "template")
    rendered = render_template(
        tmp_path / "template",
        {
            "cover_photo": PreparedBinding(Image.new("RGB", (40, 30), "blue")),
            "foreground": PreparedBinding(
                Image.new("RGBA", (12, 10), (255, 0, 0, 160))
            ),
        },
        require_ready=False,
    )
    assert rendered.getpixel((0, 0)) == (0, 0, 255, 255)
