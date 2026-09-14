"""Exercise conditional shape fields, prompt consistency, and offline workflow output."""

from __future__ import annotations

import copy
import json

import pytest
from PIL import Image

from collage.core.errors import CollageError
from collage.core.io import read_json
from collage.projects import DataPaths, ProjectStore
from collage.providers import ProviderAudit
from collage.schemas import validate_draft
from collage.schemas.common import _enum, _number
from collage.schemas.shapes import (
    SHAPE_EXAMPLES,
    SHAPE_FIELDS,
    SHAPE_PROMPT,
    compile_shape,
    validate_shape,
)
from collage.template.analysis import ANALYSIS_PROMPT
from collage.template.build.overlays import _make_basic_shape
from collage.template.review import confirm_draft
from collage.template.review.feedback import revise_draft, review_revision
from collage.workflows import WorkflowService


def raw_draft(shape):
    return {
        "slots": [],
        "overlays": [
            {
                "id": "frame",
                "label": "Frame",
                "source_rect": [2, 2, 28, 24],
                "target_rect": [2, 2, 28, 24],
                "attachment": None,
                "action": "basic_shape",
                "generation_brief": "Local shape fixture",
                "requires_exact_content": False,
                "review_notes": "",
                "shape": copy.deepcopy(shape),
            }
        ],
        "background": {"background_brief": "Gray", "review_notes": ""},
        "layer_order": [{"type": "background"}, {"type": "overlay", "id": "frame"}],
        "questions": [],
    }


class VisionFixture:
    name = "shape-fields-fixture"
    requested_model = "fixture"
    fixture = True

    def __init__(self, shape):
        self.raw = raw_draft(shape)
        self.calls = []

    def analyze(self, reference_bytes, **kwargs):
        self.calls.append(kwargs)
        return copy.deepcopy(self.raw), ProviderAudit(
            self.name,
            "fixture",
            "fixture",
            "shape-fixture",
            True,
            0,
        )


@pytest.mark.parametrize("shape", SHAPE_EXAMPLES, ids=lambda shape: shape["kind"])
def test_prompt_examples_compile_without_changing_visual_fields(shape):
    before = copy.deepcopy(shape)
    validate_draft(raw_draft(shape), require_metadata=False)
    compiled = compile_shape(shape)
    assert shape == before
    assert set(compiled) == SHAPE_FIELDS
    issues = []
    validate_shape(compiled, "$.shape", issues, executable=True)
    assert not issues
    if shape["kind"] == "rounded_rectangle":
        assert compiled["radius"] == 8.5
    assert _make_basic_shape(raw_draft(compiled)["overlays"][0]).getbbox()
    assert json.dumps(shape, ensure_ascii=False) in SHAPE_PROMPT
    assert SHAPE_PROMPT in ANALYSIS_PROMPT


@pytest.mark.parametrize("kind", ["rectangle", "ellipse", "dashed_rectangle"])
def test_unused_radius_is_not_a_required_visual_parameter(kind):
    shape = {"kind": kind, "outline": "#FFFFFF", "width": 2}
    if kind == "dashed_rectangle":
        shape.update(dash=3, gap=2)
    for unused in (None, "unused", 999):
        shape["radius"] = unused
        validate_draft(raw_draft(shape), require_metadata=False)
        assert compile_shape(shape)["radius"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("radius", None),
        ("radius", True),
        ("radius", "8.5"),
        ("radius", -1),
        ("radius", 8193),
        ("radius", float("nan")),
        ("radius", float("inf")),
        ("radius", 10**400),
        ("width", 2.5),
        ("width", "2"),
        ("width", True),
        ("kind", []),
        ("kind", {}),
        ("outline", "#GGGGGG"),
    ],
)
def test_invalid_visual_values_have_stable_field_errors(field, value):
    shape = dict(SHAPE_EXAMPLES[2])
    shape[field] = value
    with pytest.raises(CollageError) as caught:
        validate_draft(raw_draft(shape), require_metadata=False)
    assert caught.value.code == "SPEC_VALIDATION_FAILED"
    assert any(
        issue["path"] == f"$.overlays[0].shape.{field}"
        for issue in caught.value.details["issues"]
    )


@pytest.mark.parametrize(
    "kind,field",
    [
        ("rounded_rectangle", "radius"),
        ("dashed_rectangle", "dash"),
        ("dashed_rectangle", "gap"),
    ],
)
def test_missing_active_parameter_is_reported_once(kind, field):
    shape = copy.deepcopy(next(item for item in SHAPE_EXAMPLES if item["kind"] == kind))
    shape.pop(field)
    with pytest.raises(CollageError) as caught:
        validate_draft(raw_draft(shape), require_metadata=False)
    issues = [
        item
        for item in caught.value.details["issues"]
        if item["path"] == f"$.overlays[0].shape.{field}"
    ]
    assert [item["code"] for item in issues] == ["MISSING_FIELD"]


