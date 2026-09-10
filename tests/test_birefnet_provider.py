"""验证 BiRefNet provider 的配置、延迟加载、审计与错误边界。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from collage.errors import CollageError
from collage.providers import CutoutProvider, load_provider
from collage.providers import birefnet as birefnet_module
from collage.providers.birefnet import (
    DEFAULT_MODEL_ID,
    BiRefNetLiteMattingProvider,
    BiRefNetSettings,
)


class _FakeBackend:
    device = "cpu"
    dtype_name = "float32"

    def __init__(self) -> None:
        self.calls = 0

    def predict_alpha(self, image: Image.Image) -> Image.Image:
        self.calls += 1
        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).ellipse(
            (1, 1, image.width - 2, image.height - 2), fill=255
        )
        return mask


def test_provider_is_loadable_without_importing_optional_runtime() -> None:
    provider = load_provider(
        "collage.providers.birefnet:BiRefNetLiteMattingProvider", CutoutProvider
    )
    assert isinstance(provider, BiRefNetLiteMattingProvider)
    assert provider.local_only is True
    assert provider._backend is None


def test_provider_lazily_reuses_backend_and_records_local_audit() -> None:
    backend = _FakeBackend()
    factory_calls = 0

    def factory(settings: BiRefNetSettings) -> _FakeBackend:
        nonlocal factory_calls
        factory_calls += 1
        assert settings.model_source == DEFAULT_MODEL_ID
        return backend

    provider = BiRefNetLiteMattingProvider(backend_factory=factory)
    image = Image.new("RGB", (12, 10), "white")

    first, first_audit = provider.cutout(image)
    second, second_audit = provider.cutout(image)

    assert factory_calls == 1
    assert backend.calls == 2
    assert first.mode == "L" and first.size == image.size
    assert second.getextrema() == (0, 255)
    assert first_audit.name == "local-birefnet-lite-matting"
    assert first_audit.requested_model == DEFAULT_MODEL_ID
    assert first_audit.actual_model == (
        f"{DEFAULT_MODEL_ID}@{provider.settings.model_revision}"
    )
    assert first_audit.fixture is False
    assert second_audit.elapsed_ms >= 0


def test_local_model_path_is_validated_and_not_exposed_in_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = tmp_path / "private-model-location" / "model"
    model_dir.mkdir(parents=True)
    monkeypatch.setenv("COLLAGE_BIREFNET_MODEL_PATH", str(model_dir))

    settings = BiRefNetSettings.from_env()

    assert settings.model_source == str(model_dir.resolve())
    assert settings.model_revision is None
    assert settings.local_files_only is True
    assert settings.model_label == "local:model"


def test_invalid_device_has_stable_business_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLLAGE_BIREFNET_DEVICE", "magic")
    with pytest.raises(CollageError) as caught:
        BiRefNetSettings.from_env()
    assert caught.value.code == "BIREFNET_CONFIG_INVALID"


def test_missing_local_model_has_stable_business_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "COLLAGE_BIREFNET_MODEL_PATH", str(tmp_path / "does-not-exist")
    )
    with pytest.raises(CollageError) as caught:
        BiRefNetSettings.from_env()
    assert caught.value.code == "BIREFNET_MODEL_NOT_FOUND"


def test_missing_optional_dependency_has_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_import(module_name: str):
        if module_name == "torch":
            raise ImportError("test")
        raise AssertionError("dependency check should stop at torch")

    monkeypatch.setattr(birefnet_module.importlib, "import_module", missing_import)
    with pytest.raises(CollageError) as caught:
        birefnet_module._import_runtime_dependencies()

    assert caught.value.code == "BIREFNET_DEPENDENCY_MISSING"
    assert caught.value.details["module"] == "torch"


def test_failure_reason_hides_home_workspace_and_configured_paths(
    tmp_path: Path,
) -> None:
    configured = str(tmp_path / "private-model")
    message = f"failed in {Path.home()} and {Path.cwd()} using {configured}"

    reason = birefnet_module._safe_failure_reason(RuntimeError(message), configured)

    assert str(Path.home()) not in reason
    assert str(Path.cwd()) not in reason
    assert configured not in reason
    assert "<home>" in reason
    assert "<workspace>" in reason
    assert "<configured-path>" in reason
