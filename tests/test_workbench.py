"""Exercise the browser workbench against the durable end-to-end workflow."""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from collage.projects import DataPaths
from collage.studio.workbench.application import WorkbenchApplication
from collage.studio.workbench.multipart import MultipartForm, UploadedFile
from collage.studio.workbench.server import create_workbench_server


def _image_bytes(color: str, *, size: tuple[int, int] = (24, 16)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _manual_draft(*, with_photo: bool = False) -> dict:
    slots = []
    layers = [{"type": "background"}]
    if with_photo:
        slots.append(
            {
                "id": "photo",
                "label": "客户照片",
                "type": "image",
                "mode": "photo",
                "source_rect": [2, 2, 12, 10],
                "target_rect": [2, 2, 12, 10],
                "upload_hint": "上传一张照片",
                "review_notes": "工作台测试",
            }
        )
        layers.append({"type": "slot", "id": "photo"})
    return {
        "slots": slots,
        "overlays": [],
        "background": {
            "background_brief": "延续纯色背景",
            "review_notes": "工作台测试",
        },
        "layer_order": layers,
        "questions": [],
    }


def _create_form(project_id: str, *, with_photo: bool = False) -> MultipartForm:
    return MultipartForm(
        fields={
            "project_id": project_id,
            "name": "工作台测试项目",
            "reviewer": "tester",
        },
        files={
            "reference": UploadedFile(
                "reference.png", "image/png", _image_bytes("#CC8844")
            ),
            "manual_draft": UploadedFile(
                "draft.json",
                "application/json",
                json.dumps(_manual_draft(with_photo=with_photo)).encode("utf-8"),
            ),
            "background_candidate": UploadedFile(
                "background.png", "image/png", _image_bytes("#DDBB88")
            ),
        },
    )


def _review_payload(application: WorkbenchApplication, project_id: str) -> dict:
    session = application.review_session(project_id)
    mask_buffer = io.BytesIO()
    Image.new("RGBA", session.canvas_size, (255, 255, 255, 255)).save(
        mask_buffer, format="PNG"
    )
    draft = json.loads(json.dumps(session.draft))
    draft["questions"] = []
    return {
        "draft": draft,
        "mask_png": "data:image/png;base64,"
        + base64.b64encode(mask_buffer.getvalue()).decode("ascii"),
        "slot_overrides": session.review_options["slots"],
        "overlay_overrides": session.review_options["overlays"],
        "revision": session.review_options["revision"],
        "final_confirmed": True,
        "empty_mask_approved": False,
        "background_expand_px": 0,
        "background_feather_px": 0,
    }


def _wait_for_job(
    application: WorkbenchApplication, project_id: str, timeout: float = 8
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = application.latest_job(project_id)
        if job is not None and job["state"] not in {"queued", "running"}:
            assert job["state"] == "succeeded", job
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {project_id}")


def test_workbench_application_handles_customer_slots_without_exposing_paths(
    tmp_path: Path,
) -> None:
    application = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
    application.start_project(_create_form("photo-flow", with_photo=True))
    _wait_for_job(application, "photo-flow")
    assert application.project_status("photo-flow")["stage"] == "awaiting_review"

    application.save_review("photo-flow", _review_payload(application, "photo-flow"))
    _wait_for_job(application, "photo-flow")
    waiting = application.project_status("photo-flow")
    assert waiting["stage"] == "awaiting_bindings"
    assert "root" not in waiting
    assert "path" not in waiting["artifacts"]["draft"]
    assert application.project_slots("photo-flow")[0]["id"] == "photo"

    configuration = {"slots": {"photo": {"scale": 1.1, "offset_px": [1, -1]}}}
    application.submit_bindings(
        "photo-flow",
        MultipartForm(
            fields={"configuration": json.dumps(configuration)},
            files={
                "image.photo": UploadedFile(
                    "../../../customer.png",
                    "image/png",
                    _image_bytes("#2288CC", size=(20, 20)),
                )
            },
        ),
    )
    _wait_for_job(application, "photo-flow")
    rendered = application.project_status("photo-flow")
    assert rendered["stage"] == "awaiting_approval"
    assert rendered["artifacts"]["render"]["exists"] is True
    project = application.store.open("photo-flow")
    assert (project.inputs / "customer" / "001_image.png").is_file()
    assert not any(application.paths.cache.iterdir())

    # Reframing an imported image rerenders without forcing a duplicate upload.
    application.submit_bindings(
        "photo-flow",
        MultipartForm(
            fields={
                "configuration": json.dumps(
                    {"slots": {"photo": {"scale": 1.2, "offset_px": [0, 0]}}}
                )
            },
            files={},
        ),
    )
    _wait_for_job(application, "photo-flow")
    assert application.project_status("photo-flow")["stage"] == "awaiting_approval"


def _multipart_body(
    fields: dict[str, str], files: dict[str, tuple[str, str, bytes]]
) -> tuple[str, bytes]:
    boundary = "----figcopy-test-boundary"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, (filename, content_type, data) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                data,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def _request_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: object | None = None,
    content_type: str = "application/json",
    raw_body: bytes | None = None,
) -> tuple[int, dict]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["X-Figcopy-Token"] = token
    body = raw_body
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if body is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _wait_for_http_job(base_url: str, project_id: str) -> dict:
    for _ in range(300):
        _status, payload = _request_json(f"{base_url}/api/tasks/{project_id}")
        task = payload["task"]
        if task is not None and task["state"] not in {"queued", "running"}:
            assert task["state"] == "succeeded", task
            return task
        time.sleep(0.02)
    raise AssertionError("HTTP job did not finish")


def test_workbench_http_api_runs_both_human_gates(tmp_path: Path) -> None:
    server = create_workbench_server(tmp_path / "data", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base_url, timeout=5) as response:
            page = response.read().decode("utf-8")
            assert "Figcopy 工作台" in page
            assert "Content-Security-Policy" in response.headers
        match = re.search(r'name="figcopy-csrf-token" content="([^"]+)"', page)
        assert match is not None
        token = match.group(1)

        content_type, body = _multipart_body(
            {
                "project_id": "web-flow",
                "name": "浏览器全流程",
                "reviewer": "tester",
            },
            {
                "reference": ("reference.png", "image/png", _image_bytes("#CC8844")),
                "manual_draft": (
                    "manual.json",
                    "application/json",
                    json.dumps(_manual_draft()).encode("utf-8"),
                ),
                "background_candidate": (
                    "background.png",
                    "image/png",
                    _image_bytes("#DDBB88"),
                ),
            },
        )
        denied, error = _request_json(
            f"{base_url}/api/projects",
            method="POST",
            raw_body=body,
            content_type=content_type,
        )
        assert denied == 403
        assert error["code"] == "INVALID_CSRF_TOKEN"

        accepted, _result = _request_json(
            f"{base_url}/api/projects",
            method="POST",
            token=token,
            raw_body=body,
            content_type=content_type,
        )
        assert accepted == 202
        _wait_for_http_job(base_url, "web-flow")
        ok, status = _request_json(f"{base_url}/api/projects/web-flow")
        assert ok == 200
        assert status["stage"] == "awaiting_review"

        with urllib.request.urlopen(f"{base_url}/projects/web-flow/review") as response:
            review_page = response.read()
        assert b"/api/projects/web-flow/review" in review_page
        _ok, draft = _request_json(f"{base_url}/api/projects/web-flow/review/draft")
        _ok, options = _request_json(
            f"{base_url}/api/projects/web-flow/review/review-options"
        )
        mask_buffer = io.BytesIO()
        Image.new("RGBA", (24, 16), (255, 255, 255, 255)).save(
            mask_buffer, format="PNG"
        )
        draft["questions"] = []
        saved, result = _request_json(
            f"{base_url}/api/projects/web-flow/review/save",
            method="POST",
            token=token,
            payload={
                "draft": draft,
                "mask_png": "data:image/png;base64,"
                + base64.b64encode(mask_buffer.getvalue()).decode("ascii"),
                "slot_overrides": options["slots"],
                "overlay_overrides": options["overlays"],
                "revision": options["revision"],
                "final_confirmed": True,
                "empty_mask_approved": False,
                "background_expand_px": 0,
                "background_feather_px": 0,
            },
        )
        assert saved == 200
        assert result["path"] == "review/reviewed.json"
        _wait_for_http_job(base_url, "web-flow")
        _ok, status = _request_json(f"{base_url}/api/projects/web-flow")
        assert status["stage"] == "awaiting_approval"

        with urllib.request.urlopen(
            f"{base_url}/api/projects/web-flow/artifacts/render"
        ) as response:
            assert response.headers.get_content_type() == "image/png"
            assert Image.open(io.BytesIO(response.read())).size == (24, 16)

        approved, _result = _request_json(
            f"{base_url}/api/projects/web-flow/approve",
            method="POST",
            token=token,
            payload={"notes": "已在工作台检查", "allow_fixture": False},
        )
        assert approved == 202
        _wait_for_http_job(base_url, "web-flow")
        _ok, status = _request_json(f"{base_url}/api/projects/web-flow")
        assert status["stage"] == "complete"
        assert status["status"] == "ready"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
