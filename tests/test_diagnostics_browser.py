"""Exercise live logs, complete errors and retained evidence in a real browser using fixtures."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

import pytest

from collage.core.errors import CollageError
from collage.devtools.pipeline_ui_smoke import Browser
from collage.projects import DataPaths
from collage.studio.workbench.application import WorkbenchApplication
from collage.studio.workbench.server import create_workbench_server
from test_diagnostics import invalid_draft, response_provider, wait_job
from test_workbench import _create_form


@pytest.mark.parametrize("failure", ["schema", "http500"])
def test_running_logs_and_failure_details_in_browser(tmp_path, monkeypatch, failure):
    pytest.importorskip("websocket")
    executable = next(
        (
            path
            for path in [
                Path(os.environ.get("FIGCOPY_TEST_BROWSER", "no-browser")),
                Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
                Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            ]
            if path.is_file()
        ),
        None,
    )
    if executable is None:
        pytest.skip("No Chromium browser installed")
    provider = response_provider(monkeypatch, json.dumps(invalid_draft()))
    respond = provider._client.post_json
    release = threading.Event()

    def delayed(*a, **k):
        logging.getLogger("collage.fixture").info("fixture-model-waiting")
        if not release.wait(timeout=45):
            raise RuntimeError("fixture not released")
        if failure == "http500":
            raise CollageError(
                "PROVIDER_REQUEST_FAILED",
                "内网服务返回 HTTP 500",
                details={
                    "http_status": 500,
                    "operation": "analyze-reference",
                    "request_id": "fixture-request",
                    "response": "CUDA out of memory <script>window.injected=true</script>",
                },
            )
        return respond(*a, **k)

    monkeypatch.setattr(provider._client, "post_json", delayed)
    monkeypatch.setattr("collage.workflows.stages.load_provider", lambda *a: provider)
    app = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
    form = _create_form("diagnostic-browser")
    del form.files["manual_draft"]
    app.start_project(form)
    server = create_workbench_server(application=app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = None
    try:
        browser = Browser(executable, tmp_path / "browser")
        base = f"http://127.0.0.1:{server.server_port}"
        browser.call("Page.navigate", {"url": base + "/projects/diagnostic-browser"})
        browser.until(
            "document.getElementById('taskLog')?.textContent.includes('fixture-model-waiting')"
        )
        assert browser.js("state.currentStatus.task.state") == "running"
        release.set()
        wait_job(app.jobs, "diagnostic-browser")
        browser.until(
            "state.currentStatus?.task?.state === 'failed' && document.getElementById('diagnosticsPanel').open"
        )
        assert browser.js("document.getElementById('errorBanner').hidden") is False
        if failure == "schema":
            browser.until(
                "document.querySelectorAll('#errorDetails .validation-issues li').length === 7"
            )
            browser.until(
                "document.querySelector('#analysisAttempts a[href$=\"candidate.json\"]') !== null"
            )
            url = browser.js(
                "document.querySelector('#analysisAttempts a[href$=\"candidate.json\"]').getAttribute('href')"
            )
            assert (
                browser.js(
                    "fetch("
                    + json.dumps(url)
                    + ").then(r => r.json()).then(d => d.questions.length)"
                )
                == 6
            )
        else:
            browser.until(
                "document.getElementById('errorDetails').textContent.includes('CUDA out of memory')"
            )
            assert browser.js("typeof window.injected") == "undefined"
            assert browser.js("document.querySelector('#errorDetails script')") is None
        browser.screenshot(tmp_path / (failure + ".png"))
        browser.call("Page.reload")
        browser.until(
            "document.getElementById('taskLog')?.textContent.includes('fixture-model-waiting')"
        )
        assert browser.js("document.getElementById('diagnosticsPanel').open") is True
        # A new registry reads the same persisted errors and logs without model calls.
        restarted = WorkbenchApplication(app.paths)
        assert restarted.diagnostics("diagnostic-browser")["job"]["state"] == "failed"
        assert not browser.errors
        if failure == "schema":
            old_job = app.jobs.latest("diagnostic-browser")["id"]
            app.jobs.submit(
                "diagnostic-browser",
                "retry",
                lambda: logging.getLogger("collage.fixture").info("second-task-marker"),
            )
            wait_job(app.jobs, "diagnostic-browser")
            browser.until(
                "document.getElementById('taskLog').textContent.includes('second-task-marker')"
            )
            assert "fixture-model-waiting" not in browser.js(
                "document.getElementById('taskLog').textContent"
            )
            browser.js(
                "document.getElementById('diagnosticJob').value="
                + json.dumps(old_job)
                + ";document.getElementById('diagnosticJob').dispatchEvent(new Event('change'))"
            )
            browser.until(
                "document.getElementById('taskLog').textContent.includes('fixture-model-waiting')"
            )
            assert "second-task-marker" not in browser.js(
                "document.getElementById('taskLog').textContent"
            )
    finally:
        release.set()
        if browser:
            browser.close()
        wait_job(app.jobs, "diagnostic-browser")
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
