"""验证 yibu Provider 的审计门禁、请求格式、响应解析和尺寸归一化。"""

from __future__ import annotations

import base64
import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from collage.core.errors import CollageError
from collage.providers.yibu import (
    YibuImageProvider,
    YibuSettings,
    YibuVisionProvider,
    _extract_generated_image,
    _parse_json_object,
    _validated_audit_base_url,
)


def _png_base64(size: tuple[int, int] = (32, 24)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, "#315B7D").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class _AuditStubHandler(BaseHTTPRequestHandler):
    """模拟审计代理，只记录测试请求且不访问网络。"""

    def _send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", "stub-header-id")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/_yibu_audit/health":
            self._send_json({"status": "ok", "upstream": "https://yibuapi.com"})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.server.records.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "headers": {
                    name.lower(): value for name, value in self.headers.items()
                },
                "payload": payload,
            }
        )
        if self.path == "/v1/chat/completions":
            chat_response = self.server.chat_response  # type: ignore[attr-defined]
            if chat_response is not None:
                self._send_json(chat_response)
                return
            draft = {
                "slots": [],
                "overlays": [],
                "background": {
                    "background_brief": "保留纸纹",
                    "review_notes": "测试",
                },
                "layer_order": [{"type": "background"}],
                "questions": [],
            }
            self._send_json(
                {
                    "id": "chat-response-id",
                    "model": "claude-opus-4-8",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": f"```json\n{json.dumps(draft)}\n```"
                            },
                        }
                    ],
                }
            )
            return
        if self.path.endswith(":generateContent"):
            self._send_json(
                {
                    "responseId": "image-response-id",
                    "modelVersion": "gemini-3-pro-image-preview",
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "inlineData": {
                                            "mimeType": "image/png",
                                            "data": self.server.image_base64,  # type: ignore[attr-defined]
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                }
            )
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _audit_stub() -> Iterator[tuple[ThreadingHTTPServer, str]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AuditStubHandler)
    server.records = []  # type: ignore[attr-defined]
    server.image_base64 = _png_base64()  # type: ignore[attr-defined]
    server.chat_response = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _settings(base_url: str) -> YibuSettings:
    return YibuSettings(
        api_key="unit-test-key",
        audit_base_url=base_url,
        timeout_seconds=5,
    )


def test_audit_base_url_rejects_direct_gateway() -> None:
    with pytest.raises(CollageError) as caught:
        _validated_audit_base_url("https://yibuapi.com")
    assert caught.value.code == "YIBU_AUDIT_PROXY_REQUIRED"


def test_settings_can_read_explicit_shared_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shared = tmp_path / "shared.py"
    shared.write_text(
        'def get_api_key():\n    return "shared-test-key"\n', encoding="utf-8"
    )
    monkeypatch.delenv("YIBU_API_KEY", raising=False)
    monkeypatch.setenv("YIBU_SHARED_PATH", str(shared))
    monkeypatch.setenv("YIBU_AUDIT_BASE_URL", "http://127.0.0.1:17860")
    monkeypatch.setenv("YIBU_VLM_MODEL", "opus-4.8")
    settings = YibuSettings.from_env()
    assert settings.api_key == "shared-test-key"
    assert "shared-test-key" not in repr(settings)
    assert settings.vlm_model == "claude-opus-4-8"
    assert settings.image_model == "gemini-3-pro-image-preview"


def test_settings_use_larger_max_reasoning_budget_for_kimi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YIBU_API_KEY", "unit-test-key")
    monkeypatch.setenv("YIBU_VLM_MODEL", "kimi-k3")
    monkeypatch.delenv("YIBU_VLM_MAX_TOKENS", raising=False)
    monkeypatch.delenv("YIBU_VLM_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("YIBU_TIMEOUT_SECONDS", raising=False)
    settings = YibuSettings.from_env()
    assert settings.vlm_model == "kimi-k3"
    assert settings.vlm_max_tokens == 16384
    assert settings.vlm_reasoning_effort == "max"
    assert settings.timeout_seconds == 900


def test_vision_provider_uses_audited_chat_endpoint() -> None:
    with _audit_stub() as (server, base_url):
        provider = YibuVisionProvider(_settings(base_url))
        draft, audit = provider.analyze(
            b"not-decoded-by-provider",
            media_type="image/png",
            canvas={"width": 20, "height": 30},
            product_policy={"goal": "test"},
            prompt="分析参考图",
        )
    records = server.records  # type: ignore[attr-defined]
    assert len(records) == 1
    request = records[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["authorization"] == "Bearer unit-test-key"
    assert request["payload"]["model"] == "claude-opus-4-8"
    image_url = request["payload"]["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    assert draft["layer_order"] == [{"type": "background"}]
    assert audit.actual_model == "claude-opus-4-8"
    assert audit.request_id == "chat-response-id"


def test_vision_provider_sends_kimi_reasoning_configuration() -> None:
    with _audit_stub() as (server, base_url):
        settings = YibuSettings(
            api_key="unit-test-key",
            audit_base_url=base_url,
            vlm_model="kimi-k3",
            vlm_max_tokens=16384,
            vlm_reasoning_effort="max",
            timeout_seconds=5,
        )
        YibuVisionProvider(settings).analyze(
            b"not-decoded-by-provider",
            media_type="image/png",
            canvas={"width": 20, "height": 30},
            product_policy={"goal": "test"},
            prompt="分析参考图",
        )
    request = server.records[0]  # type: ignore[attr-defined]
    assert request["payload"]["model"] == "kimi-k3"
    assert request["payload"]["max_tokens"] == 16384
    assert request["payload"]["reasoning_effort"] == "max"


def test_vision_provider_reports_truncated_response() -> None:
    with _audit_stub() as (server, base_url):
        server.chat_response = {  # type: ignore[attr-defined]
            "id": "truncated-response-id",
            "model": "kimi-k3",
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": '{"slots": [{"id": "slot_1"}]'},
                }
            ],
        }
        with pytest.raises(CollageError) as caught:
            YibuVisionProvider(_settings(base_url)).analyze(
                b"not-decoded-by-provider",
                media_type="image/png",
                canvas={"width": 20, "height": 30},
                product_policy={"goal": "test"},
                prompt="分析参考图",
            )
    assert caught.value.code == "PROVIDER_OUTPUT_TRUNCATED"
    assert caught.value.details["finish_reason"] == "length"


def test_draft_parser_does_not_mistake_nested_slot_for_full_draft() -> None:
    truncated = '{"slots": [{"id": "slot_1", "label": "主图"}]'
    with pytest.raises(CollageError) as caught:
        _parse_json_object(truncated)
    assert caught.value.code == "PROVIDER_DRAFT_INCOMPLETE"


def test_image_provider_uses_native_gemini_endpoint_and_restores_size() -> None:
    with _audit_stub() as (server, base_url):
        provider = YibuImageProvider(_settings(base_url))
        result = provider.edit_background(
            Image.new("RGB", (20, 30), "gray"),
            Image.new("L", (20, 30), 255),
            brief="移除旧照片",
        )
    records = server.records  # type: ignore[attr-defined]
    assert len(records) == 1
    request = records[0]
    assert request["path"] == (
        "/v1beta/models/gemini-3-pro-image-preview:generateContent"
    )
    assert request["headers"]["x-goog-api-key"] == "unit-test-key"
    config = request["payload"]["generationConfig"]
    assert config["responseModalities"] == ["TEXT", "IMAGE"]
    assert config["imageConfig"] == {"aspectRatio": "2:3", "imageSize": "1K"}
    assert len(request["payload"]["contents"][0]["parts"]) == 5
    assert result.image.size == (20, 30)
    assert result.transform is not None
    assert result.transform["kind"] == "center_crop_uniform_resize"
    assert result.audit.actual_model == "gemini-3-pro-image-preview"
    assert provider.capabilities.supports_transparency is False


def test_image_recitation_has_actionable_error() -> None:
    response = {
        "candidates": [
            {
                "content": {},
                "finishReason": "IMAGE_RECITATION",
                "finishMessage": "Unable to show the generated image.",
            }
        ]
    }
    with pytest.raises(CollageError) as caught:
        _extract_generated_image(response, timeout=1)
    assert caught.value.code == "PROVIDER_IMAGE_RECITATION"
    assert caught.value.details["finish_reason"] == "IMAGE_RECITATION"
