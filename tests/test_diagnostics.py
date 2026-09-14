"""Verify failed model evidence, safe task history and diagnostic HTTP access with fixtures."""

from __future__ import annotations

import copy
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.request

import pytest
from PIL import Image

from collage.core.errors import CollageError
from collage.core.io import read_json, sha256_file
from collage.core.privacy import safe_value, safe_text
from collage.projects import DataPaths
from collage.providers.http import ServiceClient
from collage.providers.intranet import IntranetSettings, IntranetVisionProvider
from collage.studio.workbench.application import WorkbenchApplication
from collage.studio.workbench.jobs import JobRegistry
from collage.studio.workbench.server import create_workbench_server
from collage.template.analysis import analyze_reference
from collage.template.review.feedback import revise_draft, review_revision
from test_intranet_providers import model_draft
from test_workbench import _create_form


def wait_job(registry, project_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = registry.latest(project_id)
        if job and job["state"] not in {"running", "queued"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not complete")


def response_provider(monkeypatch, content, *, finish_reason="stop"):
    provider = IntranetVisionProvider(
        IntranetSettings("http://127.0.0.1:9", "diagnostic-private-key")
    )
    provider.fixture = True
    response = {
        "model": "fixture-model",
        "id": "fixture-request",
        "usage": {"completion_tokens": 20},
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
    }
    monkeypatch.setattr(provider._client, "post_json", lambda *a, **k: (response, {}))
    return provider


def invalid_draft():
    draft = model_draft()
    draft["overlays"][0]["generation_brief"] = None
    draft["questions"] = [{"question": f"question-{i}"} for i in range(6)]
    return draft


def reference(tmp_path):
    path = tmp_path / "reference.png"
    Image.new("RGB", (24, 16), "gray").save(path)
    return path


@pytest.mark.parametrize(
    "mode,code",
    [
        ("schema", "SPEC_VALIDATION_FAILED"),
        ("malformed", "PROVIDER_DRAFT_INCOMPLETE"),
        ("truncated", "PROVIDER_OUTPUT_TRUNCATED"),
        ("no_text", "PROVIDER_INVALID_RESPONSE"),
    ],
)
def test_failed_analysis_preserves_response_before_rejection(
    tmp_path, monkeypatch, mode, code
):
    content = json.dumps(invalid_draft()) if mode == "schema" else '{"slots": ['
    if mode == "no_text":
        content = None
    provider = response_provider(
        monkeypatch, content, finish_reason="length" if mode == "truncated" else "stop"
    )
    output = tmp_path / "analysis"
    with pytest.raises(CollageError) as caught:
        analyze_reference(reference(tmp_path), output, provider=provider)
    assert caught.value.code == code
    attempt = next((output / "attempts").iterdir())
    record = read_json(attempt / "attempt.json")
    assert record["status"] == "failed" and record["fixture"]
    assert record["id"] == caught.value.details["attempt_id"]
    assert (
        read_json(attempt / "response.json")["choices"][0]["message"]["content"]
        == content
    )
    assert (
        read_json(attempt / "response_metadata.json")["request_id"] == "fixture-request"
    )
    assert read_json(attempt / "validation.json")["error"]["code"] == code
    assert not (output / "draft.json").exists()
    assert not list((output / "analysis_cache").glob("*.json"))
    if mode == "schema":
        assert read_json(attempt / "candidate.json") == invalid_draft()
        assert len(record["error"]["details"]["issues"]) == 7


def test_retry_retains_valid_draft_and_does_not_cache_canvas_invalid_candidate(
    tmp_path, monkeypatch
):
    source = reference(tmp_path)
    output = tmp_path / "analysis"
    first = response_provider(monkeypatch, json.dumps(model_draft()))
    analyze_reference(source, output, provider=first)
    before = sha256_file(output / "draft.json")
    invalid = model_draft()
    invalid["overlays"][0]["source_rect"] = [100, 100, 50, 50]
    second = response_provider(monkeypatch, json.dumps(invalid))
    second.requested_model = "different-fixture"
    with pytest.raises(CollageError, match="Draft"):
        analyze_reference(source, output, provider=second, force=True)
    assert sha256_file(output / "draft.json") == before
    assert len(list((output / "attempts").iterdir())) == 2
    assert len(list((output / "analysis_cache").glob("*.json"))) == 1
    failed = [
        read_json(p)
        for p in (output / "attempts").glob("*/attempt.json")
        if read_json(p)["status"] == "failed"
    ][0]
    assert failed["phase"] == "validating_draft"


def test_auth_failure_and_non_json_transport_are_retained(tmp_path, monkeypatch):
    provider = response_provider(monkeypatch, "unused")

    def fail(*a, **k):
        raise CollageError(
            "PROVIDER_AUTH_FAILED", "HTTP 401", details={"http_status": 401}
        )

    monkeypatch.setattr(provider._client, "post_json", fail)
    output = tmp_path / "auth"
    with pytest.raises(CollageError):
        analyze_reference(reference(tmp_path), output, provider=provider)
    attempt = next((output / "attempts").iterdir())
    assert not (attempt / "candidate.json").exists()
    assert (
        read_json(attempt / "validation.json")["error"]["details"]["http_status"] == 401
    )
    provider = IntranetVisionProvider(
        IntranetSettings("http://127.0.0.1:9", "diagnostic-private-key")
    )
    monkeypatch.setattr(
        provider._client,
        "request",
        lambda *a, **k: (b"<html>upstream failure</html>", {}),
    )
    with pytest.raises(CollageError):
        analyze_reference(reference(tmp_path), tmp_path / "non-json", provider=provider)
    evidence = next(
        (tmp_path / "non-json" / "attempts").glob("*/transport_response.txt")
    )
    assert "upstream failure" in evidence.read_text()


@pytest.mark.parametrize("json_body", [True, False])
def test_http_500_preserves_safe_details(tmp_path, monkeypatch, json_body):
    secret = "diagnostic-private-key"
    body = {
        "error": "CUDA out of memory",
        "api_key": secret,
        "image_base64": "private-image",
        "path": "/data/customer/input.jpg",
    }
    raw = (
        json.dumps(body)
        if json_body
        else f"CUDA out of memory; Authorization: Bearer {secret}; /data/customer/input.jpg"
    )
    client = ServiceClient("http://127.0.0.1:9", 1, secret)

    def fail(*a, **k):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:9/edit",
            500,
            "error",
            {"x-request-id": "server-request"},
            io.BytesIO(raw.encode()),
        )

    monkeypatch.setattr(client._opener, "open", fail)
    with pytest.raises(CollageError) as caught:
        client.request("/edit", {"prompt": "fixture"}, operation="make-overlay")
    details = caught.value.details
    assert details["http_status"] == 500 and details["operation"] == "make-overlay"
    assert details["request_id"] == "server-request"
    assert "CUDA out of memory" in details["response"]
    assert secret not in details["response"]
    assert "private-image" not in details["response"]
    assert "/data/customer" not in details["response"]


def test_task_logs_are_isolated_incremental_and_survive_restart(tmp_path):
    registry = JobRegistry(tmp_path / "logs")
    barrier = threading.Barrier(2)

    def operation(name):
        def run():
            barrier.wait(timeout=5)
            logging.getLogger("collage.fixture").info(
                "unique-%s /data/private/photo.png", name
            )
            raise CollageError(
                "FIXTURE_FAILED",
                name,
                details={
                    "issues": [
                        {
                            "path": "$.questions[5]",
                            "message": "必须是字符串",
                            "code": "INVALID_TYPE",
                        }
                    ]
                },
            )

        return run

    for project in ("first", "second"):
        registry.submit(project, "create", operation(project))
    for project in ("first", "second"):
        job = wait_job(registry, project)
        payload = registry.logs.snapshot(project)
        messages = json.dumps(payload, ensure_ascii=False)
        assert f"unique-{project}" in messages
        assert f"unique-{'second' if project == 'first' else 'first'}" not in messages
        assert "/data/private" not in messages
        assert (
            registry.logs.snapshot(project, after=payload["job"]["last_seq"])["job"][
                "events"
            ]
            == []
        )
        restarted = JobRegistry(tmp_path / "logs")
        assert restarted.latest(project)["error"] == job["error"]
        assert (
            restarted.logs.snapshot(project)["job"]["events"]
            == payload["job"]["events"]
        )
    assert not logging.getLogger("collage").handlers


def test_interrupted_job_is_not_reported_running_after_restart(tmp_path):
    registry = JobRegistry(tmp_path / "logs")
    release = threading.Event()
    registry.submit("interrupted", "create", lambda: release.wait(timeout=5))
    try:
        restarted = JobRegistry(tmp_path / "logs")
        assert (
            restarted.latest("interrupted")["error"]["code"]
            == "WORKBENCH_JOB_INTERRUPTED"
        )
    finally:
        release.set()
        wait_job(registry, "interrupted")


def test_project_errors_and_diagnostics_survive_application_restart(
    tmp_path, monkeypatch
):
    provider = response_provider(monkeypatch, json.dumps(invalid_draft()))
    monkeypatch.setattr("collage.workflows.stages.load_provider", lambda *a: provider)
    app = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
    form = _create_form("invalid-model")
    del form.files["manual_draft"]
    app.start_project(form)
    job = wait_job(app.jobs, "invalid-model")
    assert job["state"] == "failed"
    restarted = WorkbenchApplication(app.paths)
    status = restarted.project_status("invalid-model")
    assert len(status["last_error"]["details"]["issues"]) == 7
    assert status["task"]["error"] == status["last_error"]
    payload = restarted.diagnostics("invalid-model")
    assert payload["attempts"][0]["job_id"] == job["id"]
    assert payload["attempts"][0]["files"]["candidate.json"].startswith(
        "/api/projects/"
    )
    assert str(tmp_path) not in json.dumps(payload)
    server = create_workbench_server(application=restarted, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/static/diagnostics.js") as response:
            assert b"refreshDiagnostics" in response.read()
        url = payload["attempts"][0]["files"]["candidate.json"]
        with urllib.request.urlopen(base + url) as response:
            assert response.headers["Content-Type"].startswith("text/plain")
            assert json.load(response) == invalid_draft()
        for suffix in ("?after=-1", "?after=abc", "?job_id=../project"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(
                    base + "/api/projects/invalid-model/diagnostics" + suffix
                )
        with pytest.raises(CollageError):
            restarted.diagnostic_file("invalid-model", "..", "project.json")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_evidence_redaction_preserves_field_types():
    value = {
        "generation_brief": None,
        "questions": [{"question": "test"}],
        "api_key": "private",
        "response": "data:image/png;base64,YWJjZGVm",
    }
    result = safe_value(copy.deepcopy(value))
    assert result["generation_brief"] is None
    assert result["questions"] == value["questions"]
    assert "private" not in json.dumps(result)
    assert "YWJjZGVm" not in json.dumps(result)
    assert "private-image" not in safe_text('{"image_base64":"private-image", broken')


def test_log_retention_reports_a_cursor_gap_without_unbounded_memory():
    registry = JobRegistry()
    registry.logs.update(
        {
            "id": "a" * 32,
            "project_id": "bounded",
            "state": "running",
            "created_at": "2026-09-14",
        }
    )
    for index in range(1003):
        registry.logs.append(
            "a" * 32,
            logging.LogRecord(
                "collage.fixture", logging.INFO, "", 0, "event-%s", (index,), None
            ),
        )
    snapshot = registry.logs.snapshot("bounded")["job"]
    assert len(snapshot["events"]) == 1000
    assert snapshot["truncated"] is True
    assert snapshot["events"][0]["seq"] == 4
    assert (
        registry.logs.snapshot("bounded", after=1001)["job"]["events"][0]["seq"] == 1002
    )


def test_log_disk_failure_does_not_turn_completed_work_into_a_retry(
    tmp_path, monkeypatch
):
    registry = JobRegistry(tmp_path / "logs")

    def unavailable(*a, **k):
        raise OSError("fixture full disk")

    monkeypatch.setattr(
        "collage.studio.workbench.job_logs.atomic_write_json", unavailable
    )
    registry.submit(
        "full-disk",
        "create",
        lambda: logging.getLogger("collage.fixture").info("completed-fixture-work"),
    )
    assert wait_job(registry, "full-disk")["state"] == "succeeded"
    assert (
        registry.logs.snapshot("full-disk")["job"]["log_error"]["code"]
        == "JOB_LOG_WRITE_FAILED"
    )


def test_correction_save_failure_is_not_recorded_as_success(tmp_path, monkeypatch):
    provider = response_provider(monkeypatch, json.dumps(model_draft()))
    output = tmp_path / "analysis"
    draft_path = analyze_reference(reference(tmp_path), output, provider=provider)
    draft = read_json(draft_path)
    before = sha256_file(draft_path)

    def fail_save(*a, **k):
        raise CollageError("FIXTURE_WRITE_FAILED", "fixture write failed")

    monkeypatch.setattr(
        "collage.template.review.feedback._persist_correction", fail_save
    )
    with pytest.raises(CollageError) as caught:
        revise_draft(
            draft_path,
            {
                "revision": review_revision(draft),
                "draft": draft,
                "other_feedback": "fixture correction",
                "question_resolutions": [],
            },
            provider=provider,
        )
    assert caught.value.code == "FIXTURE_WRITE_FAILED"
    attempt = read_json(
        output / "attempts" / caught.value.details["attempt_id"] / "attempt.json"
    )
    assert attempt["status"] == "failed" and attempt["phase"] == "saving_draft"
    assert sha256_file(draft_path) == before
