"""Exercise direct transports, all four provider combinations, and switch semantics."""

import base64
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image, ImageDraw

from collage.core.errors import CollageError
from collage.core.io import read_json, sha256_file
from collage.projects import DataPaths
from collage.providers.intranet import IntranetSettings, IntranetVisionProvider
from collage.providers.qwen import QwenImageProvider, QwenSettings
from collage.providers.selection import (
    INTRANET_VISION,
    QWEN_IMAGE,
    YIBU_IMAGE,
    YIBU_VISION,
    cache_configuration,
)
from collage.providers.yibu import YibuVisionProvider, YibuSettings
from collage.schemas.draft_prompt import DRAFT_PROMPT
from collage.studio.workbench.application import WorkbenchApplication
from collage.studio.workbench.multipart import UploadedFile
from collage.studio.workbench.provider_settings import ProviderRuntimeSettings
from collage.template.review.feedback import review_revision
from test_workbench import _create_form, _manual_draft, _review_payload, _wait_for_job


def model_draft():
    draft = _manual_draft()
    draft["overlays"] = [
        {
            "id": "star",
            "label": "白色装饰",
            "source_rect": [2, 2, 12, 10],
            "target_rect": [2, 2, 12, 10],
            "attachment": None,
            "action": "reference_generate",
            "generation_brief": "一颗白色星星",
            "requires_exact_content": False,
            "review_notes": "",
        }
    ]
    draft["layer_order"].append({"type": "overlay", "id": "star"})
    return draft


