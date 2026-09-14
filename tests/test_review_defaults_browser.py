"""Exercise optional feedback and hidden text acceptance through the real review page."""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path

import pytest
from PIL import Image

from collage.core.io import atomic_write_json, read_json
from collage.devtools.pipeline_ui_smoke import Browser
from collage.projects import DataPaths
from collage.providers import ProviderAudit
from collage.studio.workbench.application import WorkbenchApplication
from collage.studio.workbench.server import create_workbench_server


@pytest.mark.parametrize("mode", ["defaults", "opened", "correction"])
def test_review_acceptance_in_browser(tmp_path, monkeypatch, mode):
    pytest.importorskip("websocket")
    candidates = [
        Path(os.environ["FIGCOPY_TEST_BROWSER"])
        if os.environ.get("FIGCOPY_TEST_BROWSER")
        else tmp_path / "no-configured-browser",
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    ]
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        pytest.skip("Set FIGCOPY_TEST_BROWSER to a local Chromium executable")

    reference = tmp_path / "reference.png"
    Image.new("RGB", (160, 100), "gray").save(reference)
    overlays = [
        {
            "id": identifier,
            "label": label,
            "source_rect": [10, y, 130, 30],
            "target_rect": [10, y, 130, 30],
            "attachment": None,
            "action": "reference_generate",
            "generation_brief": "fixture lettering",
            "requires_exact_content": False,
            "review_notes": "synthetic fixture",
            "text_content": text,
        }
        for identifier, label, text, y in [
            ("english", "英文", "hello", 10),
            ("chinese", "中文", "此刻", 60),
        ]
    ]
    draft = {
        "slots": [],
        "overlays": overlays,
        "background": {"background_brief": "plain", "review_notes": "fixture"},
        "layer_order": [
            {"type": "background"},
            *({"type": "overlay", "id": item["id"]} for item in overlays),
        ],
        "questions": ["保留英文？", "保留中文？"],
    }
    manual = tmp_path / "manual.json"
    atomic_write_json(manual, draft)
    app = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
    app.workflow.start(
        "review-defaults",
        reference,
        reviewer="fixture-tester",
        manual_draft_path=manual,
        open_review=False,
    )
    project = app.store.open("review-defaults")

    # Exercise the real save endpoint and ReviewedSpec validation without enqueueing
    # paid asset generation; this check ends at the human review boundary.
    monkeypatch.setattr(
        app,
        "save_review",
        lambda project_id, payload: app.review_session(project_id).save(payload),
    )
    calls = []

    class CorrectionFixture:
        name = "browser-correction-fixture"
        requested_model = "fixture"
        fixture = True

        def analyze(self, reference_bytes, **kwargs):
            calls.append(kwargs)
            corrected = copy.deepcopy(draft)
            corrected["questions"] = []
            return corrected, ProviderAudit(
                self.name, "fixture", "fixture", "fixture", True, 0
            )

    monkeypatch.setattr(
        "collage.studio.workbench.application.load_provider",
        lambda *_: CorrectionFixture(),
    )
    server = create_workbench_server(port=0, application=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = None
    try:
        browser = Browser(executable, tmp_path / "browser-profile")
        base = f"http://127.0.0.1:{server.server_address[1]}"
        browser.call(
            "Page.navigate", {"url": base + "/projects/review-defaults/review"}
        )
        browser.until("document.querySelectorAll('#questions textarea').length === 2")
        assert browser.js("document.querySelector('#overlayDetails').open") is False
        browser.js("document.querySelector('#revise').click()")
        assert "没有填写反馈" in browser.js(
            "document.querySelector('#status').textContent"
        )
        assert calls == []

        browser.js("document.querySelector('#finalConfirmed').click()")
        assert browser.js("preflightErrors()") == []
        browser.js("""const answer = document.querySelector('#questions textarea');
            answer.value = '保留'; answer.dispatchEvent(new Event('input', {bubbles:true}));""")
        assert any("VLM" in error for error in browser.js("preflightErrors()"))

        if mode == "correction":
            # Only the first answer is filled; the second is optional.
            browser.js("document.querySelector('#revise').click()")
            browser.until(
                "document.querySelector('#status').textContent.includes('已生成纠正后的结果')"
            )
            assert len(calls) == 1
            assert (
                browser.js("document.querySelector('#finalConfirmed').checked") is False
            )
        else:
            browser.js("document.querySelector('#questions textarea').value = '  '")
            if mode == "opened":
                browser.js("document.querySelector('#overlayDetails summary').click()")
                browser.until("overlayTextReviewOpened")
                browser.js("document.querySelector('#finalConfirmed').click()")
                assert len(browser.js("preflightErrors()")) == 2
                browser.js(
                    "document.querySelector('#overlayDetails summary').click(); renderItems()"
                )
                assert len(browser.js("preflightErrors()")) == 2
                browser.js("document.querySelector('#overlayDetails summary').click()")
                browser.until("document.querySelector('#overlayDetails').open")
                browser.js("""document.querySelectorAll('[data-field=text_confirmed]').forEach(n => n.click());
                    const input = document.querySelector('[data-field=text_content]');
                    input.value = 'changed'; input.dispatchEvent(new Event('change', {bubbles:true}));""")
                assert (
                    browser.js(
                        "document.querySelector('[data-field=text_confirmed]').checked"
                    )
                    is False
                )
                assert any(
                    "完整文字" in error for error in browser.js("preflightErrors()")
                )
                browser.js(
                    "document.querySelector('[data-field=text_confirmed]').click()"
                )

        browser.js("document.querySelector('#finalConfirmed').checked = true")
        assert browser.js("preflightErrors()") == []
        browser.js("document.querySelector('#save').click()")
        browser.until("location.pathname === '/projects/review-defaults'")
        evidence = read_json(project.review / "confirmation.json")
        reviewed = read_json(project.review / "reviewed.json")
        assert evidence["default_text_accepted"] == (
            [] if mode == "opened" else ["english", "chinese"]
        )
        assert evidence["questions_accepted_as_is"] == (
            [] if mode == "correction" else draft["questions"]
        )
        assert reviewed["overlays"][0]["text_content"] == (
            "changed" if mode == "opened" else "hello"
        )
        assert len(calls) == (1 if mode == "correction" else 0)
        assert not browser.errors
    finally:
        if browser:
            browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
