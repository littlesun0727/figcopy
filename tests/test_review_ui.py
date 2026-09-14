"""验证简化确认页的自动默认值、必要门禁和保存链路。"""

from __future__ import annotations

import base64
import io
import json
import socket
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

from collage.core.errors import CollageError
from collage.core.io import atomic_write_json, read_json
from collage.studio.review_server import (
    HTML,
    REVIEW_CSS,
    REVIEW_JS,
    serve_review_ui,
)
from collage.template.analysis import analyze_reference
from collage.template.review import (
    automatic_remove_mask,
    automatic_review_notes,
    build_review_options,
    question_resolution_notes,
    validate_review_decisions,
)


def _complex_draft() -> dict:
    return {
        "canvas": {"width": 1000, "height": 1600},
        "slots": [
            {
                "id": "photo",
                "label": "羽化照片",
                "type": "image",
                "mode": "photo_feather",
                "target_rect": [0, 0, 800, 1000],
            },
            {
                "id": "caption",
                "label": "文字",
                "type": "text",
                "mode": None,
                "target_rect": [20, 1200, 600, 100],
                "default_text": "hello",
            },
        ],
        "overlays": [
            {
                "id": "ticket",
                "action": "reference_generate",
                "requires_exact_content": True,
            }
        ],
    }


def test_review_options_supply_dynamic_visual_defaults() -> None:
    options = build_review_options(
        _complex_draft(),
        None,
        None,
        background_expand_px=6,
        background_feather_px=12,
    )
    assert options["slots"]["photo"]["edge_fade_px"] == 64
    assert options["slots"]["caption"]["font_size"] == 55
    assert options["slots"]["caption"]["fallback_approved"] is True
    assert options["overlays"]["ticket"]["requires_exact_content"] is True
    assert options["background"] == {
        "expand_px": 6,
        "feather_px": 12,
        "composition_mode": "protected",
    }


def test_remove_mask_is_automatically_painted_from_source_rects() -> None:
    draft = {
        "canvas": {"width": 100, "height": 80},
        "slots": [{"source_rect": [10, 10, 20, 10]}],
        "overlays": [{"source_rect": [50, 40, 10, 10]}],
    }
    mask = automatic_remove_mask(draft)
    assert mask.getbbox() == (8, 8, 62, 52)
    assert mask.getpixel((10, 10)) == 255
    assert mask.getpixel((50, 40)) == 255
    assert mask.getpixel((40, 30)) == 0


def test_review_decisions_accept_automatic_defaults_but_keep_advanced_gate() -> None:
    draft = _complex_draft()
    options = build_review_options(
        draft,
        None,
        None,
        background_expand_px=0,
        background_feather_px=0,
    )
    # 精确内容不能再被默认降级；只有明确选择近似制作才可继续。
    with pytest.raises(CollageError) as caught:
        validate_review_decisions(draft, options["slots"], options["overlays"])
    assert caught.value.code == "HUMAN_REVIEW_REQUIRED"
    assert len(caught.value.details["blockers"]) == 1

    options["overlays"]["ticket"]["requires_exact_content"] = False
    validate_review_decisions(draft, options["slots"], options["overlays"])


def test_question_answers_are_recorded_in_review_notes() -> None:
    questions = ["层序是否正确？", "是否接受近似票券？"]
    notes = question_resolution_notes(
        questions,
        [
            {"question": questions[0], "answer": "当前层序正确"},
            {"question": questions[1], "answer": "接受近似生成"},
        ],
    )
    assert "Q: 层序是否正确？" in notes
    assert "A: 接受近似生成" in notes

    with pytest.raises(CollageError) as caught:
        question_resolution_notes(
            questions,
            [
                {"question": questions[0], "answer": ""},
                {"question": questions[1], "answer": "接受"},
            ],
        )
    assert caught.value.code == "HUMAN_REVIEW_REQUIRED"


