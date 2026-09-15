"""Verify read-only pipeline results, retained review visuals and reference comparison."""

import io
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from PIL import Image
from test_candidate_workflow import _project
from test_workbench import _wait_for_job

from collage.core.errors import CollageError
from collage.core.io import atomic_write_json, read_json, sha256_file
from collage.devtools.pipeline_ui_smoke import Browser
from collage.studio.workbench.pipeline import (
    pipeline_document,
    pipeline_image,
    review_snapshot,
)
from collage.studio.workbench.server import create_workbench_server


def test_saved_review_uses_confirmation_and_does_not_write(tmp_path, monkeypatch):
    app, provider, project = _project(tmp_path, monkeypatch)
    before = {p: sha256_file(p) for p in project.root.rglob("*") if p.is_file()}
    snapshot = review_snapshot(project, "confirmed").browser_state()
    confirmed = read_json(project.analysis / "ui_confirmed_draft.json")
    assert snapshot["draft"] == confirmed
    reviewed = read_json(project.review / "reviewed.json")
    for item in reviewed["overlays"]:
        assert (
            snapshot["review_options"]["overlays"][item["id"]]["rotation_deg"]
            == item["rotation_deg"]
        )
    assert snapshot["structure_preview"].startswith("data:image/png;base64,")
    assert len(provider.calls) == 2
    assert all(sha256_file(p) == digest for p, digest in before.items())
    assert set(before) == {p for p in project.root.rglob("*") if p.is_file()}
    # Later changes to the analysis file must not change the confirmed view.
    latest = read_json(project.analysis / "draft.json")
    latest["overlays"][0]["label"] = "later analysis"
    atomic_write_json(project.analysis / "draft.json", latest)
    assert review_snapshot(project, "confirmed").draft == confirmed
    assert (
        review_snapshot(project, "analysis").draft["overlays"][0]["label"]
        == "later analysis"
    )
    (project.analysis / "ui_confirmed_draft.json").unlink()
    assert not pipeline_document(project)["confirmed_available"]
    with pytest.raises(CollageError, match="未保存"):
        review_snapshot(project, "confirmed")


def test_material_images_follow_current_in_place_replacement(tmp_path, monkeypatch):
    app, provider, project = _project(tmp_path, monkeypatch, broken=True)
    initial = pipeline_document(project)
    assert initial["overlays"][0]["status"] == "missing"
    assert initial["overlays"][1]["status"] == "complete"
    assert set(initial["overlays"][1]["images"]) == {
        "reference",
        "raw",
        "processed",
        "final",
    }
    assert Image.open(
        io.BytesIO(pipeline_image(project, "overlay", "second", "raw"))
    ).size == (1000, 400)
    assert Image.open(
        io.BytesIO(pipeline_image(project, "overlay", "second", "final"))
    ).size == (602, 102)
    provider.broken = False
    app.regenerate_overlay(
        "candidate",
        {
            "overlay_id": "first",
            "revision": app.project_status("candidate")["template_revision"],
        },
    )
    _wait_for_job(app, "candidate")
    updated = pipeline_document(project)
    assert updated["overlays"][0]["status"] == "complete"
    assert set(updated["overlays"][0]["images"]) == {
        "reference",
        "raw",
        "processed",
        "final",
    }
    assert Image.open(
        io.BytesIO(pipeline_image(project, "overlay", "first", "raw"))
    ).size == (1000, 400)
    assert len(provider.calls) == 3
    for kind, identifier, variant in [
        ("overlay", "../project", "raw"),
        ("overlay", "first", "../../project.json"),
        ("file", "first", "final"),
    ]:
        with pytest.raises(CollageError) as error:
            pipeline_image(project, kind, identifier, variant)
        assert error.value.code == "ARTIFACT_NOT_FOUND"
    assert str(project.root) not in json.dumps(updated)


