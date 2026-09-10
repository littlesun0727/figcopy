"""验证真实图像输入、人工 Draft 导入、保护确认稿和审核阻塞规则。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from collage.analyze import analyze_reference
from collage.errors import CollageError
from collage.io_utils import atomic_save_image, atomic_write_json, read_json
from collage.providers import ProviderAudit
from collage.review import confirm_draft, infer_default_text, suggested_edge_fade_px


def _draft_payload(mode: str = "photo", questions: list[str] | None = None) -> dict:
    return {
        "slots": [
            {
                "id": "photo",
                "label": "照片",
                "type": "image",
                "mode": mode,
                "source_rect": [1, 1, 8, 8],
                "target_rect": [1, 1, 8, 8],
                "upload_hint": "上传照片",
                "review_notes": "",
            }
        ],
        "overlays": [],
        "background": {"background_brief": "延续底色", "review_notes": ""},
        "layer_order": [{"type": "background"}, {"type": "slot", "id": "photo"}],
        "questions": questions or [],
    }


class RecordingVisionProvider:
    """仅用于确认 analyze 传入了真实 PNG 字节而非本地路径字符串。"""

    name = "recording-vlm"
    requested_model = "test-model"
    fixture = True

    def __init__(self) -> None:
        self.received: bytes | None = None
        self.calls = 0

    def analyze(self, reference_bytes: bytes, **kwargs):
        self.calls += 1
        self.received = reference_bytes
        return _draft_payload(), ProviderAudit(
            self.name, self.requested_model, "test-model", "req-1", True, 1
        )


def test_analyze_passes_actual_png_and_adds_owned_metadata(tmp_path: Path) -> None:
    reference = tmp_path / "参考图.jpg"
    Image.new("RGB", (12, 10), "orange").save(reference)
    provider = RecordingVisionProvider()
    draft_path = analyze_reference(reference, tmp_path / "work", provider=provider)
    assert provider.received is not None and provider.received.startswith(
        b"\x89PNG\r\n\x1a\n"
    )
    draft = read_json(draft_path)
    assert draft["canvas"]["width"] == 12
    assert draft["source"]["path"] == "reference.png"
    assert draft["provider"]["fixture"] is True
    analyze_reference(reference, tmp_path / "work", provider=provider, force=True)
    assert provider.calls == 1
    assert read_json(draft_path)["provider"]["cache_hit"] is True


def test_manual_draft_can_be_confirmed_with_mask(tmp_path: Path) -> None:
    reference = tmp_path / "reference.png"
    manual = tmp_path / "manual.json"
    mask = tmp_path / "删除.png"
    atomic_save_image(Image.new("RGB", (12, 10), "orange"), reference)
    atomic_save_image(Image.new("L", (12, 10), 0), mask)
    atomic_write_json(manual, _draft_payload())
    draft_path = analyze_reference(
        reference, tmp_path / "work", manual_draft_path=manual
    )
    reviewed_path = confirm_draft(
        draft_path,
        tmp_path / "work" / "reviewed.json",
        remove_mask_path=mask,
        reviewer="tester",
    )
    reviewed = read_json(reviewed_path)
    assert reviewed["status"] == "reviewed"
    assert reviewed["slots"][0]["fit"] == "cover"


def test_photo_feather_receives_size_based_default(tmp_path: Path) -> None:
    reference = tmp_path / "reference.png"
    manual = tmp_path / "manual.json"
    mask = tmp_path / "mask.png"
    atomic_save_image(Image.new("RGB", (12, 10), "orange"), reference)
    atomic_save_image(Image.new("L", (12, 10), 255), mask)
    atomic_write_json(manual, _draft_payload("photo_feather"))
    draft_path = analyze_reference(
        reference, tmp_path / "work", manual_draft_path=manual
    )
    reviewed_path = confirm_draft(
        draft_path,
        tmp_path / "work" / "reviewed.json",
        remove_mask_path=mask,
        reviewer="tester",
    )
    reviewed = read_json(reviewed_path)
    assert reviewed["slots"][0]["edge_fade_px"] == 1


def test_edge_fade_suggestion_scales_and_has_an_upper_bound() -> None:
    assert suggested_edge_fade_px([0, 0, 100, 200]) == 8
    assert suggested_edge_fade_px([0, 0, 800, 1000]) == 64
    assert suggested_edge_fade_px([0, 0, 4000, 5000]) == 128


def test_default_text_is_reused_from_existing_vlm_label() -> None:
    """只复用 Draft 已识别文字，不进行第二次识图或自由猜测。"""

    assert infer_default_text({"label": "文字「together.」"}) == "together."
    assert infer_default_text({"label": "普通标题"}) is None
    assert (
        infer_default_text({"label": "文字「旧值」", "default_text": "已确认值"})
        == "已确认值"
    )


def test_inline_ui_decisions_are_applied_to_reviewed_spec(tmp_path: Path) -> None:
    reference = tmp_path / "reference.png"
    manual = tmp_path / "manual.json"
    mask = tmp_path / "mask.png"
    payload = _draft_payload("photo_feather")
    payload["slots"].append(
        {
            "id": "caption",
            "label": "标题",
            "type": "text",
            "mode": None,
            "source_rect": [0, 0, 4, 2],
            "target_rect": [0, 0, 8, 2],
            "upload_hint": "输入标题",
            "review_notes": "测试文字槽",
            "default_text": "hello",
        }
    )
    payload["overlays"].append(
        {
            "id": "ticket",
            "label": "精确票券",
            "source_rect": [0, 0, 4, 2],
            "target_rect": [0, 0, 4, 2],
            "action": "reference_generate",
            "generation_brief": "票券",
            "requires_exact_content": True,
            "review_notes": "测试精确装饰",
        }
    )
    payload["layer_order"] = [
        {"type": "background"},
        {"type": "slot", "id": "photo"},
        {"type": "slot", "id": "caption"},
        {"type": "overlay", "id": "ticket"},
    ]
    atomic_save_image(Image.new("RGB", (12, 10), "orange"), reference)
    atomic_save_image(Image.new("L", (12, 10), 255), mask)
    atomic_write_json(manual, payload)
    draft_path = analyze_reference(
        reference, tmp_path / "work", manual_draft_path=manual
    )
    reviewed_path = confirm_draft(
        draft_path,
        tmp_path / "work" / "reviewed.json",
        remove_mask_path=mask,
        reviewer="tester",
        slot_overrides_data={"caption": {"fallback_approved": True}},
        overlay_overrides_data={"ticket": {"requires_exact_content": False}},
    )
    reviewed = read_json(reviewed_path)
    assert reviewed["slots"][0]["edge_fade_px"] == 1
    assert reviewed["slots"][1]["fallback_approved"] is True
    assert reviewed["overlays"][0]["requires_exact_content"] is False


def test_unknown_and_questions_block_confirmation(tmp_path: Path) -> None:
    reference = tmp_path / "reference.png"
    manual = tmp_path / "manual.json"
    mask = tmp_path / "mask.png"
    atomic_save_image(Image.new("RGB", (12, 10), "orange"), reference)
    atomic_save_image(Image.new("L", (12, 10), 0), mask)
    atomic_write_json(manual, _draft_payload("unknown", ["需要哪种模式？"]))
    draft_path = analyze_reference(
        reference, tmp_path / "work", manual_draft_path=manual
    )
    with pytest.raises(CollageError) as caught:
        confirm_draft(
            draft_path,
            tmp_path / "reviewed.json",
            remove_mask_path=mask,
            reviewer="tester",
        )
    assert caught.value.code == "HUMAN_REVIEW_REQUIRED"


def test_existing_reviewed_file_blocks_new_analysis(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "reviewed.json").write_text("{}", encoding="utf-8")
    reference = tmp_path / "reference.png"
    manual = tmp_path / "manual.json"
    atomic_save_image(Image.new("RGB", (12, 10), "orange"), reference)
    atomic_write_json(manual, _draft_payload())
    with pytest.raises(CollageError) as caught:
        analyze_reference(reference, work, manual_draft_path=manual)
    assert caught.value.code == "REVIEWED_SPEC_PROTECTED"