@contextmanager
def model_server():
    """A loopback-only protocol simulator; no weights, customer data or external calls."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, body, status=200, mime="application/json"):
            if isinstance(body, dict):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            self.server.gets.append(self.path)
            if self.path == "/_yibu_audit/health":
                self.respond({"status": "ok", "upstream": "https://yibuapi.com"})
            elif self.path == "/health":
                self.respond(
                    {"status": "ok", "model": "Qwen-Image-Edit-2511", "busy": False}
                )
            else:
                self.respond({"error": "NOT_FOUND"}, 404)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.calls.append(
                (self.path, data, self.headers.get("Authorization"))
            )
            if self.path == "/v1/chat/completions":
                if self.server.chat_error:
                    self.respond({"error": "auth"}, self.server.chat_error)
                    return
                self.respond(
                    {
                        "id": "stub-chat",
                        "model": data["model"],
                        "choices": [
                            {
                                "finish_reason": self.server.finish_reason,
                                "message": {"content": json.dumps(self.server.draft)},
                            }
                        ],
                    }
                )
                return
            if self.path == "/edit" and self.server.busy_count:
                self.server.busy_count -= 1
                self.respond({"error": "MODEL_BUSY"}, 503)
                return
            if self.path not in {"/edit", "/v1/images/generations"}:
                self.respond({"error": "NOT_FOUND"}, 404)
                return
            with self.server.guard:
                self.server.active += 1
                self.server.peak = max(self.server.peak, self.server.active)
            try:
                time.sleep(self.server.delay)
                if self.server.bad_image:
                    self.respond(b"broken image", mime="image/png")
                    return
                if self.path == "/edit":
                    if set(data) != {"image_base64", "prompt", "seed"}:
                        self.respond({"error": "UNSUPPORTED_FIELDS"}, 422)
                        return
                    with Image.open(
                        io.BytesIO(base64.b64decode(data["image_base64"]))
                    ) as source:
                        size = source.size
                else:
                    source = data["image"]
                    if isinstance(source, list):
                        source = source[0]
                    with Image.open(
                        io.BytesIO(base64.b64decode(source.split(",")[1]))
                    ) as image:
                        size = image.size
                image = Image.new("RGB", size, "#00FF00")
                ImageDraw.Draw(image).ellipse(
                    (size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4),
                    fill="white",
                )
                output = io.BytesIO()
                image.save(output, format="PNG")
                if self.path == "/edit":
                    self.respond(output.getvalue(), mime="image/png")
                else:
                    self.respond(
                        {
                            "model": data["model"],
                            "data": [
                                {
                                    "b64_json": base64.b64encode(
                                        output.getvalue()
                                    ).decode()
                                }
                            ],
                        }
                    )
            finally:
                with self.server.guard:
                    self.server.active -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.calls, server.gets = [], []
    server.draft = model_draft()
    server.chat_error, server.busy_count = 0, 0
    server.finish_reason = "stop"
    server.delay, server.active, server.peak = 0, 0, 0
    server.bad_image = False
    server.guard = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    # Runtime settings add keys directly; isolate the whole mapping so they cannot
    # leak into later Yibu tests, including when a browser assertion fails.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for key in list(os.environ):
        if key.startswith(("COLLAGE_INTRANET_", "COLLAGE_QWEN_", "YIBU_")) or key in {
            "COLLAGE_VISION_PROVIDER",
            "COLLAGE_IMAGE_PROVIDER",
        }:
            del os.environ[key]


def configure(monkeypatch, base):
    for key, value in {
        "YIBU_API_KEY": "test-yibu-key",
        "YIBU_AUDIT_BASE_URL": base,
        "COLLAGE_INTRANET_VLM_BASE_URL": base + "/v1",
        "COLLAGE_INTRANET_VLM_API_KEY": "test-intranet-key",
        "COLLAGE_QWEN_IMAGE_BASE_URL": base,
        "COLLAGE_QWEN_IMAGE_SEED": "7",
    }.items():
        monkeypatch.setenv(key, value)


def generate(provider):
    return provider.make_overlay(
        Image.new("RGBA", (120, 80), "white"),
        brief="完整星星",
        background_mode="chroma_key",
        chroma_key=(0, 255, 0),
    )


def test_direct_vlm_reuses_prompt_without_yibu_auth_or_reasoning(monkeypatch):
    with model_server() as (server, base):
        configure(monkeypatch, base)
        monkeypatch.delenv("YIBU_API_KEY")
        provider = IntranetVisionProvider()
        result, audit = provider.analyze(
            b"reference",
            media_type="image/png",
            canvas={"width": 24, "height": 16},
            product_policy={},
            prompt=DRAFT_PROMPT,
        )
        path, payload, auth = server.calls[0]
        assert path == "/v1/chat/completions"
        assert auth == "Bearer test-intranet-key"
        assert payload["model"] == "Qwen/Qwen3.8-Flash-Next"
        assert payload["max_tokens"] == 16384 and "reasoning_effort" not in payload
        assert payload["messages"][0]["content"][0]["text"].count(DRAFT_PROMPT) == 1
        assert payload["messages"][0]["content"][1]["image_url"]["url"].endswith(
            base64.b64encode(b"reference").decode()
        )
        assert result == model_draft() and audit.actual_model == payload["model"]
        assert server.gets == []


@pytest.mark.parametrize(
    "failure,code",
    [("auth", "PROVIDER_AUTH_FAILED"), ("length", "PROVIDER_OUTPUT_TRUNCATED")],
)
def test_vlm_errors_do_not_silently_fallback(monkeypatch, failure, code):
    with model_server() as (server, base):
        configure(monkeypatch, base)
        if failure == "auth":
            server.chat_error = 401
        else:
            server.finish_reason = "length"
        with pytest.raises(CollageError) as error:
            IntranetVisionProvider().analyze(
                b"x", media_type="image/png", canvas={}, product_policy={}, prompt=""
            )
        assert error.value.code == code and len(server.calls) == 1


def test_qwen_mapping_raw_output_and_wire_contract():
    with model_server() as (server, base):
        provider = QwenImageProvider(QwenSettings(base, seed=8))
        reference = Image.new("RGBA", (132, 177), "#8899AA")
        result = provider.edit_background(
            reference, Image.new("L", reference.size), brief="保留纸纹"
        )
        assert result.image.size == reference.size
        assert result.raw_image.size != reference.size
        payload = server.calls[0][1]
        assert set(payload) == {"image_base64", "prompt", "seed"}
        assert "mask" not in payload and server.calls[0][2] is None
        overlay = generate(provider)
        assert overlay.image is overlay.raw_image
        assert overlay.audit.actual_model is None and overlay.audit.request_id is None
        assert not provider.capabilities.supports_transparency


def test_busy_is_retried_but_timeout_is_not(monkeypatch):
    monkeypatch.setattr("collage.providers.qwen._model_size", lambda size: size)
    with model_server() as (server, base):
        server.busy_count = 1
        generate(QwenImageProvider(QwenSettings(base, timeout_seconds=3)))
        assert len(server.calls) == 2
        server.calls.clear()
        server.delay = 0.2
        with pytest.raises(CollageError) as error:
            generate(QwenImageProvider(QwenSettings(base, timeout_seconds=0.05)))
        assert error.value.code == "PROVIDER_REQUEST_UNCERTAIN"
        assert error.value.details["request_state"] == "unknown"
        time.sleep(0.25)
        assert len(server.calls) == 1


def test_qwen_instances_serialize_and_preserve_request_seeds():
    with model_server() as (server, base):
        server.delay = 0.05
        providers = [QwenImageProvider(QwenSettings(base)) for _ in range(2)]
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(generate, providers))
        assert server.peak == 1 and len(server.calls) == 2
        assert results[0].transform["seed"] != results[1].transform["seed"]


def test_busy_budget_and_invalid_image_have_explicit_codes():
    with model_server() as (server, base):
        server.busy_count = 100
        with pytest.raises(CollageError) as error:
            generate(QwenImageProvider(QwenSettings(base, timeout_seconds=0.1)))
        assert error.value.code == "PROVIDER_BUSY"
        assert error.value.details["request_state"] == "not_started"
        server.busy_count, server.bad_image = 0, True
        with pytest.raises(CollageError) as error:
            generate(QwenImageProvider(QwenSettings(base)))
        assert error.value.code == "PROVIDER_INVALID_RESPONSE"


@pytest.mark.parametrize(
    "url", ["http://[invalid", "http://localhost:bad", "http://localhost:99999"]
)
def test_invalid_service_address_has_a_configuration_error(url):
    with pytest.raises(CollageError) as error:
        QwenImageProvider(QwenSettings(url))
    assert error.value.code == "PROVIDER_CONFIG_INVALID"


def test_cache_identity_isolated_without_changing_yibu_keys():
    first = QwenImageProvider(QwenSettings("http://127.0.0.1:10", seed=1))
    second = QwenImageProvider(QwenSettings("http://127.0.0.1:10", seed=2))
    third = QwenImageProvider(QwenSettings("http://127.0.0.1:11", seed=1))
    assert len({first.cache_identity, second.cache_identity, third.cache_identity}) == 3
    assert "127.0.0.1" not in json.dumps(cache_configuration(first))
    assert cache_configuration(YibuVisionProvider(YibuSettings(api_key="x"))) == {}


def test_runtime_configs_and_credentials_are_independent(monkeypatch):
    with model_server() as (_server, base):
        runtime = ProviderRuntimeSettings()
        old = runtime.configure(
            {"yibu_api_key": "old-private-key", "vlm_model": "kimi-k3"}
        )
        status = runtime.configure(
            {
                "default_vision_provider": INTRANET_VISION,
                "default_image_provider": QWEN_IMAGE,
                "intranet_base_url": base + "/v1",
                "intranet_api_key": "new-private-key",
                "qwen_base_url": base,
            }
        )
        assert status["vision"]["provider"] == INTRANET_VISION
        assert status["image"]["connection"]["state"] == "online"
        assert status["services"][YIBU_VISION]["model"] == old["vision"]["model"]
        assert "private-key" not in json.dumps(status)
        runtime.configure({"clear_credentials": True})
        assert IntranetSettings.from_env().api_key == "new-private-key"
        runtime.configure(
            {"yibu_api_key": "old-private-key", "clear_intranet_credentials": True}
        )
        assert os.environ["YIBU_API_KEY"] == "old-private-key"
        assert "COLLAGE_INTRANET_VLM_API_KEY" not in os.environ


@pytest.mark.parametrize("vision", [YIBU_VISION, INTRANET_VISION])
@pytest.mark.parametrize("image", [YIBU_IMAGE, QWEN_IMAGE])
def test_four_combinations_run_from_analysis_to_render(
    tmp_path, monkeypatch, vision, image
):
    with model_server() as (server, base):
        configure(monkeypatch, base)
        if vision == INTRANET_VISION and image == QWEN_IMAGE:
            monkeypatch.delenv("YIBU_API_KEY")
            monkeypatch.delenv("YIBU_AUDIT_BASE_URL")
        app = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
        form = _create_form("matrix")
        del form.files["manual_draft"]
        del form.files["background_candidate"]
        form.fields.update(vision_provider=vision, image_provider=image)
        app.start_project(form)
        _wait_for_job(app, "matrix")
        assert app.project_status("matrix")["stage"] == "awaiting_review"
        app.save_review("matrix", _review_payload(app, "matrix"))
        _wait_for_job(app, "matrix", timeout=15)
        assert app.project_status("matrix")["stage"] == "awaiting_approval"
        project = app.store.open("matrix")
        assert (project.renders / "result.png").is_file()
        if image == QWEN_IMAGE:
            assert (project.workspace / "background_raw.png").is_file()
        paths = [call[0] for call in server.calls]
        expected_image = "/edit" if image == QWEN_IMAGE else "/v1/images/generations"
        assert paths == ["/v1/chat/completions", expected_image, expected_image]
        assert app.project_status("matrix")["providers"] == {
            "vision_provider": vision,
            "image_provider": image,
        }
        template = read_json(project.template / "template.json")
        assert not any(item["skipped"] for item in template["build"]["warnings"])
        assert any(asset["id"] == "star" for asset in template["assets"])


def test_project_selection_feedback_regeneration_and_background_fork(
    tmp_path, monkeypatch
):
    with model_server() as (server, base):
        configure(monkeypatch, base)
        app = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
        form = _create_form("switch")
        form.files["manual_draft"] = UploadedFile(
            "draft.json", "application/json", json.dumps(model_draft()).encode()
        )
        app.start_project(form)
        _wait_for_job(app, "switch")
        app.configure_providers(
            {
                "default_vision_provider": INTRANET_VISION,
                "default_image_provider": QWEN_IMAGE,
            }
        )
        assert app.project_status("switch")["providers"]["image_provider"] == YIBU_IMAGE
        app.configure_project_providers(
            "switch", {"vision_provider": INTRANET_VISION, "image_provider": QWEN_IMAGE}
        )
        session = app.review_session("switch")
        app.revise_review(
            "switch",
            {
                "draft": session.draft,
                "revision": review_revision(session.draft),
                "question_resolutions": [],
                "other_feedback": "保留完整星星",
            },
        )
        _wait_for_job(app, "switch")
        assert server.calls[-1][2] == "Bearer test-intranet-key"
        app.save_review("switch", _review_payload(app, "switch"))
        _wait_for_job(app, "switch", timeout=15)
        source = app.store.open("switch")
        original = sha256_file(source.manifest)
        original_template = sha256_file(source.template / "template.json")
        status = app.project_status("switch")
        app.regenerate_overlay(
            "switch",
            {
                "overlay_id": "star",
                "revision": status["template_revision"],
                "image_provider": YIBU_IMAGE,
            },
        )
        job = _wait_for_job(app, "switch", timeout=15)
        target_id = job["result"]["project_id"]
        assert server.calls[-1][0] == "/v1/images/generations"
        assert (
            app.project_status(target_id)["providers"]["image_provider"] == YIBU_IMAGE
        )
        assert sha256_file(source.manifest) == original
        assert sha256_file(source.template / "template.json") == original_template
        fork = app.fork_background(target_id, app.background_revision(target_id))
        assert (
            app.project_status(fork["project_id"])["providers"]["image_provider"]
            == YIBU_IMAGE
        )
        app.configure_project_providers(
            fork["project_id"], {"image_provider": QWEN_IMAGE}
        )
        assert (
            app.project_status(target_id)["providers"]["image_provider"] == YIBU_IMAGE
        )
        with pytest.raises(CollageError, match="已有模板"):
            app.configure_project_providers("switch", {"image_provider": YIBU_IMAGE})


def test_defaults_and_busy_settings_do_not_change_active_jobs(tmp_path, monkeypatch):
    app = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
    monkeypatch.setenv("COLLAGE_VISION_PROVIDER", INTRANET_VISION)
    monkeypatch.setenv("COLLAGE_IMAGE_PROVIDER", QWEN_IMAGE)
    app.start_project(_create_form("defaults"))
    _wait_for_job(app, "defaults")
    assert app.project_status("defaults")["providers"] == {
        "vision_provider": INTRANET_VISION,
        "image_provider": QWEN_IMAGE,
    }
    entered, release = threading.Event(), threading.Event()
    app.jobs.submit("defaults", "test", lambda: (entered.set(), release.wait(3)))
    assert entered.wait(1)
    try:
        with pytest.raises(CollageError) as error:
            app.configure_providers({"default_image_provider": YIBU_IMAGE})
        assert error.value.code == "PROVIDER_SETTINGS_BUSY"
        with pytest.raises(CollageError) as error:
            app.configure_project_providers("defaults", {"image_provider": YIBU_IMAGE})
        assert error.value.code == "PROJECT_BUSY"
    finally:
        release.set()
        _wait_for_job(app, "defaults")


@pytest.mark.parametrize("request_state", ["not_started", "failed", "unknown"])
def test_overlay_attempts_recover_only_known_failures(tmp_path, request_state):
    from collage.providers import GeneratedImage, ImageCapabilities, ProviderAudit
    from collage.template.build.overlays import _provider_overlay
    from collage.core.state import NodeCache

    class Provider:
        capabilities = ImageCapabilities(
            "fixture", "fixture", True, True, True, "white_edit", fixture=True
        )
        failure = True
        calls = 0

        def make_overlay(self, *args, **kwargs):
            self.calls += 1
            if self.failure:
                raise CollageError(
                    "PROVIDER_REQUEST_FAILED",
                    "fixture",
                    details={"request_state": request_state},
                )
            raw = Image.new("RGBA", (100, 80))
            ImageDraw.Draw(raw).rectangle((20, 20, 79, 59), fill="white")
            return GeneratedImage(
                raw, ProviderAudit("fixture", "fixture", None, None, True, 0)
            )

    provider = Provider()
    cache = NodeCache(tmp_path / "cache")
    overlay = {
        "id": "star",
        "background_mode": "transparent",
        "generation_brief": "star",
        "chroma_key": None,
        "chroma_tolerance": 30,
    }
    reference = Image.new("RGBA", (100, 80), "white")
    with pytest.raises(CollageError):
        _provider_overlay(provider, reference, overlay, cache, "key")
    record = read_json(tmp_path / "overlay_attempts/key/0.json")
    assert record["status"] == (
        "uncertain" if request_state == "unknown" else request_state
    )
    provider.failure = False
    if request_state == "unknown":
        with pytest.raises(CollageError) as error:
            _provider_overlay(provider, reference, overlay, cache, "key")
        assert error.value.code == "OVERLAY_REQUEST_UNCERTAIN" and provider.calls == 1
    else:
        _provider_overlay(provider, reference, overlay, cache, "key")
        _provider_overlay(provider, reference, overlay, cache, "key")
        assert provider.calls == 2
        assert (tmp_path / "overlay_attempts/key/1_raw.png").is_file()


def test_qwen_background_preserves_protected_pixels(tmp_path, monkeypatch):
    with model_server() as (_server, base):
        configure(monkeypatch, base)
        app = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
        form = _create_form("protected")
        del form.files["background_candidate"]
        form.fields["image_provider"] = QWEN_IMAGE
        app.start_project(form)
        _wait_for_job(app, "protected")
        payload = _review_payload(app, "protected")
        mask = Image.new("RGBA", (24, 16))
        ImageDraw.Draw(mask).rectangle((0, 0, 11, 15), fill="white")
        encoded = io.BytesIO()
        mask.save(encoded, format="PNG")
        payload["mask_png"] = (
            "data:image/png;base64," + base64.b64encode(encoded.getvalue()).decode()
        )
        app.save_review("protected", payload)
        _wait_for_job(app, "protected", timeout=15)
        project = app.store.open("protected")
        with Image.open(project.workspace / "background.png") as output:
            assert output.getpixel((23, 15)) == (204, 136, 68, 255)
            assert output.getpixel((0, 0)) != (204, 136, 68, 255)
        assert (project.workspace / "background_raw.png").is_file()
