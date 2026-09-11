"""Exercise customer-photo backgrounds and offline recovery using synthetic fixtures only."""

from __future__ import annotations

import copy

import pytest
from PIL import Image

from collage.core.errors import CollageError, SpecValidationError
from collage.core.io import atomic_write_json, read_json
from collage.providers import ProviderAudit
from collage.rendering.model import PreparedBinding
from collage.rendering.service import render_template
from collage.schemas import validate_build_spec, validate_draft, validate_template_spec
from collage.studio.review_session import ReviewSession
from collage.studio.workbench.layout import layer_items
from collage.template.analysis import analyze_reference
from collage.template.build import build_template
from collage.template.guide import create_upload_guide
from collage.template.review import confirm_draft
from collage.template.review.feedback import revise_draft, review_revision
from collage.template.review.recovery import (
    available_recoveries,
    recover_saved_correction,
)
from collage.template.validation import validate_package
from collage.workflows.stages import WorkflowStages


class FixtureVision:
    """Return explicit synthetic model data without a network client."""

    name = "fixture-photo-background"
    requested_model = "fixture"
    fixture = True

    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    def analyze(self, reference_bytes, **kwargs):
        self.calls += 1
        return copy.deepcopy(self.raw), ProviderAudit(
            self.name, "fixture", "fixture", "fixture-request", True, 1
        )


def raw_photo(*, decoration=False):
    raw = {
        "slots": [
            {
                "id": "cover_photo",
                "label": "背景照片",
                "type": "image",
                "mode": "photo",
                "source_rect": [0, 0, 40, 30],
                "target_rect": [0, 0, 40, 30],
                "upload_hint": "上传背景照片",
                "review_notes": "",
            }
        ],
        "overlays": [],
        "background": {
            "mode": "slot",
            "slot_id": "cover_photo",
            "review_notes": "无需清版",
        },
        "layer_order": [{"type": "slot", "id": "cover_photo"}],
        "questions": [],
    }
    if decoration:
        raw["overlays"].append(
            {
                "id": "frame",
                "label": "边框",
                "source_rect": [5, 5, 15, 10],
                "target_rect": [5, 5, 15, 10],
                "action": "basic_shape",
                "generation_brief": "",
                "requires_exact_content": False,
                "review_notes": "",
                "shape": {
                    "kind": "rectangle",
                    "fill": None,
                    "outline": "#FF0000",
                    "width": 2,
                    "radius": 0,
                    "dash": 1,
                    "gap": 0,
                },
            }
        )
        raw["layer_order"].append({"type": "overlay", "id": "frame"})
    return raw


def draft_file(root, raw=None):
    root.mkdir(parents=True, exist_ok=True)
    reference = root / "source.png"
    Image.new("RGB", (40, 30), "orange").save(reference)
    return analyze_reference(
        reference, root / "analysis", provider=FixtureVision(raw or raw_photo())
    )


def make_package(root, *, decoration=False):
    draft_path = draft_file(root, raw_photo(decoration=decoration))
    reviewed = root / "review" / "reviewed.json"
    confirm_draft(draft_path, reviewed, reviewer="fixture-tester")
    manifest = build_template(reviewed, root / "template", work_dir=root / "work")
    return manifest.parent, reviewed


