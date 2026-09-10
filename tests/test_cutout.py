"""验证透明 PNG 直通、普通图片失败以及云端抠图显式授权。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from collage import cli
from collage.cutout import prepare_cutout
from collage.errors import CollageError
from collage.providers import ProviderAudit


class CloudCutout:
    name = "cloud-test"
    local_only = False

    def __init__(self) -> None:
        self.calls = 0

    def cutout(self, customer_image: Image.Image):
        self.calls += 1
        mask = Image.new("L", customer_image.size, 0)
        ImageDraw.Draw(mask).ellipse(
            (1, 1, customer_image.width - 2, customer_image.height - 2), fill=255
        )
        return mask, ProviderAudit(self.name, "test", "test", "cutout-1", True, 1)


def test_existing_transparent_png_needs_no_provider(tmp_path: Path) -> None:
    source = tmp_path / "transparent.png"
    image = Image.new("RGBA", (10, 10), (255, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((2, 2, 7, 7), fill=(255, 0, 0, 255))
    image.save(source)
    output = tmp_path / "out.png"
    prepare_cutout(source, output)
    assert Image.open(output).getchannel("A").getextrema() == (0, 255)
    assert output.with_suffix(".png.audit.json").is_file()


def test_cloud_cutout_requires_explicit_upload_authorization(tmp_path: Path) -> None:
    source = tmp_path / "opaque.png"
    Image.new("RGB", (10, 10), "red").save(source)
    provider = CloudCutout()
    with pytest.raises(CollageError) as caught:
        prepare_cutout(source, tmp_path / "out.png", provider=provider)
    assert caught.value.code == "CUSTOMER_UPLOAD_NOT_AUTHORIZED"
    assert provider.calls == 0
    prepare_cutout(
        source, tmp_path / "out.png", provider=provider, allow_cloud_upload=True
    )
    assert provider.calls == 1


def test_cutout_cli_uses_builtin_birefnet_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (4, 4), "red").save(source)
    loaded: list[str] = []
    provider = CloudCutout()

    def fake_load(spec: str, expected_protocol: object) -> CloudCutout:
        loaded.append(spec)
        return provider

    def fake_prepare(*args: object, **kwargs: object) -> Path:
        assert kwargs["provider"] is provider
        return output

    monkeypatch.setattr(cli, "load_provider", fake_load)
    monkeypatch.setattr(cli, "prepare_cutout", fake_prepare)
    args = cli._parser().parse_args(
        ["cutout", "--input", str(source), "--out", str(output)]
    )

    assert cli._run(args) == output
    assert loaded == [cli.DEFAULT_CUTOUT_PROVIDER]


def test_transparent_cli_input_does_not_read_birefnet_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "transparent.png"
    output = tmp_path / "output.png"
    image = Image.new("RGBA", (6, 6), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((2, 2, 3, 3), fill=(255, 0, 0, 255))
    image.save(source)
    monkeypatch.setenv(
        "COLLAGE_BIREFNET_MODEL_PATH", str(tmp_path / "missing-model")
    )
    args = cli._parser().parse_args(
        ["cutout", "--input", str(source), "--out", str(output)]
    )

    assert cli._run(args) == output
    assert Image.open(output).getchannel("A").getextrema() == (0, 255)
