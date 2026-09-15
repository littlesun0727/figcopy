"""Observe real build transitions and per-material previews with a gated offline provider."""

import io
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from test_workbench import _create_form, _review_payload, _wait_for_job

from collage.core.errors import CollageError
from collage.devtools.pipeline_ui_smoke import Browser
from collage.projects import DataPaths
from collage.providers import GeneratedImage, ImageCapabilities, ProviderAudit
from collage.studio.workbench.application import WorkbenchApplication
from collage.studio.workbench.multipart import UploadedFile
from collage.studio.workbench.pipeline import pipeline_document, pipeline_image
from collage.studio.workbench.server import create_workbench_server


class GatedProvider:
    capabilities = ImageCapabilities(
        "live-progress-fixture", "fixture", True, True, True, "white_edit", fixture=True
    )

    def __init__(self, *, fail=None, hard_fail=None):
        self.fail = fail
        self.hard_fail = hard_fail
        self.calls = []
        self.entered = {
            key: threading.Event() for key in ("background", "star", "flower", "tape")
        }
        self.release = {key: threading.Event() for key in self.entered}

    def gate(self, name):
        self.calls.append(name)
        self.entered[name].set()
        assert self.release[name].wait(60), "fixture was not released"
        if name == self.hard_fail:
            raise RuntimeError("interrupted fixture build")
        if name == self.fail:
            raise CollageError(
                "FIXTURE_GENERATION_FAILED", "fixture material has no output"
            )

    def finish(self):
        for event in self.release.values():
            event.set()

    def edit_background(self, reference, mask, **kwargs):
        self.gate("background")
        return GeneratedImage(
            Image.new("RGB", reference.size, "#efdfc2"),
            ProviderAudit(
                self.capabilities.name, "fixture", "fixture", "bg-fixture", True, 0
            ),
        )

    def make_overlay(self, image, *, brief, **kwargs):
        name = next(
            name for name in ("star", "flower", "tape") if brief.startswith(name)
        )
        self.gate(name)
        raw = Image.new("RGBA", (240, 160))
        draw = ImageDraw.Draw(raw)
        if name == "star":
            draw.polygon(
                [
                    (120, 20),
                    (145, 65),
                    (200, 75),
                    (155, 105),
                    (160, 145),
                    (120, 122),
                    (80, 145),
                    (85, 105),
                    (40, 75),
                    (95, 65),
                ],
                fill="#f5b43b",
            )
        elif name == "flower":
            draw.ellipse((65, 25, 175, 135), fill="#ee789e")
        else:
            draw.rounded_rectangle((30, 55, 210, 105), radius=8, fill="#76bfc4")
        return GeneratedImage(
            raw,
            ProviderAudit(
                self.capabilities.name, "fixture", "fixture", name + "-fixture", True, 0
            ),
            raw_image=raw,
        )


def start_build(tmp_path, monkeypatch, *, fail=None, hard_fail=None):
    app = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
    provider = GatedProvider(fail=fail, hard_fail=hard_fail)
    monkeypatch.setattr(app.workflow.stages, "_image_provider", lambda *_: provider)
    form = _create_form("live")
    form.fields["name"] = "素材制作进度 · 离线演示"
    form.files.pop("background_candidate")
    reference = Image.new("RGB", (480, 320), "#e0c49e")
    draw = ImageDraw.Draw(reference)
    draw.polygon(
        [
            (65, 25),
            (85, 55),
            (125, 60),
            (95, 80),
            (95, 105),
            (65, 90),
            (35, 105),
            (40, 80),
            (15, 60),
            (50, 55),
        ],
        fill="#eaa434",
    )
    draw.ellipse((180, 25, 280, 105), fill="#df749a")
    draw.rounded_rectangle((335, 48, 465, 86), radius=6, fill="#68aeb5")
    buffer = io.BytesIO()
    reference.save(buffer, format="PNG")
    form.files["reference"] = UploadedFile(
        "reference.png", "image/png", buffer.getvalue()
    )
    draft = json.loads(form.files["manual_draft"].data)
    draft["overlays"] = [
        {
            "id": name,
            "label": label,
            "source_rect": rect,
            "target_rect": rect,
            "attachment": None,
            "action": "reference_generate",
            "generation_brief": name,
            "requires_exact_content": False,
            "review_notes": "",
        }
        for name, label, rect in [
            ("star", "星星贴纸", [10, 20, 120, 90]),
            ("flower", "花朵装饰", [175, 20, 110, 90]),
            ("tape", "胶带", [330, 40, 140, 55]),
        ]
    ]
    draft["layer_order"] += [
        {"type": "overlay", "id": item["id"]} for item in draft["overlays"]
    ]
    form.files["manual_draft"] = UploadedFile(
        "draft.json", "application/json", json.dumps(draft).encode()
    )
    app.start_project(form)
    _wait_for_job(app, "live")
    app.save_review("live", _review_payload(app, "live"))
    assert provider.entered["background"].wait(10)
    return app, provider, app.store.open("live")