def test_customer_photos_are_read_from_the_saved_slot_binding(tmp_path, monkeypatch):
    from test_workbench import _image_bytes
    from collage.studio.workbench.multipart import MultipartForm, UploadedFile

    app, provider, project = _project(tmp_path, monkeypatch, photo=True)
    assert not pipeline_document(project)["slots"][0]["has_image"]
    app.submit_bindings(
        "candidate",
        MultipartForm(
            fields={"configuration": json.dumps({"slots": {}})},
            files={
                "image.photo": UploadedFile(
                    "photo.png", "image/png", _image_bytes("blue")
                )
            },
        ),
    )
    _wait_for_job(app, "candidate")
    slot = pipeline_document(project)["slots"][0]
    assert slot["has_image"] and slot["id"] == "photo"
    pixels = Image.open(
        io.BytesIO(pipeline_image(project, "slot", "photo", "input"))
    ).convert("RGB")
    assert pixels.getpixel((0, 0)) == (0, 0, 255)
    assert len(provider.calls) == 2


def test_old_material_raw_requires_unambiguous_request_record(tmp_path, monkeypatch):
    app, provider, project = _project(tmp_path, monkeypatch)
    transform_path = project.workspace / "overlay_first_transform.json"
    transform = read_json(transform_path)
    cache_key = transform.pop("evidence_cache_key")
    atomic_write_json(transform_path, transform)
    template_path = project.template / "template.json"
    template = read_json(template_path)
    audit = next(
        item
        for item in template["build"]["providers"]
        if item["node"] == "overlay:first"
    )
    audit["request_id"] = "saved-first-request"
    atomic_write_json(template_path, template)
    record_path = project.workspace / "overlay_attempts" / cache_key / "0.json"
    record = read_json(record_path)
    record["audit"]["request_id"] = "saved-first-request"
    atomic_write_json(record_path, record)
    assert "raw" in pipeline_document(project)["overlays"][0]["images"]
    record["audit"]["request_id"] = "unrelated-request"
    atomic_write_json(record_path, record)
    assert "raw" not in pipeline_document(project)["overlays"][0]["images"]
    assert len(provider.calls) == 2


