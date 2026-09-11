"""Exercise paid-call replay boundaries, independent edge refinement and local crop coverage."""

import pytest
from PIL import Image, ImageDraw

from collage.core.errors import CollageError
from collage.core.io import read_json
from collage.devtools.trial_support import TrialVision
from collage.providers.base import ProviderAudit
from collage.providers.yibu.inspection import YibuInspectionProvider, parse_inspection
from collage.providers.yibu.settings import YibuSettings
from collage.template.auto_geometry import (
    normalized_rect,
    refine_window,
    isolate_light_strokes,
    validate_structure,
)
from collage.template.auto_composition import fit_visible_head


@pytest.mark.parametrize(
    "rect",
    [
        [-0.1, 0, 0.5, 1],
        [0, 0, 0, 1],
        [0.9, 0, 0.2, 1],
        [True, 0, 1, 1],
        [0, float("nan"), 1, 1],
    ],
)
def test_invalid_model_geometry_is_rejected(rect):
    with pytest.raises(CollageError, match="模型"):
        normalized_rect(rect, (200, 300))


def test_window_refinement_uses_real_line_pixels():
    im = Image.new("RGB", (240, 300), "#556677")
    ImageDraw.Draw(im).rectangle((30, 60, 190, 230), outline="white", width=3)
    rect, evidence = refine_window(
        im,
        [32, 58, 161, 173],
        {"style": "rectangle", "width_fraction": 0.01},
        main=False,
    )
    assert all(e["pixel_evidence"] for e in evidence["edges"])
    assert abs(rect[0] - 32) <= 2 and abs(rect[1] - 62) <= 2
    blank = Image.new("RGB", im.size, "#556677")
    _, absent = refine_window(
        blank,
        [32, 58, 161, 173],
        {"style": "rectangle", "width_fraction": 0.01},
        main=False,
    )
    assert not any(e["pixel_evidence"] for e in absent["edges"])


def test_stroke_isolation_does_not_copy_photographed_rgb():
    im = Image.new("RGB", (100, 70), "#607080")
    draw = ImageDraw.Draw(im)
    draw.line((20, 10, 40, 55), fill="white", width=3)
    draw.rectangle((65, 20, 85, 45), fill="#D03522")
    asset, evidence = isolate_light_strokes(im, [0, 0, 100, 70])
    assert evidence["source_rgb_copied"] is False
    assert asset.getchannel("A").getbbox() is not None
    assert asset.convert("RGB").getextrema() == ((255, 255), (255, 255), (255, 255))
    assert asset.getpixel((75, 30))[3] == 0


def test_crop_preserves_cover_and_improves_head_visibility():
    source = Image.new("RGB", (200, 400), "white")
    slot = {"rect": [0, 0, 100, 100], "anchor": [0.5, 0.5]}
    focus = {"head_box": [0.35, 0.05, 0.3, 0.15]}
    binding, decision = fit_visible_head(
        source, slot, focus, Image.new("L", (100, 100), 255)
    )
    assert decision["cover_complete"] is True
    assert decision["head_visible_fraction"] == 1
    assert binding.offset_px[1] > 0
    assert decision["production_threshold_calibrated"] is False


def test_json_contract_and_default_model_parameters(monkeypatch):
    settings = YibuSettings(
        api_key="test-key", vlm_model="kimi-k3", vlm_reasoning_effort="high"
    )
    provider = YibuInspectionProvider(settings)
    captured = {}

    def post(path, payload, **kwargs):
        captured.update(payload)
        return {
            "choices": [
                {
                    "message": {"content": '{"status":"uncertain"}'},
                    "finish_reason": "stop",
                }
            ],
            "model": "kimi-k3",
            "usage": {"total_tokens": 12},
            "id": "request-fixture",
        }, {}

    monkeypatch.setattr(provider._client, "post_json", post)
    result, audit, usage = provider.inspect(
        [("test", Image.new("RGB", (2, 2)))],
        prompt="inspect",
        contract="json",
        operation="test",
    )
    assert result["status"] == "uncertain" and usage["total_tokens"] == 12
    assert captured["model"] == "kimi-k3" and captured["reasoning_effort"] == "high"
    assert audit.request_id == "request-fixture"
    assert parse_inspection('```json\n{"ok":true}\n```') == {"ok": True}
    with pytest.raises(CollageError):
        parse_inspection("[]")