def test_discrete_numeric_values_are_converted_only_when_exact():
    shape = {
        "kind": "dashed_rectangle",
        "outline": "#FFFFFF",
        "width": 2.0,
        "dash": 3.0,
        "gap": 2.0,
    }
    compiled = compile_shape(shape)
    for field in ("width", "dash", "gap"):
        assert type(compiled[field]) is int
        assert type(shape[field]) is float
    issues = []
    validate_shape({**compiled, "width": 2.0}, "$.shape", issues, executable=True)
    assert issues
    for field in ("dash", "gap"):
        with pytest.raises(CollageError):
            compile_shape({**shape, field: 2.5})


@pytest.mark.parametrize(
    "kind", ["rectangle", "rounded_rectangle", "ellipse", "dashed_rectangle"]
)
def test_old_complete_shapes_keep_identical_pixels(kind):
    old = {
        "kind": kind,
        "fill": None,
        "outline": "#FFFFFF",
        "width": 2,
        "radius": 8,
        "dash": 3,
        "gap": 2,
    }
    before = _make_basic_shape(raw_draft(old)["overlays"][0])
    after = _make_basic_shape(raw_draft(compile_shape(old))["overlays"][0])
    assert before.tobytes() == after.tobytes()


def test_generated_overlay_shape_is_rejected_before_review():
    raw = raw_draft(SHAPE_EXAMPLES[0])
    raw["overlays"][0]["action"] = "reference_generate"
    with pytest.raises(CollageError) as caught:
        validate_draft(raw, require_metadata=False)
    assert any(
        item["path"] == "$.overlays[0].shape" for item in caught.value.details["issues"]
    )


def test_malformed_enum_and_overflowing_number_do_not_crash_validator():
    issues = []
    _enum([], {"photo"}, "$.mode", issues)
    _number(10**400, "$.radius", issues, minimum=0, maximum=8192)
    assert [item.path for item in issues] == ["$.mode", "$.radius"]


@pytest.mark.parametrize("shape", SHAPE_EXAMPLES, ids=lambda shape: shape["kind"])
def test_workflow_compiles_model_shape_and_renders_locally(
    tmp_path, monkeypatch, shape
):
    reference = tmp_path / "reference.png"
    background = tmp_path / "background.png"
    Image.new("RGB", (32, 30), "#333333").save(reference)
    Image.new("RGB", (32, 30), "#333333").save(background)
    provider = VisionFixture(shape)

    def load(spec, protocol):
        assert spec == "fixture:vision", "Unexpected model load during local build"
        return provider

    monkeypatch.setattr("collage.workflows.stages.load_provider", load)
    store = ProjectStore(DataPaths.resolve(tmp_path / "data"))
    workflow = WorkflowService(store)
    started = workflow.start(
        "shape-flow",
        reference,
        reviewer="fixture-tester",
        vision_provider_spec="fixture:vision",
        background_candidate_path=background,
        open_review=False,
    )
    assert started["stage"] == "awaiting_review"
    project = store.open("shape-flow")
    draft_path = project.analysis / "draft.json"
    assert read_json(draft_path)["overlays"][0]["shape"] == shape
    assert len(provider.calls) == 1
    assert SHAPE_PROMPT in provider.calls[0]["prompt"]

    # Exercise the same shape contract during feedback; this is a fixture call.
    revise_draft(
        draft_path,
        {
            "revision": review_revision(read_json(draft_path)),
            "other_feedback": "Check the frame placement.",
        },
        provider=provider,
    )
    assert len(provider.calls) == 2
    assert SHAPE_PROMPT in provider.calls[1]["prompt"]
    mask = project.review / "mask.png"
    Image.new("L", (32, 30), 255).save(mask)
    confirm_draft(
        draft_path,
        project.review / "reviewed.json",
        remove_mask_path=mask,
        reviewer="fixture-tester",
        background_candidate_path=project.inputs / "background_candidate.png",
        background_expand_px=0,
        background_feather_px=0,
    )
    reviewed = read_json(project.review / "reviewed.json")
    compiled = reviewed["overlays"][0]["shape"]
    assert compiled == compile_shape(shape)
    preview = workflow.resume("shape-flow", open_review=False)
    assert preview["stage"] == "awaiting_approval"
    manifest = read_json(project.template / "template.json")
    assert manifest["status"] == "needs_review"
    assert manifest["review"]["visual_approved"] is False
    assert any(
        item["name"] == "local-basic-shape" for item in manifest["build"]["providers"]
    )
    asset = next(item for item in manifest["assets"] if item["id"] == "frame")
    with Image.open(project.template / asset["path"]) as actual:
        expected = _make_basic_shape(reviewed["overlays"][0])
        assert actual.convert("RGBA").tobytes() == expected.tobytes()
    assert read_json(project.renders / "result.png.render.json")["network_calls"] == 0