def test_photo_background_skips_generation_and_preserves_decoration(
    tmp_path, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Background/provider must never run for a photo base")

    monkeypatch.setattr("collage.template.build.service._build_background", forbidden)
    monkeypatch.setattr("collage.workflows.stages.load_provider", forbidden)
    package, reviewed = make_package(tmp_path, decoration=True)
    spec = read_json(reviewed)
    assert WorkflowStages._image_provider({"options": {}}, spec) is None
    template = validate_package(package, require_ready=False)
    assert template["version"] == "collage-template/3"
    assert template["status"] == "needs_review"
    assert template["build"]["fixture_used"] is True
    assert len(template["assets"]) == 1
    assert all(item["role"] != "background" for item in template["assets"])
    assert not (package / "assets" / "background.png").exists()
    assert not (tmp_path / "review" / "remove_mask.png").exists()
    for name in (
        "background.png",
        "background_candidate.png",
        "remove_mask.png",
        "blend_mask.png",
    ):
        assert not (tmp_path / "work" / name).exists()
    state = read_json(tmp_path / "work" / "state.json")
    assert state["nodes"]["background"]["status"] == "skipped"
    assert state["nodes"]["background"]["code"] == "BACKGROUND_PROVIDED_BY_SLOT"
    assert all(item["node"] != "background" for item in template["build"]["providers"])
    for color in ("green", "blue"):
        binding = {"cover_photo": PreparedBinding(Image.new("RGB", (80, 60), color))}
        image = render_template(package, binding, require_ready=False)
        assert image.getpixel((0, 0)) == Image.new("RGBA", (1, 1), color).getpixel(
            (0, 0)
        )
        assert image.getpixel((5, 5)) == (255, 0, 0, 255)
        assert (
            image.tobytes()
            == render_template(package, binding, require_ready=False).tobytes()
        )
    create_upload_guide(
        package,
        tmp_path / "guide.html",
        tmp_path / "bindings.json",
        require_ready=False,
    )
    assert "全屏背景必填" in (tmp_path / "guide.html").read_text(encoding="utf-8")
    assert "background_candidate.png" not in (
        tmp_path / "work" / "inspection.html"
    ).read_text(encoding="utf-8")
    assert layer_items(template)[0]["background"] is True


def test_review_session_needs_no_mask_or_empty_mask_approval(tmp_path):
    path = draft_file(tmp_path)
    output = tmp_path / "review" / "reviewed.json"
    session = ReviewSession(path, output, reviewer="fixture-tester")
    assert session.review_options["initial_mask_source"] == "not_required"
    session.save(
        {
            "draft": session.draft,
            "revision": review_revision(session.draft),
            "final_confirmed": True,
        }
    )
    assert read_json(output)["version"] == "collage-build/3"
    assert not (output.parent / "remove_mask.png").exists()
    assert read_json(output.parent / "confirmation.json")["final_confirmed"] is True


@pytest.mark.parametrize(
    "change",
    [
        {"target_rect": [1, 0, 39, 30]},
        {"mode": "cutout"},
        {"mode": "photo_feather"},
    ],
)
def test_invalid_photo_background_geometry_or_mode_rejected(tmp_path, change):
    raw = raw_photo()
    raw["slots"][0].update(change)
    with pytest.raises(SpecValidationError):
        draft_file(tmp_path, raw)
    assert not (tmp_path / "analysis" / "draft.json").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"required": False},
        {"fit": "contain"},
        {"rotation_deg": 1},
        {"clip_mask": "hole.png"},
        {"edge_fade_px": 2},
        {"target_rect": [0, 0, 39, 30]},
    ],
)
def test_build_rejects_background_overrides_that_could_expose_base(tmp_path, change):
    path = draft_file(tmp_path)
    output = tmp_path / "reviewed.json"
    with pytest.raises(SpecValidationError):
        confirm_draft(
            path,
            output,
            reviewer="fixture-tester",
            slot_overrides_data={"cover_photo": change},
        )
    assert not output.exists()


def test_versioned_rules_leave_legacy_validation_strict(tmp_path):
    draft = read_json(draft_file(tmp_path))
    draft["version"] = "collage-draft/1"
    draft["background"] = {"background_brief": "", "review_notes": ""}
    with pytest.raises(SpecValidationError) as error:
        validate_draft(draft)
    assert any(issue.code == "INVALID_LAYER_ORDER" for issue in error.value.issues)
    draft["layer_order"].insert(0, {"type": "background"})
    validate_draft(draft)


@pytest.mark.parametrize("problem", ["alpha", "offset", "missing"])
def test_background_binding_must_actually_cover_canvas(tmp_path, problem):
    package, _ = make_package(tmp_path)
    source = Image.new("RGBA", (40, 30), "green")
    if problem == "alpha":
        source.putpixel((2, 2), (0, 0, 0, 254))
    prepared = {
        "cover_photo": PreparedBinding(
            source, offset_px=(1, 0) if problem == "offset" else (0, 0)
        )
    }
    if problem == "missing":
        prepared = {}
    with pytest.raises(CollageError) as error:
        render_template(package, prepared, require_ready=False)
    assert error.value.code == (
        "MISSING_BINDING" if problem == "missing" else "BACKGROUND_SLOT_COVERAGE_FAILED"
    )