def wait_failure(app):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = app.latest_job("live")
        if job["state"] == "failed":
            return
        time.sleep(0.02)
    raise AssertionError("fixture failure did not arrive")


def test_live_counts_and_images_arrive_before_template_is_finished(
    tmp_path, monkeypatch
):
    app, provider, project = start_build(tmp_path, monkeypatch, fail="flower")
    try:
        data = pipeline_document(project)
        assert data["progress"]["phase"] == "background"
        assert data["progress"]["total"] == 3 and data["progress"]["completed"] == 0
        assert data["background"]["started_at"]
        assert all(item["images"].get("reference") for item in data["overlays"])
        provider.release["background"].set()
        assert provider.entered["star"].wait(10)
        data = pipeline_document(project)
        assert data["background"]["status"] == "complete"
        assert data["progress"]["current"]["index"] == 1
        provider.release["star"].set()
        assert provider.entered["flower"].wait(10)
        data = pipeline_document(project)
        assert data["progress"]["completed"] == 1
        assert data["progress"]["current"]["index"] == 2
        assert not (project.template / "template.json").exists()
        assert set(data["overlays"][0]["images"]) == {
            "reference",
            "raw",
            "processed",
            "final",
        }
        assert Image.open(
            io.BytesIO(pipeline_image(project, "overlay", "star", "raw"))
        ).size == (240, 160)
        provider.release["flower"].set()
        assert provider.entered["tape"].wait(10)
        data = pipeline_document(project)
        assert data["progress"]["current"]["index"] == 3
        assert (
            data["progress"]["completed"]
            == data["progress"]["skipped"]
            == data["progress"]["remaining"]
            == 1
        )
        assert data["overlays"][1]["warnings"]
        provider.release["tape"].set()
        _wait_for_job(app, "live")
        progress = pipeline_document(project)["progress"]
        assert progress["phase"] == "complete"
        assert (progress["completed"], progress["skipped"], progress["processed"]) == (
            2,
            1,
            3,
        )
        assert provider.calls == ["background", "star", "flower", "tape"]
    finally:
        provider.finish()


def test_interrupted_retry_does_not_count_saved_pixels_as_new_completion(
    tmp_path, monkeypatch
):
    app, provider, project = start_build(tmp_path, monkeypatch, hard_fail="flower")
    try:
        provider.finish()
        wait_failure(app)
        assert pipeline_document(project)["progress"]["phase"] == "failed"
        assert (project.template / "assets/overlay_star.png").is_file()
        retry = GatedProvider()
        retry.capabilities = replace(
            retry.capabilities, name="new-fixture-configuration"
        )
        monkeypatch.setattr(app.workflow.stages, "_image_provider", lambda *_: retry)
        app.jobs.submit(
            "live", "retry", lambda: app.workflow.resume("live", open_review=False)
        )
        try:
            assert retry.entered["background"].wait(10)
            data = pipeline_document(project)
            assert data["progress"]["completed"] == 0
            assert data["progress"]["phase"] == "background"
            assert data["overlays"][0]["status"] == "pending"
            assert "final" not in data["overlays"][0]["images"]
            assert (project.template / "assets/overlay_star.png").is_file()
        finally:
            retry.finish()
        _wait_for_job(app, "live")
        assert pipeline_document(project)["progress"]["completed"] == 3
    finally:
        provider.finish()


def test_background_failure_is_visible_without_material_progress(tmp_path, monkeypatch):
    app, provider, project = start_build(tmp_path, monkeypatch, fail="background")
    try:
        provider.release["background"].set()
        wait_failure(app)
        data = pipeline_document(project)
        assert data["background"]["status"] == "failed"
        assert data["progress"]["phase"] == "failed"
        assert data["progress"]["completed"] == 0 and data["progress"]["remaining"] == 3
        assert provider.calls == ["background"]
    finally:
        provider.finish()


def test_photo_background_without_decorations_has_no_fake_generation_count(
    tmp_path, monkeypatch
):
    from test_background_source import background_project, save_payload

    app, project = background_project(tmp_path, photo=True, decoration=False)
    monkeypatch.setattr(app.workflow.stages, "_image_provider", lambda *_: None)
    app.save_review("source", save_payload(app.review_session("source")))
    _wait_for_job(app, "source")
    data = pipeline_document(project)
    assert data["background"]["status"] == "skipped"
    assert (
        data["progress"]["total"]
        == data["progress"]["completed"]
        == data["progress"]["skipped"]
        == 0
    )
    assert data["progress"]["phase"] == "complete"


