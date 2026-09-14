"""Exercise attachment editing, grouping and exact previews in a real browser."""

import os
import threading
from pathlib import Path

import pytest

from collage.core.io import read_json
from collage.devtools.pipeline_ui_smoke import Browser
from collage.studio.workbench.server import create_workbench_server
from test_photo_attachment import attachment_project
from test_workbench import _wait_for_job


@pytest.mark.parametrize("phase", ["review", "layout"])
def test_attachment_browser(tmp_path, monkeypatch, phase):
    pytest.importorskip("websocket")
    executable = next(
        (
            p
            for p in [
                Path(os.environ.get("FIGCOPY_TEST_BROWSER", "no-browser")),
                Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
                Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            ]
            if p.is_file()
        ),
        None,
    )
    if executable is None:
        pytest.skip("Set FIGCOPY_TEST_BROWSER to a local Chromium executable")
    app, provider, project = attachment_project(
        tmp_path, monkeypatch, confirm=phase == "layout"
    )
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
                + "/projects/attachment/"
                + ("review" if phase == "review" else "layers")
            },
        )
        if phase == "review":
            browser.until(
                "document.querySelector('#structurePreview')?.naturalWidth === 260"
            )
            browser.js("document.querySelector('#overlayDetails').open = true")
            browser.js(
                "const owner = document.querySelector('[data-owner=frame_back]'); owner.value = ''; owner.dispatchEvent(new Event('change'))"
            )
            browser.until(
                "!layoutBusy && draft.overlays.find(item => item.id === 'frame_back').attachment === null"
            )
            assert browser.js(
                "draft.layer_order.some(item => item.type === 'overlay' && item.id === 'frame_back')"
            )
            browser.js(
                "{ const owner = document.querySelector('[data-owner=frame_back]'); owner.value = 'back'; owner.dispatchEvent(new Event('change')); }"
            )
            browser.until(
                "!layoutBusy && draft.overlays.find(item => item.id === 'frame_back').attachment?.slot_id === 'back'"
            )
            browser.js("updateRect('slot', 'back', 0, 30)")
            browser.until(
                "!layoutBusy && draft.slots.find(item => item.id === 'back').target_rect[0] === 30"
            )
            assert (
                browser.js(
                    "draft.overlays.find(item => item.id === 'frame_back').target_rect[0]"
                )
                == 30
            )
            assert (
                browser.js(
                    "draft.overlays.find(item => item.id === 'middle').attachment"
                )
                is None
            )
            browser.screenshot(tmp_path / "review-structure.png")
            browser.js(
                "document.querySelector('#finalConfirmed').checked = true; document.querySelector('#save').click()"
            )
            browser.until("location.pathname === '/projects/attachment'")
            _wait_for_job(app, "attachment")
            template = read_json(project.template / "template.json")
            frame = next(
                item for item in template["overlays"] if item["id"] == "frame_back"
            )
            assert frame["rect"][0] == 30 and frame["attachment"]["slot_id"] == "back"
        else:
            browser.until(
                "document.querySelector('#layers button[data-id=\"slot:back\"]') !== null"
            )
            browser.js(
                "document.querySelector('#layers button[data-id=\"slot:back\"]').click()"
            )
            browser.js(
                "const width = document.querySelector('#width'); width.value = '150'; width.dispatchEvent(new Event('change'))"
            )
            browser.until(
                "!busy && documentState.items.find(item => item.id === 'slot:back').rect[2] === 150"
            )
            assert (
                browser.js(
                    "documentState.items.find(item => item.id === 'asset:frame_back').rect[2]"
                )
                == 150
            )
            assert browser.js("document.querySelector('#keepAspect').disabled") is True
            browser.js(
                "const angle = document.querySelector('#rotation'); angle.value = '90'; angle.dispatchEvent(new Event('change'))"
            )
            browser.until(
                "!busy && documentState.items.find(item => item.id === 'asset:frame_back').rotation_deg === 90"
            )
            browser.js("document.querySelector('#up').click()")
            browser.until("!busy && documentState.items[1].id === 'asset:middle'")
            browser.js("document.querySelector('#up').click()")
            browser.until(
                "!busy && documentState.items.findIndex(item => item.id === 'slot:back') > documentState.items.findIndex(item => item.id === 'slot:front')"
            )
            browser.js("document.querySelector('#preview').click()")
            browser.until(
                "!busy && !document.querySelector('#exactPreview').hidden && document.querySelector('#exactPreview').naturalWidth === 260"
            )
            browser.screenshot(tmp_path / "layout-exact.png")
            browser.js("document.querySelector('#save').click()")
            browser.until(
                "location.pathname.startsWith('/projects/attachment-layout-')"
            )
            target = app.store.open(browser.js("location.pathname.split('/')[2]"))
            template = read_json(target.template / "template.json")
            assert template["layer_order"][2] == {"type": "slot", "id": "front"}
            assert (
                next(
                    item for item in template["overlays"] if item["id"] == "frame_back"
                )["rotation_deg"]
                == 90
            )
            assert (
                next(item for item in template["overlays"] if item["id"] == "top")[
                    "attachment"
                ]
                is None
            )
        assert provider.calls == []
        assert not browser.errors
    finally:
        if browser:
            browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