def test_automatic_notes_make_silent_defaults_auditable() -> None:
    draft = _complex_draft()
    draft["questions"] = ["是否接受当前设置？"]
    options = build_review_options(
        draft,
        None,
        None,
        background_expand_px=0,
        background_feather_px=0,
    )
    notes = automatic_review_notes(
        draft,
        options["slots"],
        options["overlays"],
        questions_deferred=True,
    )
    assert "本地通用字体" in notes
    assert "已改为近似制作" not in notes
    assert "未要求逐题填写" in notes


def test_automatic_notes_record_incomplete_basic_shape_fallback() -> None:
    draft = _complex_draft()
    draft["overlays"][0]["action"] = "basic_shape"
    options = build_review_options(
        draft,
        None,
        None,
        background_expand_px=0,
        background_feather_px=0,
    )

    notes = automatic_review_notes(
        draft,
        options["slots"],
        options["overlays"],
        questions_deferred=False,
    )

    assert "basic_shape 缺少可执行参数" in notes


def test_review_page_hides_nonessential_path_and_question_inputs() -> None:
    page_source = f"{HTML}\n{REVIEW_CSS}\n{REVIEW_JS}"
    assert 'href="/static/review.css"' in HTML
    assert 'src="/static/review.js"' in HTML
    assert "已按图片框大小自动设置" in page_source
    assert "字体样式已自动处理" in page_source
    assert "生成完整的独立素材" in page_source
    assert 'id="questions"' in page_source
    assert 'id="otherFeedback"' in page_source
    assert 'id="finalConfirmed"' in page_source
    assert "defer_questions: true" not in page_source
    assert "可选槽位 clip mask 路径" not in page_source
    assert "精确透明素材路径" not in page_source
    assert "data-question-index" not in page_source
    assert "恢复自动涂层" in page_source
    assert "删除蒙版为空" in page_source


def _full_manual_draft() -> dict:
    """构造涵盖羽化、文字、精确装饰和人工问题的有效 Draft。"""

    return {
        "slots": [
            {
                "id": "photo",
                "label": "羽化照片",
                "type": "image",
                "mode": "photo_feather",
                "source_rect": [0, 0, 80, 80],
                "target_rect": [0, 0, 80, 80],
                "upload_hint": "上传照片",
                "review_notes": "测试",
            },
            {
                "id": "caption",
                "label": "标题",
                "type": "text",
                "mode": None,
                "source_rect": [0, 80, 80, 20],
                "target_rect": [0, 80, 80, 20],
                "upload_hint": "输入标题",
                "review_notes": "测试",
                "default_text": "hello",
            },
        ],
        "overlays": [
            {
                "id": "ticket",
                "label": "票券",
                "source_rect": [40, 0, 40, 20],
                "target_rect": [40, 0, 40, 20],
                "attachment": None,
                "action": "reference_generate",
                "generation_brief": "测试票券",
                "requires_exact_content": True,
                "review_notes": "测试",
            }
        ],
        "background": {"background_brief": "延续灰色", "review_notes": "测试"},
        "layer_order": [
            {"type": "background"},
            {"type": "slot", "id": "photo"},
            {"type": "slot", "id": "caption"},
            {"type": "overlay", "id": "ticket"},
        ],
        "questions": ["是否接受近似票券？"],
    }


def _available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_json(url: str) -> dict:
    for _ in range(100):
        try:
            with urllib.request.urlopen(url, timeout=0.2) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.02)
    raise AssertionError(f"review server did not start: {url}")


