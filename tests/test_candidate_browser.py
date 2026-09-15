"""Verify candidate warnings, layout placeholders and manual regeneration in a real browser."""

import os
import threading
from pathlib import Path

import pytest
from test_candidate_workflow import _project

from collage.devtools.pipeline_ui_smoke import Browser
from collage.studio.workbench.server import create_workbench_server


@pytest.mark.parametrize("photo", [False, True])
def test_candidate_page_and_single_piece_regeneration(tmp_path, monkeypatch, photo):
    pytest.importorskip("websocket")
    candidates = [
        Path(os.environ.get("FIGCOPY_TEST_BROWSER", "no-browser")),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    ]
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        pytest.skip("Set FIGCOPY_TEST_BROWSER to a local Chromium executable")
    app, provider, source = _project(tmp_path, monkeypatch, broken=True, photo=photo)
    server = create_workbench_server(port=0, application=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = None
    try:
        browser = Browser(executable, tmp_path / "browser")
        browser.call(
            "Page.navigate",
            {"url": f"http://127.0.0.1:{server.server_address[1]}/projects/candidate"},
        )
        browser.until("document.querySelector('#regenerateOverlay') !== null")
        assert "first" in browser.js(
            "document.querySelector('#overlayActions').textContent"
        )
        assert "已跳过" in browser.js(
            "document.querySelector('#overlayActions').textContent"
        )
        if photo:
            browser.until(
                "Array.from(document.images).some(img => img.src.includes('/layout/preview') && img.complete && img.naturalWidth > 0)"
            )
            assert "编号占位" in browser.js(
                "document.querySelector('#actionPanel').textContent"
            )
        else:
            browser.until("document.querySelector('#approvalForm') !== null")
        browser.screenshot(tmp_path / "candidate-before.png")
        provider.broken = False
        browser.js("window.figcopyOriginalPage = true")
        browser.js(
            "document.querySelector('#overlayChoice').value = 'first'; document.querySelector('#regenerateOverlay').click()"
        )
        browser.until(
            "document.querySelector('#regenerateOverlay') !== null && !document.querySelector('#regenerateOverlay').disabled && !document.querySelector('#overlayActions').textContent.includes('已跳过')"
        )
        assert browser.js("location.pathname") == "/projects/candidate"
        assert browser.js("window.figcopyOriginalPage") is True
        assert "替换当前素材" in browser.js(
            "document.querySelector('#regenerateOverlay').textContent"
        )
        assert "已跳过" not in browser.js(
            "document.querySelector('#overlayActions').textContent"
        )
        assert len(provider.calls) == 3
        assert app.project_status(source.project_id)["warnings"] == []
        assert len(app.store.list()) == 1
        assert not browser.errors
        browser.screenshot(tmp_path / "candidate-after.png")
    finally:
        if browser:
            browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
