"""Drive real background-source controls with synthetic projects and a mocked VLM."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from test_background_source import (
    background_project,
    fixed_raw,
    save_payload,
    tree_hashes,
)
from test_photo_background import FixtureVision

from collage.core.io import read_json
from collage.devtools.pipeline_ui_smoke import Browser
from collage.studio.workbench.server import create_workbench_server


@pytest.mark.parametrize("mode", ["to_photo", "to_fixed", "correction", "fork"])
def test_background_choice_in_browser(tmp_path, monkeypatch, mode):
    pytest.importorskip("websocket")
    candidates = [
        Path(os.environ.get("FIGCOPY_TEST_BROWSER", str(tmp_path / "not-set"))),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    ]
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        pytest.skip("Set FIGCOPY_TEST_BROWSER to a local Chromium executable")
    app, project = background_project(tmp_path, photo=mode == "to_fixed")
    provider = FixtureVision(fixed_raw())
    monkeypatch.setattr(
        "collage.studio.workbench.application.load_provider", lambda *_: provider
    )
    monkeypatch.setattr(
        app,
        "save_review",
        lambda project_id, payload: app.review_session(project_id).save(payload),
    )
    original = None
    if mode == "fork":
        session = app.review_session("source")
        session.save(save_payload(session))
        original = tree_hashes(project.root)
    server = create_workbench_server(port=0, application=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = None
    try:
        browser = Browser(executable, tmp_path / "browser")
        base = f"http://127.0.0.1:{server.server_address[1]}"
        browser.call(
            "Page.navigate",
            {
                "url": base
                + ("/projects/source" if mode == "fork" else "/projects/source/review")
            },
        )
        if mode == "fork":
            browser.until(
                "document.querySelector('#backgroundRevision') && !document.querySelector('#backgroundRevision').hidden"
            )
            browser.js("document.querySelector('#backgroundRevision').click()")
            browser.until(
                "location.pathname.endsWith('/review') && location.pathname !== '/projects/source/review'"
            )
            project = app.store.open(browser.js("location.pathname.split('/')[2]"))
            assert not (project.review / "reviewed.json").exists()
        browser.until(
            "document.querySelector('#status')?.textContent.includes('请检查识别结果')"
        )
        assert browser.js("document.querySelector('#backgroundSource').value") == (
            "slot" if mode == "to_fixed" else "fixed"
        )
        if mode != "to_fixed":
            assert (
                browser.js(
                    "getComputedStyle(document.querySelector('#backgroundSlotControl')).display"
                )
                == "none"
            )
            assert "仍使用固定底板" in browser.js(
                "document.querySelector('#backgroundSourceHint').textContent"
            )
            # Preserve a user's fixed-background setting across the round trip.
            browser.js("document.querySelector('#backgroundExpand').value = '7'")
            browser.js("document.querySelector('#finalConfirmed').checked = true")
            browser.js(
                "const source = document.querySelector('#backgroundSource'); source.value = 'slot'; source.dispatchEvent(new Event('change', {bubbles:true}))"
            )
            assert (
                browser.js("document.querySelector('#finalConfirmed').checked") is False
            )
            assert (
                browser.js("document.querySelector('#backgroundControls').hidden")
                is True
            )
            assert (
                browser.js("document.querySelector('[data-mode=cover_photo]').disabled")
                is True
            )
            assert browser.js("draft.layer_order[0]") == {
                "type": "slot",
                "id": "cover_photo",
            }
            browser.js("selectBackground('fixed')")
            assert (
                browser.js("document.querySelector('#backgroundExpand').value") == "7"
            )
            assert (
                browser.js("document.querySelector('#backgroundBrief').value")
                == "保留纸张底板"
            )
            assert browser.js("maskHasPixels()") is True
            browser.js("selectBackground('slot')")
        else:
            browser.js("selectBackground('fixed')")
            assert (
                browser.js("document.querySelector('#backgroundControls').hidden")
                is False
            )
            assert browser.js("maskHasPixels()") is True
            browser.js(
                "const brief = document.querySelector('#backgroundBrief'); brief.value='生成纸张底板'; brief.dispatchEvent(new Event('input', {bubbles:true}))"
            )
        assert provider.calls == 0
        assert browser.js("overlayTextReviewOpened") is False
        if mode == "correction":
            browser.js(
                "document.querySelector('#otherFeedback').value = '保留装饰'; document.querySelector('#revise').click()"
            )
            browser.until(
                "document.querySelector('#status').textContent.includes('与刚才的显式选择不同')"
            )
            assert provider.calls == 1
            assert (
                browser.js("document.querySelector('#backgroundSource').value")
                == "fixed"
            )
            assert (
                browser.js("document.querySelector('#finalConfirmed').checked") is False
            )
            evidence = read_json(project.analysis / "review_feedback.json")
            assert evidence["background_decision"]["after"]["mode"] == "slot"
            browser.js("selectBackground('slot')")
        browser.js("document.querySelector('#finalConfirmed').checked = true")
        assert browser.js("preflightErrors()") == []
        browser.js("document.querySelector('#save').click()")
        browser.until("!location.pathname.endsWith('/review')")
        reviewed = read_json(project.review / "reviewed.json")
        assert reviewed["version"] == (
            "collage-reviewed/1" if mode == "to_fixed" else "collage-build/3"
        )
        assert (project.review / "remove_mask.png").exists() == (mode == "to_fixed")
        if original:
            assert tree_hashes(app.store.open("source").root) == original
        assert not browser.errors
    finally:
        if browser:
            browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