def test_unknown_paid_request_is_not_replayed(tmp_path, monkeypatch):
    vision = TrialVision(tmp_path, YibuSettings(api_key="test-key"))
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise CollageError("TEMPORARY_NETWORK_ERROR", "uncertain")

    monkeypatch.setattr(vision.provider, "inspect", fail)
    image = Image.new("RGB", (2, 2))
    with pytest.raises(CollageError):
        vision.inspect("node", [("test", image)], "prompt", "contract")
    recorded = read_json(tmp_path / "requests/node.json")
    assert recorded["status"] == "failed_or_uncertain"
    assert recorded["request_parameters"]["model"] == vision.settings.vlm_model
    assert recorded["request_parameters"]["image_count"] == 1
    assert "test-key" not in (tmp_path / "requests/node.json").read_text()
    assert recorded["finished_at"] and recorded["elapsed_ms"] >= 0
    with pytest.raises(CollageError) as error:
        vision.inspect("node", [("test", image)], "prompt", "contract")
    assert error.value.code == "AUTO_REQUEST_NOT_REPLAYED" and len(calls) == 1


def test_cached_call_binds_prompt_image_and_model(tmp_path, monkeypatch):
    vision = TrialVision(tmp_path, YibuSettings(api_key="test-key"))
    calls = []

    def inspect(*args, **kwargs):
        calls.append(1)
        return {"ok": True}, ProviderAudit("fixture", "test", None, None, True, 1), {}

    monkeypatch.setattr(vision.provider, "inspect", inspect)
    images = [("test", Image.new("RGB", (2, 2)))]
    assert vision.inspect("node", images, "prompt", "contract") == {"ok": True}
    assert vision.inspect("node", images, "prompt", "contract") == {"ok": True}
    with pytest.raises(CollageError) as error:
        vision.inspect("node", images, "changed prompt", "contract")
    assert error.value.code == "AUTO_NODE_INPUT_CHANGED" and len(calls) == 1


def test_unimplemented_layout_is_not_silently_simplified():
    with pytest.raises(CollageError) as error:
        validate_structure({"family": "fixed_board"})
    assert error.value.code == "AUTO_LAYOUT_NOT_IMPLEMENTED"


@pytest.mark.parametrize("diagnostic_only", [False, True])
def test_real_compiler_builds_v2_from_synthetic_model_evidence(
    tmp_path, diagnostic_only
):
    from collage.core.io import atomic_save_image, atomic_write_json
    from collage.template.automatic import compile_template
    from collage.template.probes import probe_template

    source = Image.new("RGB", (240, 300), "#607080")
    ImageDraw.Draw(source).rectangle((30, 60, 190, 230), outline="white", width=3)
    atomic_save_image(source, tmp_path / "reference.png")
    for node in ["structure", "geometry_review"]:
        atomic_write_json(
            tmp_path / "requests" / (node + ".json"), {"synthetic_unit_test": True}
        )
    structure = {
        "family": "full_canvas_photo_with_insets",
        "slots": [
            {
                "id": "main",
                "label": "main",
                "role": "main",
                "mode": "photo",
                "rect": [0, 0, 1, 1],
                "rotation_deg": 0,
                "frame": {"style": "none", "color": "#FFFFFF"},
            },
            {
                "id": "inset",
                "label": "inset",
                "role": "inset",
                "mode": "photo",
                "rect": [0.125, 0.2, 161 / 240, 171 / 300],
                "rotation_deg": 0,
                "frame": {
                    "style": "dashed_rectangle",
                    "color": "#FFFFFF",
                    "width_fraction": 0.01,
                    "dash_fraction": 0.02,
                    "gap_fraction": 0.02,
                },
            },
        ],
        "foreground": [],
        "layer_order": ["main", "inset"],
        "unresolved": [],
    }
    if diagnostic_only:
        structure["unresolved"] = [
            "This synthetic boundary is intentionally unresolved"
        ]
    template = compile_template(tmp_path, structure, diagnostic_only=diagnostic_only)
    assert template["status"] == "needs_validation"
    assert template["provenance"]["kind"] == (
        "diagnostic" if diagnostic_only else "automatic"
    )
    assert template["provenance"]["unresolved"] == structure["unresolved"]
    if diagnostic_only:
        from collage.template.validation import validate_package

        assert (
            template["provenance"]["continuation"]["instruction_source"]
            == "explicit_user_instruction"
        )
        with pytest.raises(CollageError):
            validate_package(tmp_path / "template", require_ready=True)
    assert template["review"]["visual_approved"] is False
    probe_template(tmp_path / "template", tmp_path / "probes")
    quality = read_json(tmp_path / "probes/quality.json")
    assert quality["status"] == "unverified"
    assert quality["production_acceptance"] is False
    assert all(slot["visible_pixels"] > 0 for slot in quality["slots"])


