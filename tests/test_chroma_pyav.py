"""Verify PyAV keying on fine strokes, enclosed screen pixels, imported alpha and runtime failures."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from collage.core.errors import CollageError
from collage.imaging import chroma
from collage.imaging.operations import remove_chroma_background


def test_uneven_green_and_enclosed_holes_are_keyed_without_eroding_strokes():
    image = Image.new("RGB", (65, 49), (18, 206, 10))
    draw = ImageDraw.Draw(image)
    draw.rectangle((12, 10, 50, 36), outline="white", width=1)
    draw.line((5, 42, 59, 42), fill="white", width=1)
    draw.point((56, 8), fill="white")
    draw.line((14, 14, 20, 14), fill=(225, 250, 225), width=1)
    before = image.tobytes()

    output = remove_chroma_background(image, (0, 255, 0))

    assert output.size == image.size and output.mode == "RGBA"
    assert output.getpixel((0, 0))[3] == 0
    assert output.getpixel((30, 20))[3] == 0
    assert output.getpixel((12, 22)) == (255, 255, 255, 255)
    assert output.getpixel((30, 42)) == (255, 255, 255, 255)
    assert output.getpixel((56, 8)) == (255, 255, 255, 255)
    red, green, blue, alpha = output.getpixel((17, 14))
    assert green <= max(red, blue) and alpha == 255
    assert image.tobytes() == before


@pytest.mark.parametrize("size", [(1, 1), (5, 7), (17, 11), (66, 49)])
def test_non_aligned_frame_strides_keep_existing_transparency(size):
    image = Image.new("RGBA", size, (0, 255, 0, 255))
    image.putpixel((0, 0), (235, 235, 235, 128))
    if size != (1, 1):
        image.putpixel((size[0] - 1, size[1] - 1), (255, 255, 255, 0))

    output = remove_chroma_background(image, (0, 255, 0))

    assert output.size == size
    assert output.getpixel((0, 0)) == (235, 235, 235, 128)
    if size != (1, 1):
        assert output.getpixel((size[0] - 1, size[1] - 1))[3] == 0


@pytest.mark.parametrize(
    "key",
    [
        (255, 0, 255),
        (0, 255, 255),
        (255, 255, 0),
        (0, 255, 0),
        (255, 64, 0),
        (64, 0, 255),
    ],
)
def test_existing_key_palette_keeps_white_and_neutral_foreground(key):
    image = Image.new("RGB", (17, 13), key)
    image.putpixel((7, 6), (255, 255, 255))
    image.putpixel((9, 6), (80, 80, 80))

    output = remove_chroma_background(image, key)

    assert output.getpixel((0, 0))[3] == 0
    assert output.getpixel((7, 6)) == (255, 255, 255, 255)
    assert output.getpixel((9, 6)) == (80, 80, 80, 255)


def test_blue_key_uses_blue_despill_and_does_not_change_red():
    image = Image.new("RGB", (19, 15), (8, 12, 210))
    image.putpixel((8, 7), (225, 225, 255))
    image.putpixel((10, 7), (240, 40, 30))

    output = remove_chroma_background(image, (0, 0, 255))

    assert output.getpixel((0, 0))[3] == 0
    assert output.getpixel((8, 7)) == (225, 225, 225, 255)
    assert output.getpixel((10, 7)) == (240, 40, 30, 255)


def test_key_transition_has_soft_alpha():
    image = Image.new("RGB", (19, 15), (0, 200, 0))
    image.putpixel((9, 7), (50, 210, 50))

    output = remove_chroma_background(image, (0, 255, 0))

    assert 0 < output.getpixel((9, 7))[3] < 255


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tolerance": -1},
        {"softness": 256},
        {"hue_tolerance": 129},
        {"minimum_saturation": -1},
    ],
)
def test_invalid_settings_have_business_code(kwargs):
    with pytest.raises(CollageError) as caught:
        remove_chroma_background(Image.new("RGB", (3, 3)), (0, 255, 0), **kwargs)
    assert caught.value.code == "INVALID_CHROMA_SETTINGS"


def test_missing_dependency_has_safe_business_code(monkeypatch):
    def missing(name):
        raise ImportError("private runtime path")

    monkeypatch.setattr(chroma.importlib, "import_module", missing)
    with pytest.raises(CollageError) as caught:
        chroma.require_chroma_backend()
    assert caught.value.code == "CHROMA_DEPENDENCY_MISSING"
    assert "private runtime path" not in str(caught.value.as_dict())


def test_filter_execution_failure_has_safe_business_code(monkeypatch):
    class BrokenBackend:
        @staticmethod
        def VideoFrame(*args):
            raise ValueError("private runtime path")

    monkeypatch.setattr(chroma, "require_chroma_backend", lambda: BrokenBackend())
    with pytest.raises(CollageError) as caught:
        remove_chroma_background(Image.new("RGB", (3, 3), "green"), (0, 255, 0))
    assert caught.value.code == "CHROMA_PROCESSING_FAILED"
    assert "private runtime path" not in str(caught.value.as_dict())
