"""Use a real browser to configure internal services and recover an existing project."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from collage.devtools.pipeline_ui_smoke import Browser
from collage.projects import DataPaths
from collage.providers.selection import INTRANET_VISION, QWEN_IMAGE, YIBU_VISION
from collage.studio.workbench.application import WorkbenchApplication
from collage.studio.workbench.server import create_workbench_server
from test_intranet_providers import isolated_config, model_server  # noqa: F401
from test_workbench import _create_form, _image_bytes, _wait_for_job


@pytest.mark.parametrize("mode", ["create", "retry"])
def test_provider_choices_in_browser(tmp_path, monkeypatch, mode):
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
        pytest.skip("Set FIGCOPY_TEST_BROWSER to a Chromium executable")
    with model_server() as (model, model_base):
        app = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
        if mode == "retry":
            form = _create_form("browser-retry")
            del form.files["manual_draft"]
            app.start_project(form)
            for _ in range(200):
                if app.latest_job("browser-retry")["state"] == "failed":
                    break
                time.sleep(0.02)
            assert app.latest_job("browser-retry")["state"] == "failed"
            assert app.project_status("browser-retry")["stage"] == "blocked"
        server = create_workbench_server(port=0, application=app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        browser = None
        try:
            browser = Browser(executable, tmp_path / "browser")
            base = f"http://127.0.0.1:{server.server_port}"
            browser.call(
                "Page.navigate",
                {"url": base + ("/projects/browser-retry" if mode == "retry" else "/")},
            )
            browser.until(
                "state.providerStatus !== null && document.querySelector('#providerSettings') !== null"
            )
            if mode == "retry":
                browser.until("document.querySelector('#retryForm') !== null")
            browser.js("document.querySelector('#providerSettings').click()")
            browser.until(
                "document.querySelector('#providerDialog').open && document.querySelector('#defaultVision').options.length === 2"
            )
            values = {
                "defaultVision": INTRANET_VISION,
                "defaultImage": QWEN_IMAGE,
                "intranetBaseUrl": model_base + "/v1",
                "intranetApiKey": "browser-private-key",
                "qwenBaseUrl": model_base,
            }
            browser.js(
                "Object.entries(" + json.dumps(values) + ").forEach(([id,value]) => {"
                " const input=document.getElementById(id); input.value=value;"
                " input.dispatchEvent(new Event('input', {bubbles:true})); });"
            )
            browser.js("document.querySelector('#providerSubmit').click()")
            browser.until("!document.querySelector('#providerDialog').open")
            assert browser.js("document.querySelector('#intranetApiKey').value") == ""
            assert os.environ.get("YIBU_API_KEY") is None
            if mode == "create":
                browser.js("document.querySelector('#newProject').click()")
                browser.until("document.querySelector('#createDialog').open")
                assert (
                    browser.js("document.querySelector('#createVision').value")
                    == INTRANET_VISION
                )
                assert (
                    browser.js("document.querySelector('#createImage').value")
                    == QWEN_IMAGE
                )
                source = tmp_path / "reference.png"
                source.write_bytes(_image_bytes("#CC8844"))
                root = browser.call("DOM.getDocument")["root"]["nodeId"]
                node = browser.call(
                    "DOM.querySelector",
                    {"nodeId": root, "selector": "#referenceUpload"},
                )["nodeId"]
                browser.call(
                    "DOM.setFileInputFiles", {"nodeId": node, "files": [str(source)]}
                )
                browser.js(
                    "document.querySelector('[name=project_id]').value='browser-create';"
                    "document.querySelector('[name=reviewer]').value='tester';"
                    "document.querySelector('#createSubmit').click()"
                )
                project_id = "browser-create"
                browser.until("location.pathname === '/projects/browser-create'")
            else:
                project_id = "browser-retry"
            _wait_for_job(app, project_id)
            browser.until(
                "state.currentStatus?.task?.state === 'succeeded' && "
                "!document.querySelector('#saveProjectProviders')?.disabled && "
                "document.querySelector('#projectVision')?.value === "
                + json.dumps(INTRANET_VISION)
            )
            assert (
                app.project_status(project_id)["providers"]["image_provider"]
                == QWEN_IMAGE
            )
            assert model.calls[-1][0] == "/v1/chat/completions"
            assert model.calls[-1][2] == "Bearer browser-private-key"
            browser.js(
                "document.querySelector('#projectVision').value="
                + json.dumps(YIBU_VISION)
                + ";"
                "document.querySelector('#saveProjectProviders').click()"
            )
            browser.until(
                "state.currentStatus?.providers?.vision_provider === "
                + json.dumps(YIBU_VISION)
            )
            assert app.provider_status()["selection"]["vision"] == INTRANET_VISION
            browser.screenshot(tmp_path / "provider-project.png")
            assert not browser.errors
        finally:
            if browser:
                browser.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
