"""Reproduce validation-contract mismatches with synthetic inputs and no model calls."""

from __future__ import annotations

import json
import logging
import platform
import tempfile
from pathlib import Path

from PIL import Image, __version__ as pillow_version

from collage.core.errors import CollageError
from collage.imaging.operations import edge_fade_mask, make_blend_mask, rect_to_box
from collage.providers import ProviderAudit
from collage.schemas import validate_bindings, validate_draft
from collage.schemas.reviewed import _reviewed_slot
from collage.template.analysis import analyze_reference
from collage.core.io import atomic_write_json
from collage.template.review.service import confirm_draft
from collage.template.build.overlays import _make_basic_shape
from collage.template.review.service import _reviewed_overlay


def raw_draft():
    return {
        "slots": [],
        "overlays": [{
            "id": "frame", "label": "Frame",
            "source_rect": [1, 1, 18, 18], "target_rect": [1, 1, 18, 18],
            "action": "basic_shape", "generation_brief": "White frame",
            "requires_exact_content": False, "review_notes": "",
            "shape": {
                "kind": "rounded_rectangle", "fill": None, "outline": "#FFFFFF",
                "width": 2, "radius": 8, "dash": 3, "gap": 2,
            },
        }],
        "background": {"background_brief": "Gray", "review_notes": ""},
        "layer_order": [{"type": "background"}, {"type": "overlay", "id": "frame"}],
        "questions": [],
    }


def result(operation):
    try:
        value = operation()
        details = {"outcome": "accepted"}
        if isinstance(value, Image.Image):
            details["image_size"] = list(value.size)
        return details
    except CollageError as error:
        return {
            "outcome": "business_error", "code": error.code,
            "issues": error.details.get("issues", []),
        }
    except Exception as error:
        return {"outcome": "unexpected_exception", "type": type(error).__name__}


def inspect_shape(name, **changes):
    raw = raw_draft()
    raw["overlays"][0]["shape"].update(changes)
    return {
        "case": name,
        "schema": result(lambda: validate_draft(raw, require_metadata=False)),
        "draw": result(lambda: _make_basic_shape(raw["overlays"][0])),
    }


class RejectedFixture:
    name = "validation-contract-fixture"
    requested_model = "fixture"
    fixture = True

    def __init__(self):
        self.calls = 0

    def analyze(self, reference_bytes, **kwargs):
        self.calls += 1
        raw = raw_draft()
        raw["overlays"][0]["shape"]["radius"] = None
        return raw, ProviderAudit(self.name, "fixture", "fixture", "fixture", True, 0)


def reviewed_photo(**changes):
    slot = {
        "id": "photo", "label": "Photo", "type": "image", "required": True,
        "mode": "photo_feather", "source_rect": [0, 0, 20, 20],
        "target_rect": [0, 0, 20, 20], "upload_hint": "", "review_notes": "",
        "rotation_deg": 0, "fit": "cover", "anchor": [0.5, 0.5],
        "clip_mask": None, "edge_fade_px": 2,
    }
    slot.update(changes)
    return slot


def inspect_reviewed_slot(slot):
    issues = []
    _reviewed_slot(slot, "$.slots[0]", issues, (20, 20))
    return {"issues": [issue.as_dict() for issue in issues]}


