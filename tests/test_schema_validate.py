"""验证严格 schema、安全相对路径和透明素材门禁。"""

from __future__ import annotations

from pathlib import Path

import pytest

from collage.errors import SpecValidationError
from collage.io_utils import atomic_write_json, read_json
from collage.schema import (
    draft_has_release_blockers,
    validate_bindings,
    validate_draft,
    validate_template_spec,
)
from collage.validate import validate_package


def test_unknown_mode_and_question_are_release_blockers() -> None:
    payload = {
        "slots": [
            {
                "id": "subject",
                "label": "主体",
                "type": "image",
                "mode": "unknown",
                "source_rect": [0, 0, 10, 10],
                "target_rect": [0, 0, 10, 10],
                "upload_hint": "上传图片",
                "review_notes": "需确认",
            }
        ],
        "overlays": [],
        "background": {"background_brief": "", "review_notes": ""},
        "layer_order": [{"type": "background"}, {"type": "slot", "id": "subject"}],
        "questions": ["这是照片还是抠图？"],
    }
    draft = validate_draft(payload, require_metadata=False)
    blockers = draft_has_release_blockers(draft)
    assert len(blockers) == 2


def test_unknown_schema_field_is_rejected() -> None:
    with pytest.raises(SpecValidationError) as caught:
        validate_draft(
            {
                "slots": [],
                "overlays": [],
                "background": {"background_brief": "", "review_notes": ""},
                "layer_order": [{"type": "background"}],
                "questions": [],
                "confidence": 0.99,
            },
            require_metadata=False,
        )
    assert any(issue.code == "UNKNOWN_FIELD" for issue in caught.value.issues)


def test_package_rejects_path_traversal(asset_package_factory, tmp_path: Path) -> None:
    package = asset_package_factory(tmp_path)
    spec = read_json(package / "template.json")
    spec["assets"][1]["path"] = "../outside.png"
    atomic_write_json(package / "template.json", spec)
    with pytest.raises(SpecValidationError) as caught:
        validate_package(package, require_ready=False)
    assert any(issue.code == "UNSAFE_PACKAGE_PATH" for issue in caught.value.issues)


@pytest.mark.parametrize(
    "unsafe_path", ["C:/secret.png", "assets/file.png:stream", "assets/CON.png"]
)
def test_package_rejects_windows_unsafe_paths(
    asset_package_factory, tmp_path: Path, unsafe_path: str
) -> None:
    package = asset_package_factory(tmp_path)
    spec = read_json(package / "template.json")
    spec["assets"][1]["path"] = unsafe_path
    atomic_write_json(package / "template.json", spec)
    with pytest.raises(SpecValidationError) as caught:
        validate_package(package, require_ready=False)
    assert any(issue.code == "UNSAFE_PACKAGE_PATH" for issue in caught.value.issues)


def test_opaque_overlay_cannot_claim_alpha(
    asset_package_factory, tmp_path: Path
) -> None:
    package = asset_package_factory(tmp_path)
    spec = read_json(package / "template.json")
    spec["assets"][1]["requires_alpha"] = True
    atomic_write_json(package / "template.json", spec)
    with pytest.raises(SpecValidationError) as caught:
        validate_package(package, require_ready=False)
    assert any(issue.code == "OPAQUE_OVERLAY" for issue in caught.value.issues)


def test_ready_requires_visual_evidence(asset_package_factory, tmp_path: Path) -> None:
    package = asset_package_factory(tmp_path)
    spec = read_json(package / "template.json")
    spec["status"] = "ready"
    with pytest.raises(SpecValidationError):
        validate_template_spec(spec)


def test_missing_or_duplicate_layer_reference_is_rejected(
    asset_package_factory, tmp_path: Path
) -> None:
    package = asset_package_factory(tmp_path)
    spec = read_json(package / "template.json")
    spec["layers"][-1] = spec["layers"][-2]
    with pytest.raises(SpecValidationError) as caught:
        validate_template_spec(spec, require_ready=False)
    codes = {issue.code for issue in caught.value.issues}
    assert "DUPLICATE_LAYER_REFERENCE" in codes
    assert "MISSING_LAYER_REFERENCE" in codes


def test_corrupt_asset_is_rejected(asset_package_factory, tmp_path: Path) -> None:
    package = asset_package_factory(tmp_path)
    (package / "assets" / "red.png").write_bytes(b"not an image")
    with pytest.raises(SpecValidationError) as caught:
        validate_package(package, require_ready=False)
    assert any(issue.code == "IMAGE_DECODE_FAILED" for issue in caught.value.issues)


def test_confirmed_default_text_does_not_require_customer_binding() -> None:
    template = {
        "slots": [
            {
                "id": "caption",
                "type": "text",
                "required": True,
                "default_text": "已确认标题",
            }
        ]
    }
    bindings = validate_bindings(
        {"version": "collage-bindings/1", "slots": {}}, template
    )
    assert bindings["slots"] == {}
