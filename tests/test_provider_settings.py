"""Verify safe, process-only Provider configuration for the browser workbench."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import pytest

from collage.core.errors import CollageError
from collage.studio.workbench.provider_settings import ProviderRuntimeSettings
from collage.studio.workbench.server import create_workbench_server

_PROVIDER_ENVIRONMENT = (
    "YIBU_API_KEY",
    "YIBU_SHARED_PATH",
    "YIBU_CREDENTIALS_FILE",
    "YIBU_AUDIT_BASE_URL",
    "YIBU_VLM_MODEL",
    "YIBU_VLM_MAX_TOKENS",
    "YIBU_VLM_REASONING_EFFORT",
    "YIBU_IMAGE_MODEL",
    "YIBU_IMAGE_SIZE",
    "YIBU_TIMEOUT_SECONDS",
    "COLLAGE_BIREFNET_DEVICE",
    "COLLAGE_BIREFNET_MODEL_PATH",
)


@pytest.fixture(autouse=True)
def isolated_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PROVIDER_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(("model", "expected"), [("kimi-k3", "high"), ("opus-4.8", "")])
def test_runtime_reasoning_default_matches_selected_model(
    monkeypatch: pytest.MonkeyPatch, model: str, expected: str
) -> None:
    monkeypatch.setenv("YIBU_VLM_MODEL", model)
    status = ProviderRuntimeSettings().status()
    assert status["vision"]["reasoning_effort"] == expected
    assert status["image"]["model"] == "doubao-seedream-5-0-260128"


def test_runtime_key_is_never_returned_and_can_be_cleared() -> None:
    runtime = ProviderRuntimeSettings()
    secret = "sk-ui-test-secret-do-not-persist"

    status = runtime.configure(
        {
            "yibu_api_key": secret,
            "vlm_model": "kimi-k3",
            "vlm_max_tokens": "16384",
            "vlm_reasoning_effort": "max",
            "image_model": "doubao-seedream-5-0-260128",
            "image_size": "1K",
            "timeout_seconds": "900",
            "birefnet_device": "cpu",
        }
    )

    serialized = json.dumps(status, ensure_ascii=False)
    assert secret not in serialized
    assert "yibu_api_key" not in serialized.lower()
    assert status["credential"] == {
        "configured": True,
        "source": "当前工作台内存 Key",
        "shared_path_configured": False,
    }
    assert status["vision"]["model"] == "kimi-k3"
    assert status["vision"]["max_tokens"] == "16384"
    assert status["vision"]["reasoning_effort"] == "max"
    assert status["vision"]["timeout_seconds"] == "900"
    assert os.environ["YIBU_API_KEY"] == secret

    cleared = runtime.configure({"clear_credentials": True})
    assert cleared["credential"]["configured"] is False
    assert "YIBU_API_KEY" not in os.environ
    assert "YIBU_SHARED_PATH" not in os.environ


def test_shared_path_switches_away_from_an_existing_session_key(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared.py"
    shared.write_text(
        "def get_api_key():\n    return 'shared-secret-not-returned'\n",
        encoding="utf-8",
    )
    runtime = ProviderRuntimeSettings()
    runtime.configure({"yibu_api_key": "direct-secret"})

    status = runtime.configure({"shared_path": str(shared)})

    assert "YIBU_API_KEY" not in os.environ
    assert os.environ["YIBU_SHARED_PATH"] == str(shared.resolve())
    assert status["credential"]["source"] == "shared.py"
    serialized = json.dumps(status, ensure_ascii=False)
    assert "direct-secret" not in serialized
    assert "shared-secret-not-returned" not in serialized


def test_invalid_shared_path_rolls_back_existing_settings(tmp_path: Path) -> None:
    runtime = ProviderRuntimeSettings()
    runtime.configure({"yibu_api_key": "keep-this-secret", "vlm_model": "kimi-k3"})

    with pytest.raises(CollageError, match="shared.py"):
        runtime.configure({"shared_path": str(tmp_path / "missing.py")})

    assert os.environ["YIBU_API_KEY"] == "keep-this-secret"
    assert os.environ["YIBU_VLM_MODEL"] == "kimi-k3"
    assert runtime.status()["credential"]["source"] == "当前工作台内存 Key"


def test_local_cutout_model_is_ready_only_when_required_files_exist(
    tmp_path: Path,
) -> None:
    model = tmp_path / "birefnet-model"
    model.mkdir()
    runtime = ProviderRuntimeSettings()

    status = runtime.configure({"birefnet_model_path": str(model)})
    assert status["cutout"]["model_cached"] is False

    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "model.safetensors").write_bytes(b"test-weight-placeholder")
    assert runtime.status()["cutout"]["model_cached"] is True


class _TokenParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None
        self.key_input_type: str | None = None
        self.key_input_autocomplete: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "figcopy-csrf-token":
            self.token = values.get("content")
        if tag == "input" and values.get("id") == "yibuApiKey":
            self.key_input_type = values.get("type")
            self.key_input_autocomplete = values.get("autocomplete")


def _json_request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: object | None = None,
) -> tuple[int, dict]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["X-Figcopy-Token"] = token
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_provider_settings_require_csrf_and_do_not_persist_key(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    server = create_workbench_server(data_dir, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    secret = "sk-http-test-secret-do-not-persist"
    try:
        with urllib.request.urlopen(base_url, timeout=5) as response:
            page = response.read().decode("utf-8")
        parser = _TokenParser()
        parser.feed(page)
        assert parser.token is not None
        assert "yibuApiKey" in page
        assert parser.key_input_type == "password"
        assert parser.key_input_autocomplete == "off"

        ok, initial = _json_request(base_url + "/api/provider-settings")
        assert ok == 200
        assert initial["credential"]["configured"] is False
        assert initial["vision"]["model"] == "kimi-k3"
        assert initial["vision"]["reasoning_effort"] == "high"

        denied, error = _json_request(
            base_url + "/api/provider-settings",
            method="POST",
            payload={"yibu_api_key": secret},
        )
        assert denied == 403
        assert error["code"] == "INVALID_CSRF_TOKEN"

        saved, status = _json_request(
            base_url + "/api/provider-settings",
            method="POST",
            token=parser.token,
            payload={
                "yibu_api_key": secret,
                "vlm_model": "kimi-k3",
                "image_model": "doubao-seedream-5-0-260128",
            },
        )
        assert saved == 200
        assert status["credential"]["configured"] is True
        assert status["vision"]["reasoning_effort"] == "high"
        assert secret not in json.dumps(status)

        fetched, status = _json_request(base_url + "/api/provider-settings")
        assert fetched == 200
        assert status["credential"]["source"] == "当前工作台内存 Key"
        assert secret not in json.dumps(status)
        for path in data_dir.rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