@pytest.mark.parametrize("status,expected_calls", [(504, 2), (None, 1)])
def test_geometry_retries_known_http_failure_once(tmp_path, status, expected_calls):
    from collage.template.automatic import _inspect_geometry

    calls = []

    class Vision:
        def inspect(self, name, images, prompt, contract):
            calls.append(name)
            if name == "geometry_review":
                raise CollageError(
                    "TEMPORARY_NETWORK_ERROR",
                    "network failure",
                    details={"http_status": status},
                )
            return {"corrected": True}

    image = Image.new("RGB", (30, 40))
    if status:
        assert _inspect_geometry(
            tmp_path, Vision(), image, [("source", image)], "prompt", "contract"
        ) == {"corrected": True}
    else:
        with pytest.raises(CollageError):
            _inspect_geometry(
                tmp_path, Vision(), image, [("source", image)], "prompt", "contract"
            )
    assert len(calls) == expected_calls


def test_private_path_log_filter_preserves_http_address():
    import logging
    from collage.devtools.trial_support import PrivatePathFilter

    record = logging.LogRecord(
        "trial",
        logging.INFO,
        "",
        0,
        "endpoint=%s output=%s",
        ("http://127.0.0.1:17860", r"D:\private\source.png"),
        None,
    )
    assert PrivatePathFilter().filter(record)
    assert record.getMessage() == "endpoint=http://127.0.0.1:17860 output=<local-file>"


def test_unresolved_structure_gets_only_one_semantic_repair(tmp_path):
    from collage.core.io import atomic_save_image
    from collage.template.automatic import compile_template, refine_structure

    atomic_save_image(
        Image.new("RGB", (120, 160), "#607080"), tmp_path / "reference.png"
    )
    structure = {
        "family": "full_canvas_photo_with_insets",
        "slots": [
            {
                "id": "main",
                "label": "main",
                "role": "main",
                "mode": "photo",
                "rect": [0, 0, 1, 1],
                "rotation_deg": 0,
                "frame": {"style": "none"},
            }
        ],
        "foreground": [],
        "layer_order": ["main"],
        "unresolved": ["Essential visual boundary remains ambiguous"],
    }
    calls = []

    class Vision:
        def inspect(self, name, images, prompt, contract):
            calls.append(name)
            if name == "structure_resolution":
                assert len(images) == 1
                assert images[0][1].size == (120, 160)
            return structure

    result = refine_structure(tmp_path, structure, Vision())
    assert calls == ["geometry_review", "structure_resolution"]
    assert result["unresolved"]
    assert read_json(tmp_path / "refined_structure.json") == structure
    assert read_json(tmp_path / "repaired_structure.json") == structure
    with pytest.raises(CollageError) as error:
        compile_template(tmp_path, result)
    assert error.value.code == "AUTO_STRUCTURE_UNRESOLVED"


def test_diagnostic_source_requires_explicit_continuation_record():
    from collage.schemas.provenance import validate_provenance

    issues = []
    validate_provenance(
        {
            "kind": "diagnostic",
            "policy_version": "test/1",
            "evidence_sha256": ["a" * 64],
            "unresolved": ["Known ambiguity"],
        },
        "$.provenance",
        issues,
    )
    assert any("continuation" in issue.path for issue in issues)


@pytest.mark.parametrize("modified", [False, True])
def test_continue_reuses_bound_model_result_only(tmp_path, modified):
    from collage.core.io import atomic_write_json
    from collage.devtools.real_trial import recorded_structure

    structure = {"slots": [], "unresolved": ["Preserved question"]}
    atomic_write_json(tmp_path / "repaired_structure.json", structure)
    evidence = dict(structure)
    if modified:
        evidence["unresolved"] = []
    atomic_write_json(
        tmp_path / "requests/structure_resolution.json",
        {"status": "complete", "result": evidence},
    )
    if modified:
        with pytest.raises(CollageError) as error:
            recorded_structure(tmp_path)
        assert error.value.code == "AUTO_STRUCTURE_EVIDENCE_MISMATCH"
    else:
        assert recorded_structure(tmp_path) == structure
