"""Regression checks for full backgrounds, complete stickers, and subject sizing."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from conftest import make_reviewed_spec
from collage.core.errors import CollageError
from collage.core.io import atomic_save_image, atomic_write_json, read_json
from collage.core.state import NodeCache
from collage.providers import GeneratedImage, ImageCapabilities, ProviderAudit
from collage.providers.yibu.image import _fit_generated_image
from collage.rendering.image_layer import _render_image_slot
from collage.rendering.model import PreparedBinding
from collage.template.build import build_template
from collage.template.build.overlays import _provider_overlay


@pytest.mark.parametrize("mode", ["protected", "full_candidate"])
def test_background_mode_controls_pixels_outside_remove_mask(
    tmp_path: Path, mode: str
) -> None:
    spec_path = make_reviewed_spec(tmp_path)
    spec = read_json(spec_path)
    spec["background"]["composition_mode"] = mode
    atomic_write_json(spec_path, spec)
    package = tmp_path / "template"
    build_template(spec_path, package, work_dir=tmp_path / "work")
    result = Image.open(package / "assets/background.png").convert("RGB")
    assert result.getpixel((12, 8)) == (136, 170, 204)
    assert result.getpixel((0, 0)) == (
        (136, 170, 204) if mode == "full_candidate" else (221, 208, 176)
    )


def test_full_background_cannot_override_an_explicit_protection_mask(
    tmp_path: Path,
) -> None:
    spec_path = make_reviewed_spec(tmp_path)
    spec = read_json(spec_path)
    atomic_save_image(Image.new("L", (24, 16), 0), tmp_path / "allowed.png")
    spec["background"].update(
        composition_mode="full_candidate", allowed_mask="allowed.png"
    )
    atomic_write_json(spec_path, spec)
    with pytest.raises(CollageError, match="allowed_mask") as caught:
        build_template(spec_path, tmp_path / "template")
    assert caught.value.code == "BACKGROUND_MODE_CONFLICT"


@pytest.mark.parametrize("fill", [(0, 0, 0, 0), (255, 0, 255, 255)])
def test_square_generated_sticker_keeps_top_and_bottom_content(
    fill: tuple[int, int, int, int],
) -> None:
    image = Image.new("RGBA", (100, 100), fill)
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 5, 59, 24), fill="red")
    draw.rectangle((40, 75, 59, 94), fill="blue")
    result, record = _fit_generated_image(
        image, (100, 40), preserve_content=True, fill=fill
    )
    pixels = [
        result.getpixel((x, y))
        for y in range(result.height)
        for x in range(result.width)
    ]
    assert any(r > 200 and g < 30 and b < 30 and a > 200 for r, g, b, a in pixels)
    assert any(b > 200 and r < 30 and g < 30 and a > 200 for r, g, b, a in pixels)
    assert result.getpixel((0, 20)) == fill
    assert "crop_box" not in record


class RepositioningOverlayProvider:
    """Simulate a model placing complete objects outside its input padding window."""

    capabilities = ImageCapabilities(
        name="test-repositioning",
        requested_model="test",
        supports_reference_image=True,
        supports_mask_edit=False,
        supports_transparency=True,
        mask_polarity="white_edit",
        output_sizes=((100, 100),),
        fixture=True,
    )

    def make_overlay(self, reference_crop: Image.Image, **kwargs) -> GeneratedImage:
        assert reference_crop.size == (100, 100)
        output = Image.new("RGBA", (100, 100))
        draw = ImageDraw.Draw(output)
        draw.rectangle((40, 5, 59, 24), fill="red")
        draw.rectangle((40, 75, 59, 94), fill="blue")
        return GeneratedImage(
            output, ProviderAudit("test", "test", "test", None, True, 0)
        )


def test_overlay_does_not_recrop_generated_content_using_input_padding(
    tmp_path: Path,
) -> None:
    overlay = {
        "id": "sticker",
        "background_mode": "alpha",
        "generation_brief": "two shapes",
        "chroma_tolerance": 40,
    }
    result, *_ = _provider_overlay(
        RepositioningOverlayProvider(),
        Image.new("RGBA", (100, 40)),
        overlay,
        NodeCache(tmp_path),
        "test-complete-content",
    )
    pixels = [
        result.getpixel((x, y))
        for y in range(result.height)
        for x in range(result.width)
    ]
    assert any(r > 200 and b < 30 and a > 200 for r, g, b, a in pixels)
    assert any(b > 200 and r < 30 and a > 200 for r, g, b, a in pixels)


def test_cutout_fits_visible_subject_and_preserves_requested_offset(
    tmp_path: Path,
) -> None:
    source = Image.new("RGBA", (100, 100))
    ImageDraw.Draw(source).rectangle((40, 20, 59, 59), fill="red")
    canvas = Image.new("RGBA", (100, 100))
    slot = {
        "id": "subject",
        "rect": [0, 0, 100, 100],
        "mode": "cutout",
        "fit": "contain",
        "anchor": [0.5, 0.5],
        "clip_mask_sha256": None,
        "clip_mask": None,
        "rotation_deg": 0,
        "edge_fade_px": 0,
    }
    _render_image_slot(
        tmp_path, canvas, slot, PreparedBinding(image=source, offset_px=(5, 0))
    )
    assert canvas.getchannel("A").getbbox() == (30, 0, 80, 100)