def run():
    logging.disable(logging.CRITICAL)
    cases = [
        inspect_shape("rounded_radius_8_5", radius=8.5),
        inspect_shape("rounded_radius_8_0", radius=8.0),
        inspect_shape("rounded_radius_null", radius=None),
        inspect_shape("rectangle_radius_null", kind="rectangle", radius=None),
        inspect_shape("rectangle_unused_dash_gap_null", kind="rectangle", dash=None, gap=None),
        inspect_shape("rounded_width_2_0", width=2.0),
        inspect_shape("dashed_gap_2_0", kind="dashed_rectangle", gap=2.0),
        inspect_shape("invalid_color", outline="#GGGGGG"),
    ]
    raw = raw_draft()
    raw["overlays"][0]["shape"]["kind"] = "rectangle"
    for field in ("radius", "dash", "gap"):
        raw["overlays"][0]["shape"].pop(field)
    cases.append({
        "case": "rectangle_omits_unused_fields",
        "schema": result(lambda: validate_draft(raw, require_metadata=False)),
        "draw": result(lambda: _make_basic_shape(raw["overlays"][0])),
    })
    raw = raw_draft()
    raw["overlays"][0]["shape"]["kind"] = []
    cases.append({"case": "shape_kind_array", "schema": result(
        lambda: validate_draft(raw, require_metadata=False))})
    raw = raw_draft()
    raw["overlays"][0]["target_rect"][0] = 10 ** 400
    cases.append({"case": "huge_integer_coordinate", "schema": result(
        lambda: validate_draft(raw, require_metadata=False))})
    raw = raw_draft()
    raw["overlays"][0]["target_rect"] = [1, 1, 0.1, 18]
    cases.append({
        "case": "subpixel_rect_collapses",
        "schema": result(lambda: validate_draft(raw, require_metadata=False)),
        "raster_box": result(lambda: rect_to_box(raw["overlays"][0]["target_rect"])),
    })
    raw = raw_draft()
    raw["overlays"][0]["target_rect"] = [1, 1, 1000000, 1000000]
    cases.append({
        "case": "unbounded_target_size_no_allocation_attempted",
        "schema": result(lambda: validate_draft(raw, require_metadata=False)),
    })
    raw = raw_draft()
    raw["overlays"][0].pop("shape")
    cases.append({
        "case": "basic_shape_without_shape",
        "schema": result(lambda: validate_draft(raw, require_metadata=False)),
        "reviewed_action": _reviewed_overlay(raw["overlays"][0], {})["action"],
    })
    raw = raw_draft()
    raw["overlays"][0]["text_content"] = ""
    cases.append({"case": "no_text_as_empty_string", "schema": result(
        lambda: validate_draft(raw, require_metadata=False))})
    raw = raw_draft()
    raw["overlays"][0]["action"] = "reference_generate"
    cases.append({
        "case": "generated_overlay_with_valid_shape",
        "schema": result(lambda: validate_draft(raw, require_metadata=False)),
        "reviewed_shape_retained": _reviewed_overlay(raw["overlays"][0], {})["shape"] is not None,
    })
    cases.append({
        "case": "feather_width_2_5",
        "reviewed_schema": inspect_reviewed_slot(reviewed_photo(edge_fade_px=2.5)),
        "mask": result(lambda: edge_fade_mask((20, 20), 2.5)),
    })
    cases.append({
        "case": "background_feather_2_5_runtime",
        "mask": result(lambda: make_blend_mask(
            Image.new("L", (20, 20)), feather_px=2.5)),
    })
    cases.append({
        "case": "background_expand_2_5_runtime",
        "mask": result(lambda: make_blend_mask(
            Image.new("L", (20, 20)), expand_px=2.5)),
    })
    template = {"slots": [{"id": "photo", "type": "image", "required": True, "fit": "cover"}]}
    bindings = {"version": "collage-bindings/1", "slots": {"photo": {"path": "fixture.png", "scale": 0.5}}}
    from collage.rendering.layout import _fit_to_rect
    cases.append({
        "case": "cover_scale_under_one",
        "schema": result(lambda: validate_bindings(bindings, template)),
        "fit": result(lambda: _fit_to_rect(
            Image.new("RGB", (20, 20)), (20, 20), fit="cover",
            anchor=(0.5, 0.5), scale_adjustment=0.5)),
    })
    # New synthetic data only; no original project or customer file is read.
    scratch = Path(tempfile.mkdtemp(prefix="figcopy-validation-audit-")).resolve()
    assert scratch.parent == Path(tempfile.gettempdir()).resolve()
    reference = scratch / "reference.png"
    Image.new("RGB", (20, 20), "gray").save(reference)
    raw = raw_draft()
    raw["overlays"][0]["action"] = "reference_generate"
    manual = scratch / "manual.json"
    atomic_write_json(manual, raw)
    def analyze_and_confirm():
        draft_path = analyze_reference(
            reference, scratch / "action-analysis", manual_draft_path=manual)
        mask = scratch / "mask.png"
        Image.new("L", (20, 20), 255).save(mask)
        return confirm_draft(
            draft_path, scratch / "reviewed.json", remove_mask_path=mask,
            reviewer="fixture-tester")

    cases.append({
        "case": "generated_shape_analysis_and_confirmation",
        "pipeline": result(analyze_and_confirm),
    })
    provider = RejectedFixture()
    outcomes = []
    for _ in range(2):
        outcomes.append(result(lambda: analyze_reference(
            reference, scratch / "analysis", provider=provider)))
    cases.append({
        "case": "invalid_received_response_retry",
        "attempts": outcomes, "fixture_provider_calls": provider.calls,
        "persisted_files": sorted(p.relative_to(scratch / "analysis").as_posix()
                                  for p in (scratch / "analysis").rglob("*") if p.is_file()),
    })
    return {
        "evidence_type": "synthetic_local_probe",
        "real_model_calls": 0,
        "python": platform.python_version(),
        "pillow": pillow_version,
        "case_count": len(cases),
        "cases": cases,
    }


if __name__ == "__main__":
    report = run()
    target = Path(__file__).with_name("results-current.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
