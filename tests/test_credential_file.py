"""Check JSON credential loading without disclosing private keys or changing routing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collage.core.errors import CollageError
from collage.providers.yibu.settings import YibuSettings
from collage.studio.workbench import provider_settings


@pytest.fixture
def credential_file(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "credentials.local.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "test",
                "base_url": "https://must-not-bypass-audit.invalid",
                "api_keys": ["fixture-private-key"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("YIBU_API_KEY", raising=False)
    monkeypatch.delenv("YIBU_SHARED_PATH", raising=False)
    monkeypatch.setenv("YIBU_CREDENTIALS_FILE", str(path))
    monkeypatch.setenv("YIBU_AUDIT_BASE_URL", "http://127.0.0.1:17860")
    return path


def test_json_file_keeps_secret_private_and_uses_audit_proxy(
    credential_file: Path, monkeypatch
) -> None:
    settings = YibuSettings.from_env()
    assert settings.api_key == "fixture-private-key"
    assert settings.audit_base_url == "http://127.0.0.1:17860"
    assert "fixture-private-key" not in repr(settings)
    monkeypatch.setenv("YIBU_API_KEY", "explicit-test-key")
    assert YibuSettings.from_env().api_key == "explicit-test-key"


@pytest.mark.parametrize(
    "body", ['{"api_keys":[]}', '{"api_keys":[null]}', "private malformed json"]
)
def test_invalid_file_errors_do_not_expose_contents(
    credential_file: Path, body: str
) -> None:
    credential_file.write_text(body, encoding="utf-8")
    with pytest.raises(CollageError) as caught:
        YibuSettings.from_env()
    assert caught.value.code == "YIBU_CREDENTIAL_FILE_INVALID"
    assert body not in str(caught.value)
    assert str(credential_file) not in str(caught.value)


def test_workbench_reports_file_and_clears_its_reference(
    credential_file: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        provider_settings, "_audit_health", lambda url: {"state": "not_checked"}
    )
    runtime = provider_settings.ProviderRuntimeSettings()
    status = runtime.status()
    assert status["credential"]["configured"] is True
    assert status["credential"]["source"] == "启动环境中的 JSON 凭据文件"
    assert str(credential_file) not in json.dumps(status)
    assert "fixture-private-key" not in json.dumps(status)
    status = runtime.configure({"clear_credentials": True})
    assert status["credential"]["configured"] is False
    assert credential_file.is_file()