def test_pipeline_http_is_read_only_and_serves_images(tmp_path, monkeypatch):
    app, provider, project = _project(tmp_path, monkeypatch)
    server = create_workbench_server(port=0, application=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(
            base + "/api/projects/candidate/pipeline"
        ) as response:
            data = json.load(response)
        with urllib.request.urlopen(
            base + data["overlays"][0]["images"]["raw"]
        ) as response:
            assert response.headers["Content-Type"] == "image/png"
            assert Image.open(io.BytesIO(response.read())).size == (1000, 400)
        with urllib.request.urlopen(
            base + "/api/projects/candidate/pipeline/review/confirmed/session"
        ) as response:
            assert json.load(response)["draft"] == read_json(
                project.analysis / "ui_confirmed_draft.json"
            )
        request = urllib.request.Request(
            base + "/api/projects/candidate/pipeline/review/confirmed/save",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(request)
        with urllib.request.urlopen(
            base + "/projects/candidate/review?view=confirmed"
        ) as response:
            assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
        with urllib.request.urlopen(base + "/projects/candidate/review") as response:
            assert response.headers["X-Frame-Options"] == "DENY"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(
                base + "/projects/invalid%22id/review?view=confirmed"
            )
        assert len(provider.calls) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_pipeline_browsing_and_comparison_in_browser(tmp_path, monkeypatch):
    pytest.importorskip("websocket")
    candidates = [
        Path(os.environ.get("FIGCOPY_TEST_BROWSER", "no-browser")),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    ]
    executable = next((p for p in candidates if p.is_file()), None)
    if executable is None:
        pytest.skip("Set FIGCOPY_TEST_BROWSER to a local Chromium executable")
    app, provider, project = _project(tmp_path, monkeypatch)
    server = create_workbench_server(port=0, application=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = None
    released = threading.Event()
    try:
        browser = Browser(executable, tmp_path / "browser")
        base = f"http://127.0.0.1:{server.server_address[1]}"
        browser.call("Page.navigate", {"url": base + "/projects/candidate"})
        browser.until(
            "document.querySelectorAll('.result-comparison img').length === 2 && Array.from(document.querySelectorAll('.result-comparison img')).every(i => i.complete && i.naturalWidth > 0)"
        )
        assert browser.js(
            "document.querySelector('.result-comparison img').src.includes('/artifacts/reference')"
        )
        browser.js("document.querySelector('.result-comparison a').click()")
        browser.until("document.querySelector('#imageDialog').open")
        browser.js("document.querySelector('#imageDialog button').click()")
        browser.js("document.querySelector('[data-stage-index=\"1\"]').click()")
        browser.until(
            "Boolean(document.querySelector('iframe')?.contentDocument?.querySelector('body.read-only'))"
        )
        assert browser.js(
            "document.querySelector('iframe').contentDocument.querySelector('#structurePreview').naturalWidth > 0"
        )
        assert browser.js(
            "document.querySelector('iframe').contentDocument.querySelector('#save').disabled"
        )
        assert (
            browser.js(
                "document.querySelector('iframe').contentDocument.querySelector('#view').width"
            )
            == 24
        )
        browser.js(
            "window.savedReviewFrame = document.querySelector('iframe'); window.frameDocument = savedReviewFrame.contentDocument"
        )
        browser.js("""window.savedApi = api;
            api = url => url.endsWith('/slots') ? new Promise((resolve, reject) => { window.rejectSlots = reject; }) : savedApi(url);
            window.delayedSlots = loadBindingForm(state.currentStatus); true;""")
        browser.js("rejectSlots(new Error('delayed slot failure')); delayedSlots")
        browser.js("api = savedApi")
        assert browser.js("savedReviewFrame === document.querySelector('iframe')")
        browser.js("document.querySelector('#actionPanel').scrollIntoView()")
        browser.screenshot(tmp_path / "draft-review.png")
        app.jobs.submit("candidate", "build", lambda: released.wait(timeout=40))
        browser.js("refreshCurrent(true)")
        browser.until("!document.querySelector('#taskBanner').hidden")
        assert browser.js("state.viewedStage") == 1
        assert browser.js(
            "window.savedReviewFrame === document.querySelector('iframe') && frameDocument === savedReviewFrame.contentDocument"
        )
        browser.call("Page.reload")
        browser.until(
            "document.querySelector('iframe') !== null && state.viewedStage === 1"
        )
        browser.js("document.querySelector('[data-stage-index=\"2\"]').click()")
        browser.until("document.querySelectorAll('.material-card').length === 2")
        browser.js("document.querySelector('[data-piece=first]').open = true")
        browser.until(
            "Array.from(document.querySelectorAll('[data-piece=first] img')).every(i => i.complete && i.naturalWidth > 0)"
        )
        assert (
            browser.js(
                "document.querySelector('[data-piece=first]').querySelectorAll('img').length"
            )
            == 4
        )
        released.set()
        _wait_for_job(app, "candidate")
        browser.js("refreshCurrent(true)")
        browser.until("document.querySelector('#taskBanner').hidden")
        assert browser.js("state.viewedStage") == 2
        assert browser.js("document.querySelector('[data-piece=first]').open")
        browser.screenshot(tmp_path / "materials.png")
        browser.js("document.querySelector('#returnCurrentStep').click()")
        browser.until("document.querySelector('#approvalForm') !== null")
        browser.js(
            "document.querySelector('#allowFixture').checked = true; document.querySelector('#approveButton').click()"
        )
        browser.until(
            "document.querySelector('#stageBadge').textContent.includes('已发布')"
        )
        browser.until(
            "document.querySelectorAll('.result-comparison img').length === 2"
        )
        browser.js(
            "document.querySelector('.result-comparison').scrollIntoView({block: 'center'})"
        )
        browser.screenshot(tmp_path / "comparison.png")
        assert len(provider.calls) == 2
        assert not browser.errors
    finally:
        released.set()
        if browser:
            browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
