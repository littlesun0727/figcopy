"""验证几何变换、EXIF、遮罩极性与非编辑区像素保护。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from collage.core.errors import CollageError
from collage.imaging.geometry import (
    contain_transform,
    pad_for_model,
    restore_from_model,
)
from collage.imaging.operations import (
    alpha_is_meaningful,
    choose_chroma_key,
    chroma_alpha_is_clean,
    clean_chroma_edges,
    convert_remove_mask_polarity,
    make_blend_mask,
    normalize_image,
    protected_background_compose,
    remove_chroma_background,
    remove_chroma_key,
)


def test_contain_mapping_round_trip_and_size_guard() -> None:
    transform = contain_transform((300, 100), (256, 256))
    rect = [17.25, 8.5, 90.0, 22.0]
    restored = transform.inverse_rect(transform.map_rect(rect))
    assert restored == pytest.approx(rect)
    padded, recorded = pad_for_model(Image.new("RGBA", (300, 100), "red"), (256, 256))
    assert padded.size == (256, 256)
    assert restore_from_model(padded, recorded).size == (300, 100)
    with pytest.raises(CollageError, match="尺寸") as caught:
        restore_from_model(Image.new("RGBA", (255, 256)), recorded)
    assert caught.value.code == "PROVIDER_SIZE_MISMATCH"


def test_mask_polarity_and_protected_pixels_are_exact() -> None:
    core = Image.new("L", (7, 5), 0)
    core.putpixel((3, 2), 255)
    assert convert_remove_mask_polarity(core, "white_edit").getpixel((3, 2)) == 255
    assert convert_remove_mask_polarity(core, "white_preserve").getpixel((3, 2)) == 0
    allowed = Image.new("L", core.size, 0)
    for y in range(1, 4):
        for x in range(1, 6):
            allowed.putpixel((x, y), 255)
    blend = make_blend_mask(core, allowed_mask=allowed, expand_px=1, feather_px=1)
    source = Image.new("RGBA", core.size, (12, 34, 56, 255))
    generated = Image.new("RGBA", core.size, (220, 180, 140, 255))
    result = protected_background_compose(source, generated, blend)
    assert result.getpixel((0, 0)) == source.getpixel((0, 0))
    assert result.getpixel((3, 2)) == generated.getpixel((3, 2))


def test_core_outside_allowed_is_rejected() -> None:
    core = Image.new("L", (3, 3), 0)
    core.putpixel((1, 1), 255)
    allowed = Image.new("L", (3, 3), 0)
    with pytest.raises(CollageError) as caught:
        make_blend_mask(core, allowed_mask=allowed)
    assert caught.value.code == "CORE_OUTSIDE_ALLOWED_MASK"


def test_alpha_and_adaptive_chroma_key() -> None:
    opaque = Image.new("RGBA", (4, 4), (0, 255, 0, 255))
    assert not alpha_is_meaningful(opaque)
    key = choose_chroma_key(opaque)
    assert key != (0, 255, 0)
    keyed = Image.new("RGB", (5, 5), key)
    keyed.putpixel((2, 2), (20, 30, 40))
    transparent = remove_chroma_key(keyed, key, tolerance=10)
    assert transparent.getpixel((0, 0))[3] == 0
    assert transparent.getpixel((2, 2))[3] == 255
    assert alpha_is_meaningful(transparent)


def test_textured_chroma_background_including_enclosed_key_is_removed() -> None:
    key = (0, 255, 0)
    image = Image.new("RGB", (24, 18), (0, 72, 0))
    for y in range(image.height):
        for x in range(image.width):
            green = 55 + ((x * 17 + y * 11) % 115)
            image.putpixel((x, y), (3, green, 8))
    for y in range(4, 14):
        for x in range(5, 19):
            image.putpixel((x, y), (245, 185, 205))
    image.putpixel((12, 9), (0, 72, 0))

    cleaned = remove_chroma_background(image, key, tolerance=20)

    assert cleaned.getpixel((0, 0))[3] == 0
    assert cleaned.getpixel((23, 17))[3] == 0
    assert cleaned.getpixel((6, 5))[3] == 255
    # A matching screen pixel is keyed even when a foreground outline encloses it.
    assert cleaned.getpixel((12, 9))[3] == 0
    assert chroma_alpha_is_clean(cleaned)


def test_chroma_quality_gate_rejects_tiny_or_only_partial_transparency() -> None:
    dirty = Image.new("RGBA", (20, 12), (0, 120, 0, 128))
    dirty.putpixel((0, 0), (0, 120, 0, 0))
    dirty.putpixel((10, 6), (240, 180, 200, 255))

    assert alpha_is_meaningful(dirty)
    assert not chroma_alpha_is_clean(dirty)


def test_chroma_edge_cleanup_erodes_alpha_without_changing_rgb() -> None:
    image = Image.new("RGBA", (9, 9), (255, 0, 255, 0))
    for y in range(1, 8):
        for x in range(1, 8):
            alpha = 128 if x in {1, 7} or y in {1, 7} else 255
            image.putpixel((x, y), (40, 80, 120, alpha))

    cleaned = clean_chroma_edges(image, inset_px=1)

    assert cleaned.getpixel((1, 4)) == (40, 80, 120, 0)
    assert cleaned.getpixel((4, 4)) == (40, 80, 120, 255)


def test_chroma_edge_cleanup_uses_bounded_size_adaptive_inset() -> None:
    small = clean_chroma_edges(Image.new("RGBA", (100, 100), "white"))
    large = clean_chroma_edges(Image.new("RGBA", (600, 600), "white"))

    assert small.getchannel("A").getbbox() == (1, 1, 99, 99)
    assert large.getchannel("A").getbbox() == (2, 2, 598, 598)


def test_exif_orientation_is_applied_before_canvas_coordinates(tmp_path: Path) -> None:
    source = Image.new("RGB", (2, 3), "black")
    source.putpixel((0, 0), (255, 0, 0))
    exif = Image.Exif()
    exif[274] = 6  # 顺时针 90°
    path = tmp_path / "oriented.jpg"
    source.save(path, exif=exif, quality=100, subsampling=0)
    normalized = normalize_image(path)
    assert normalized.size == (3, 2)
    assert normalized.getexif().get(274) in {None, 1}