def test_alpha_outside_the_visible_crop_does_not_fail_background(tmp_path):
    package, _ = make_package(tmp_path)
    source = Image.new("RGBA", (80, 30), (0, 0, 0, 0))
    source.paste(Image.new("RGBA", (40, 30), "green"), (20, 0))
    image = render_template(
        package, {"cover_photo": PreparedBinding(source)}, require_ready=False
    )
    assert image.getchannel("A").getextrema() == (255, 255)


def failed_legacy_correction(root):
    before = raw_photo()
    before["background"] = {"background_brief": "旧背景", "review_notes": ""}
    before["layer_order"].insert(0, {"type": "background"})
    before["questions"] = ["背景是否替换？"]
    path = draft_file(root, before)
    raw = raw_photo()
    raw["background"] = {"background_brief": "旧模型要求清版预览", "review_notes": ""}
    provider = FixtureVision(raw)
    current = read_json(path)
    payload = {
        "revision": review_revision(current),
        "question_resolutions": [
            {"question": current["questions"][0], "answer": "改成全屏客户照片"},
        ],
    }
    with pytest.raises(SpecValidationError):
        revise_draft(path, payload, provider=provider)
    request_path = next((path.parent / "feedback").glob("*/request.json"))
    return path, request_path, provider


def test_saved_legacy_response_recovers_without_model_or_approval(tmp_path):
    path, request_path, provider = failed_legacy_correction(tmp_path)
    raw_path = request_path.parent / "response.json"
    saved_raw = raw_path.read_bytes()
    saved_before = (request_path.parent / "before.json").read_bytes()
    options = available_recoveries(path)
    assert options[0]["background_slot_id"] == "cover_photo"
    result = recover_saved_correction(
        path, request_path.parent.name, background_slot="cover_photo"
    )
    assert result["network_calls"] == 0 and provider.calls == 1
    assert raw_path.read_bytes() == saved_raw
    assert (request_path.parent / "before.json").read_bytes() == saved_before
    draft = validate_draft(read_json(path))
    assert draft["version"] == "collage-draft/2"
    assert draft["questions"] == []
    assert draft["background"] == {
        "mode": "slot",
        "slot_id": "cover_photo",
        "review_notes": "背景由客户上传的全屏照片提供，无需制作固定背景或清版旧照片。",
    }
    assert draft["provider"] == read_json(raw_path)["audit"]
    assert draft["prompt_version"] == read_json(request_path)["version"]
    assert read_json(request_path)["recovery"]["network_calls"] == 0
    assert (path.parent / "draft_preview.png").is_file()
    assert not (path.parent / "reviewed.json").exists()
    assert not available_recoveries(path)
    assert (
        recover_saved_correction(path, request_path.parent.name)["revision"]
        == result["revision"]
    )


@pytest.mark.parametrize(
    "problem",
    ["stale", "reference", "confirmed", "wrong_slot", "missing_response", "path"],
)
def test_recovery_refuses_stale_or_unsafe_results(tmp_path, problem):
    path, request_path, provider = failed_legacy_correction(tmp_path)
    request_id = request_path.parent.name
    reviewed = tmp_path / "review" / "reviewed.json"
    slot = "cover_photo"
    if problem == "stale":
        current = read_json(path)
        current["questions"].append("新增问题")
        atomic_write_json(path, current)
    elif problem == "reference":
        Image.new("RGB", (40, 30), "purple").save(path.parent / "reference.png")
    elif problem == "confirmed":
        atomic_write_json(reviewed, {"already": "confirmed"})
    elif problem == "wrong_slot":
        slot = "unknown"
    elif problem == "missing_response":
        (request_path.parent / "response.json").unlink()
    else:
        request_id = "../" + request_id
    before = path.read_bytes()
    with pytest.raises(CollageError):
        recover_saved_correction(
            path, request_id, background_slot=slot, reviewed_path=reviewed
        )
    assert path.read_bytes() == before
    assert provider.calls == 1


def test_model_declared_photo_background_correction_is_accepted(tmp_path):
    legacy = raw_photo()
    legacy["background"] = {"background_brief": "", "review_notes": ""}
    legacy["layer_order"].insert(0, {"type": "background"})
    path = draft_file(tmp_path, legacy)
    provider = FixtureVision(raw_photo())
    revise_draft(
        path,
        {
            "revision": review_revision(read_json(path)),
            "other_feedback": "背景改为客户照片",
        },
        provider=provider,
    )
    assert validate_draft(read_json(path))["version"] == "collage-draft/2"
    assert provider.calls == 1