def test_default_running_page_updates_each_material_in_browser(tmp_path, monkeypatch):
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
    app, provider, project = start_build(tmp_path, monkeypatch, fail="flower")
    server = create_workbench_server(port=0, application=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = None
    try:
        browser = Browser(executable, tmp_path / "browser")
        base = f"http://127.0.0.1:{server.server_address[1]}"
        browser.call("Page.navigate", {"url": base + "/projects/live"})
        browser.until(
            "document.querySelector('#buildProgress')?.textContent.includes('正在清版')"
        )
        assert browser.js("state.viewedStage") is None
        browser.until("document.querySelectorAll('[data-material-id]').length === 3")
        assert "已完成 0 件" in browser.js(
            "document.querySelector('.build-counts').textContent"
        )
        browser.js("document.querySelector('#actionPanel').scrollIntoView()")
        browser.screenshot(tmp_path / "01-background.png")
        provider.release["background"].set()
        assert provider.entered["star"].wait(10)
        browser.until(
            "document.querySelector('.build-current')?.textContent.includes('第 1 / 3 件')"
        )
        provider.release["star"].set()
        assert provider.entered["flower"].wait(10)
        browser.until(
            "document.querySelector('.build-current')?.textContent.includes('第 2 / 3 件')"
        )
        assert "已完成 1 件" in browser.js(
            "document.querySelector('.build-counts').textContent"
        )
        browser.js(
            "document.querySelector('[data-material-id=star]').scrollIntoView({block:'center'})"
        )
        browser.until(
            "Array.from(document.querySelectorAll('[data-material-id=star] .material-pair img')).length === 2 && Array.from(document.querySelectorAll('[data-material-id=star] .material-pair img')).every(i => i.complete && i.naturalWidth > 0)"
        )
        browser.js(
            "window.completedCrop = document.querySelector('[data-material-id=star] img'); window.originalPage = true"
        )
        browser.js("""window.realApi = api; window.slowReads = 0;
            window.releaseSlowRead = null;
            const slowRead = new Promise(resolve => { window.releaseSlowRead = resolve; });
            api = async url => { if (url.endsWith('/pipeline')) { slowReads++; await slowRead; } return realApi(url); };
            window.pendingBuildRead = renderPipelineView(state.currentStatus, true); true;""")
        browser.js(
            "renderPipelineView(state.currentStatus, true); renderPipelineView(state.currentStatus, true); true"
        )
        assert browser.js("slowReads") == 1
        browser.js("releaseSlowRead(); pendingBuildRead")
        browser.js("api = realApi")
        assert browser.js(
            "completedCrop === document.querySelector('[data-material-id=star] img')"
        )
        browser.screenshot(tmp_path / "02-first-result.png")
        browser.js(
            "document.querySelectorAll('[data-material-id=star] .material-pair a')[1].click()"
        )
        browser.until("document.querySelector('#imageDialog').open")
        provider.release["flower"].set()
        assert provider.entered["tape"].wait(10)
        browser.until(
            "document.querySelector('.build-current')?.textContent.includes('第 3 / 3 件')"
        )
        assert browser.js("document.querySelector('#imageDialog').open")
        assert browser.js(
            "completedCrop === document.querySelector('[data-material-id=star] img')"
        )
        assert "失败/跳过 1 件" in browser.js(
            "document.querySelector('.build-counts').textContent"
        )
        browser.js("document.querySelector('#imageDialog button').click()")
        browser.js("document.querySelector('[data-stage-index=\"1\"]').click()")
        browser.until(
            "Boolean(document.querySelector('iframe')?.contentDocument?.querySelector('body.read-only'))"
        )
        browser.js("refreshCurrent(true)")
        assert browser.js("state.viewedStage") == 1
        browser.js("document.querySelector('#returnCurrentStep').click()")
        browser.until(
            "document.querySelector('.build-current')?.textContent.includes('第 3 / 3 件')"
        )
        browser.call("Page.reload")
        browser.until(
            "document.querySelector('.build-current')?.textContent.includes('第 3 / 3 件')"
        )
        assert "已完成 1 件" in browser.js(
            "document.querySelector('.build-counts').textContent"
        )
        provider.release["tape"].set()
        _wait_for_job(app, "live")
        browser.until("document.querySelector('.result-comparison img') !== null")
        browser.js("renderProject({...state.currentStatus, stage: 'reviewed', task: null})")
        assert browser.js("document.querySelector('#continueButton') !== null")
        assert browser.js("document.querySelector('#buildMaterials') === null")
        assert not browser.errors
        assert provider.calls == ["background", "star", "flower", "tape"]
    finally:
        provider.finish()
        if browser:
            browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