@pytest.mark.parametrize("composition_mode", ["protected", "full_candidate"])
def test_review_server_saves_complex_draft_with_automatic_decisions(
    tmp_path: Path,
    composition_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collage.providers import ProviderAudit
    import collage.studio.review_server as review_server

    class CorrectionFixture:
        name = "fixture-vision"
        requested_model = "fixture"
        fixture = True

        def analyze(self, reference_bytes, **kwargs):
            assert "接受近似" in kwargs["prompt"]
            corrected = _full_manual_draft()
            corrected["questions"] = []
            corrected["overlays"][0]["requires_exact_content"] = False
            return corrected, ProviderAudit(
                self.name, "fixture", "fixture", None, True, 0
            )

    monkeypatch.setattr(
        review_server, "load_provider", lambda *_args: CorrectionFixture()
    )
    reference = tmp_path / "reference.png"
    manual = tmp_path / "manual.json"
    work = tmp_path / "work"
    Image.new("RGB", (100, 100), "gray").save(reference)
    atomic_write_json(manual, _full_manual_draft())
    draft_path = analyze_reference(reference, work, manual_draft_path=manual)
    output_path = work / "reviewed.json"
    port = _available_port()
    thread = threading.Thread(
        target=serve_review_ui,
        args=(draft_path, output_path),
        kwargs={"reviewer": "tester", "port": port},
        daemon=True,
    )
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    browser_draft = _wait_for_json(f"{base_url}/draft")
    options = _wait_for_json(f"{base_url}/review-options")
    with urllib.request.urlopen(base_url, timeout=5) as response:
        token = re.search(
            r'name="figcopy-csrf-token" content="([^"]+)"', response.read().decode()
        ).group(1)
    correction = urllib.request.Request(
        f"{base_url}/revise",
        method="POST",
        headers={"Content-Type": "application/json", "X-Figcopy-Token": token},
        data=json.dumps(
            {
                "revision": options["revision"],
                "question_resolutions": [
                    {"question": browser_draft["questions"][0], "answer": "接受近似"}
                ],
            }
        ).encode(),
    )
    with urllib.request.urlopen(correction, timeout=5) as response:
        assert json.loads(response.read())["task"]["kind"] == "revise_review"
    for _ in range(100):
        task = _wait_for_json(f"{base_url}/correction-status")["task"]
        if task["state"] not in {"queued", "running"}:
            assert task["state"] == "succeeded", task
            break
        time.sleep(0.02)
    browser_draft = _wait_for_json(f"{base_url}/draft")
    options = _wait_for_json(f"{base_url}/review-options")
    assert browser_draft["questions"] == []
    with urllib.request.urlopen(base_url, timeout=5) as response:
        assert b"/static/review.css" in response.read()
    with urllib.request.urlopen(f"{base_url}/static/review.css", timeout=5) as response:
        assert response.headers.get_content_type() == "text/css"
        assert b"system-ui" in response.read()
    with urllib.request.urlopen(f"{base_url}/static/review.js", timeout=5) as response:
        assert response.headers.get_content_type() == "text/javascript"
        assert b"fetch(" in response.read()
    with urllib.request.urlopen(f"{base_url}/mask", timeout=5) as response:
        automatic_mask = Image.open(io.BytesIO(response.read())).convert("L")
    assert automatic_mask.getbbox() is not None

    mask_buffer = io.BytesIO()
    Image.new("RGBA", (100, 100), (255, 255, 255, 255)).save(mask_buffer, format="PNG")
    browser_draft["questions"] = []
    payload = {
        "draft": browser_draft,
        "mask_png": "data:image/png;base64,"
        + base64.b64encode(mask_buffer.getvalue()).decode("ascii"),
        "slot_overrides": options["slots"],
        "overlay_overrides": options["overlays"],
        "revision": options["revision"],
        "final_confirmed": True,
        "empty_mask_approved": False,
        "background_composition_mode": composition_mode,
        "background_expand_px": 4,
        "background_feather_px": 8,
    }
    request = urllib.request.Request(
        f"{base_url}/save",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Figcopy-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.loads(response.read())
    thread.join(timeout=5)

    assert result["ok"] is True
    assert not thread.is_alive()
    reviewed = read_json(output_path)
    assert reviewed["slots"][0]["edge_fade_px"] == 6
    assert reviewed["slots"][1]["fallback_approved"] is True
    assert reviewed["overlays"][0]["requires_exact_content"] is False
    assert reviewed["background"]["composition_mode"] == composition_mode
    assert reviewed["background"]["expand_px"] == 4
    assert reviewed["background"]["feather_px"] == 8
    assert "本地通用字体" in reviewed["review"]["notes"]
    assert (
        read_json(work / "review_feedback.json")["question_resolutions"][0]["answer"]
        == "接受近似"
    )
    assert "未要求逐题填写" not in reviewed["review"]["notes"]
