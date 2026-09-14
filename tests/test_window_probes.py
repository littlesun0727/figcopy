"""Exercise A1 window geometry, source provenance, and failed quality gates."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from collage.cli.app import main
from collage.core.errors import CollageError, SpecValidationError
from collage.core.io import atomic_save_image, atomic_write_json, read_json, sha256_file
from collage.devtools.window_demo import create_window_demo
from collage.rendering.model import PreparedBinding
from collage.rendering.service import render_template
from collage.schemas import validate_build_spec, validate_template_spec
from collage.template.probes import probe_template
from collage.template.validation import validate_package


@pytest.fixture
def window_case(tmp_path: Path) -> Path:
    root = tmp_path / "windows"
    create_window_demo(root, run_probe=False)
    return root


def test_four_windows_match_design_with_translucent_occlusion(
    window_case: Path, monkeypatch
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("window probes must not use the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    report_path = probe_template(
        window_case / "template",
        window_case / "probe",
        expectations_path=window_case / "evaluation" / "expectations.json",
    )
    report = read_json(report_path)
    assert report["status"] == "passed"
    assert report["production_acceptance"] is False
    assert report["probe_fixture_used"] and report["template_fixture_used"]
    assert report["network_calls"] == 0
    assert len(report["slots"]) == 4
    opaque = Image.open(window_case / "probe/visibility/photo_left.png")
    translucent = Image.open(window_case / "probe/visibility/photo_right.png")
    assert opaque.getpixel((80, 52)) == 0
    assert translucent.getpixel((340, 48)) == 127
    assert opaque.getpixel((50, 90)) == 255
    assert (
        Image.open(window_case / "probe/visibility/photo_bottom.png").getpixel(
            (251, 223)
        )
        == 0
    )


def test_intruding_foreground_is_rejected_even_with_valid_asset_hash(
    window_case: Path,
) -> None:
    root = window_case / "template"
    spec = read_json(root / "template.json")
    asset = next(item for item in spec["assets"] if item["id"] == "tape")
    image = Image.open(root / asset["path"]).convert("RGBA")
    ImageDraw.Draw(image).rectangle((60, 90, 110, 120), fill="white")
    atomic_save_image(image, root / asset["path"])
    asset["sha256"] = sha256_file(root / asset["path"])
    atomic_write_json(root / "template.json", spec)
    validate_package(root, require_ready=False)  # 文件有效不等于照片窗口正确。
    report = read_json(
        probe_template(
            root,
            window_case / "bad",
            expectations_path=window_case / "evaluation" / "expectations.json",
        )
    )
    left = next(item for item in report["slots"] if item["slot_id"] == "photo_left")
    assert report["status"] == "failed"
    assert left["code"] == "PROBE_WINDOW_MISMATCH"
    assert left["missing_pixels"] == 51 * 31


def test_wrong_foreground_order_and_missing_expectations(window_case: Path) -> None:
    root = window_case / "template"
    spec = read_json(root / "template.json")
    tape = spec["layer_order"].pop()
    spec["layer_order"].insert(1, tape)
    atomic_write_json(root / "template.json", spec)
    report = read_json(
        probe_template(
            root,
            window_case / "wrong-order",
            expectations_path=window_case / "evaluation" / "expectations.json",
        )
    )
    assert report["status"] == "failed"
    assert any(item.get("unexpected_pixels", 0) > 0 for item in report["slots"])
    diagnostic = read_json(probe_template(root, window_case / "diagnostic"))
    assert diagnostic["status"] == "unverified"
    assert diagnostic["production_acceptance"] is False


def test_v2_masks_are_bound_to_content_and_empty_windows_fail(
    window_case: Path,
) -> None:
    root = window_case / "template"
    spec = read_json(root / "template.json")
    slot = spec["slots"][1]
    atomic_save_image(Image.new("L", (140, 104), 0), root / slot["clip_mask"])
    with pytest.raises(SpecValidationError) as caught:
        validate_package(root, require_ready=False)
    codes = {issue.code for issue in caught.value.issues}
    assert {"MASK_HASH_MISMATCH", "EMPTY_SLOT_MASK"} <= codes


def test_provenance_never_impersonates_human_approval(window_case: Path) -> None:
    spec = read_json(window_case / "build.json")
    assert "review" not in validate_build_spec(spec)
    template = validate_package(window_case / "template", require_ready=False)
    assert template["version"] == "collage-template/4"
    assert template["status"] == "needs_validation"
    assert template["review"]["reviewer"] is None
    assert template["review"]["visual_approved"] is False
    # 把 provenance 名称改成 automatic 仍不构成验收，更不能手填 ready。
    template["provenance"]["kind"] = "automatic"
    template["status"] = "ready"
    template["review"].update(
        reviewer="pretend",
        reviewed_at="2026-09-11T00:00:00Z",
        visual_approved=True,
        evidence_sha256=["a" * 64],
    )
    with pytest.raises(SpecValidationError) as caught:
        validate_template_spec(template)
    assert "AUTO_ACCEPTANCE_UNAVAILABLE" in {
        issue.code for issue in caught.value.issues
    }


@pytest.mark.parametrize(
    "mutation", ["unresolved", "bad_hash", "fake_reviewer", "preserve_missing"]
)
def test_invalid_automatic_decisions_cannot_build(
    window_case: Path, mutation: str
) -> None:
    spec = read_json(window_case / "build.json")
    spec["provenance"]["kind"] = "automatic"
    if mutation == "unresolved":
        spec["provenance"]["unresolved"] = ["which nested photo is replaceable"]
    elif mutation == "bad_hash":
        spec["provenance"]["evidence_sha256"] = ["not-a-hash"]
    elif mutation == "fake_reviewer":
        spec["review"] = {"reviewer": "auto"}
    else:
        spec["overlays"][0]["prepared_asset"] = None
    with pytest.raises(SpecValidationError):
        validate_build_spec(spec)


def test_fixed_template_repeated_render_is_identical(window_case: Path) -> None:
    template = validate_package(window_case / "template", require_ready=False)
    before = {
        asset["path"]: sha256_file(window_case / "template" / asset["path"])
        for asset in template["assets"]
    }
    prepared = {
        slot["id"]: PreparedBinding(image=Image.new("RGBA", (71, 95), "#5599AA"))
        for slot in template["slots"]
    }
    first = render_template(window_case / "template", prepared, require_ready=False)
    second = render_template(window_case / "template", prepared, require_ready=False)
    assert first.tobytes() == second.tobytes()
    assert before == {
        name: sha256_file(window_case / "template" / name) for name in before
    }


def test_probe_output_cannot_modify_template(window_case: Path) -> None:
    root = window_case / "template"
    with pytest.raises(CollageError) as caught:
        probe_template(root, root / "probes")
    assert caught.value.code == "PROBE_OUTPUT_CONFLICT"


def test_cli_probe_failure_has_nonzero_exit_and_report(
    window_case: Path, capsys, monkeypatch
) -> None:
    # 此测试验证 CLI 退出码，日志 handler 由 pytest 管理，不能绑定即将关闭的捕获流。
    monkeypatch.setattr("collage.cli.app.configure_logging", lambda verbose: None)
    root = window_case / "template"
    spec = read_json(root / "template.json")
    # 将 main_photo 放在所有小窗口之后，模拟层序错误导致小窗口全部消失。
    main_slot = spec["layer_order"].pop(1)
    spec["layer_order"].append(main_slot)
    atomic_write_json(root / "template.json", spec)
    code = main(
        ["probe", "--template", str(root), "--out", str(window_case / "cli-bad")]
    )
    assert code == 2
    assert "PROBE_CHECK_FAILED" in capsys.readouterr().err
    report = read_json(window_case / "cli-bad" / "quality.json")
    assert report["status"] == "failed"
    assert any(item["code"] == "PROBE_SLOT_HIDDEN" for item in report["slots"])


def test_expectations_require_every_slot_and_evaluation_label(
    window_case: Path,
) -> None:
    path = window_case / "evaluation" / "expectations.json"
    record = read_json(path)
    record["purpose"] = "automatic_production_input"
    atomic_write_json(path, record)
    with pytest.raises(CollageError) as caught:
        probe_template(
            window_case / "template",
            window_case / "bad-purpose",
            expectations_path=path,
        )
    assert caught.value.code == "PROBE_EXPECTATIONS_INVALID"
    assert not (window_case / "bad-purpose").exists()
