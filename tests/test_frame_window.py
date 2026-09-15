"""Verify reusable frame-window fitting, clipping, opt-outs, and cache invalidation."""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from collage.core.io import atomic_write_json, read_json, sha256_file
from collage.rendering import PreparedBinding, render_template
from collage.rendering.frame_window import detect_window, plan_photo_windows
from collage.rendering.layout import _rotate_and_place
from collage.rendering.service import _render_layers


@pytest.fixture
def framed_package(asset_package_factory, tmp_path):
    package = asset_package_factory(tmp_path, ())
    template = read_json(package / "template.json")
    template["canvas"].update(width=140, height=160)
    background = package / "assets/bg.png"
    Image.new("RGBA", (140, 160), "white").save(background)
    template["assets"] = [template["assets"][0]]
    template["assets"][0]["sha256"] = sha256_file(background)
    frame = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(frame)
    draw.rectangle((5, 5, 94, 94), fill="blue")
    draw.rectangle((15, 15, 84, 74), fill=(0, 0, 0, 0))
    frame_path = package / "assets/decoration.png"
    frame.save(frame_path)
    template["assets"].append(
        {
            "id": "decoration",
            "path": "assets/decoration.png",
            "role": "overlay",
            "requires_alpha": True,
            "sha256": sha256_file(frame_path),
        }
    )
    template["slots"] = [
        {
            "id": "picture",
            "type": "image",
            "label": "Photo",
            "required": True,
            "upload_hint": "Upload",
            "rect": [10, 10, 100, 100],
            "rotation_deg": 0,
            "mode": "photo",
            "fit": "cover",
            "anchor": [0.5, 0.5],
            "clip_mask": None,
            "clip_mask_sha256": None,
            "edge_fade_px": 0,
        }
    ]
    template["overlays"] = [
        {
            "id": "decoration",
            "attachment": {"slot_id": "picture", "position": "above"},
            "rect": [10, 10, 100, 120],
            "rotation_deg": 0,
            "fit": "contain",
            "anchor": [0.5, 0.5],
        }
    ]
    template["layer_order"].append({"type": "slot", "id": "picture"})
    atomic_write_json(package / "template.json", template)
    prepared = {"picture": PreparedBinding(image=Image.new("RGBA", (100, 100), "red"))}
    return package, template, prepared, frame


def _legacy_render(package, template, prepared):
    with patch("collage.rendering.service.plan_photo_windows", return_value={}):
        return _render_layers(package, template, prepared)


def test_public_render_fits_actual_contained_window_without_mutation(framed_package):
    package, template, prepared, _ = framed_package
    original = copy.deepcopy(template)
    manifest = (package / "template.json").read_bytes()
    before = _legacy_render(package, template, prepared)
    result = render_template(package, prepared, require_ready=False)
    assert before.getpixel((12, 12))[:3] == (255, 0, 0)
    assert result.getpixel((12, 12))[:3] == (255, 255, 255)
    assert result.getpixel((50, 50))[:3] == (255, 0, 0)
    assert result.getpixel((20, 30))[:3] == (0, 0, 255)
    assert (
        result.tobytes()
        == render_template(package, prepared, require_ready=False).tobytes()
    )
    assert template == original
    assert (package / "template.json").read_bytes() == manifest


def test_rotated_offset_window_shares_frame_center_and_occlusion(framed_package):
    package, template, prepared, frame = framed_package
    template["overlays"][0]["rotation_deg"] = 90
    template["slots"][0]["rotation_deg"] = 90
    actual = _render_layers(package, template, prepared)
    # Independent sprite oracle: fill the known source opening, then rotate
    # the complete sprite about its offset frame center (not the photo center).
    filled = frame.copy()
    ImageDraw.Draw(filled).rectangle((15, 15, 84, 74), fill="red")
    local = Image.new("RGBA", (100, 120), (0, 0, 0, 0))
    local.alpha_composite(filled, (0, 10))
    expected = Image.new("RGBA", (140, 160), "white")
    _rotate_and_place(expected, local, [10, 10, 100, 120], 90)
    assert actual.tobytes() == expected.tobytes()
    Image.new("RGBA", (10, 10), "lime").save(package / "assets/front.png")
    template["assets"].append({"id": "front", "path": "assets/front.png"})
    template["overlays"].append(
        {
            "id": "front",
            "attachment": None,
            "rect": [50, 50, 10, 10],
            "rotation_deg": 0,
            "fit": "contain",
            "anchor": [0.5, 0.5],
        }
    )
    template["layer_order"].append({"type": "overlay", "id": "front"})
    assert _render_layers(package, template, prepared).getpixel((55, 55))[:3] == (
        0,
        255,
        0,
    )


def test_round_window_clips_corners(framed_package):
    package, template, prepared, frame = framed_package
    frame = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(frame)
    draw.ellipse((5, 5, 94, 94), fill="blue")
    draw.ellipse((15, 15, 84, 84), fill=(0, 0, 0, 0))
    frame.save(package / "assets/decoration.png")
    result = _render_layers(package, template, prepared)
    assert result.getpixel((60, 70))[:3] == (255, 0, 0)
    assert result.getpixel((25, 35))[:3] == (255, 255, 255)
    assert result.getpixel((25, 70))[:3] == (255, 0, 0)


@pytest.mark.parametrize("kind", ["opaque", "open", "multiple", "tiny"])
def test_ambiguous_or_missing_window_keeps_original_render(framed_package, kind):
    package, template, prepared, frame = framed_package
    draw = ImageDraw.Draw(frame)
    if kind == "opaque":
        draw.rectangle((15, 15, 84, 74), fill="white")
    elif kind == "open":
        draw.rectangle((40, 0, 60, 25), fill=(0, 0, 0, 0))
    elif kind == "multiple":
        draw.rectangle((45, 15, 55, 74), fill="blue")
    else:
        draw.rectangle((15, 15, 84, 74), fill="blue")
        draw.rectangle((45, 45, 49, 49), fill=(0, 0, 0, 0))
    frame.save(package / "assets/decoration.png")
    assert (
        _render_layers(package, template, prepared).tobytes()
        == _legacy_render(package, template, prepared).tobytes()
    )


@pytest.mark.parametrize(
    "opt_out",
    [
        "unattached",
        "below",
        "cutout",
        "photo_feather",
        "contain",
        "mask",
        "distant",
        "multiple_frames",
    ],
)
def test_explicit_intent_and_uncertain_ownership_keep_original(framed_package, opt_out):
    package, template, prepared, frame = framed_package
    slot, overlay = template["slots"][0], template["overlays"][0]
    if opt_out == "unattached":
        overlay["attachment"] = None
        template["layer_order"].append({"type": "overlay", "id": overlay["id"]})
    elif opt_out == "below":
        overlay["attachment"]["position"] = "below"
    elif opt_out in ("cutout", "photo_feather"):
        slot["mode"] = opt_out
        if opt_out == "photo_feather":
            slot["edge_fade_px"] = 5
    elif opt_out == "contain":
        slot["fit"] = "contain"
    elif opt_out == "mask":
        Image.new("L", (100, 100), 128).save(package / "assets/mask.png")
        slot["clip_mask"] = "assets/mask.png"
    elif opt_out == "distant":
        overlay["rect"][0] = 120
    else:
        template["assets"].append({**template["assets"][-1], "id": "second"})
        template["overlays"].append({**copy.deepcopy(overlay), "id": "second"})
    assert (
        _render_layers(package, template, prepared).tobytes()
        == _legacy_render(package, template, prepared).tobytes()
    )


def test_user_pan_zoom_and_anchor_remain_effective(framed_package):
    package, template, prepared, _ = framed_package
    source = Image.new("RGBA", (200, 100), "red")
    ImageDraw.Draw(source).rectangle((100, 0, 199, 99), fill="blue")
    prepared["picture"].image = source
    centered = _render_layers(package, template, prepared)
    prepared["picture"].scale = 1.5
    prepared["picture"].offset_px = (15, 0)
    shifted = _render_layers(package, template, prepared)
    assert centered.getpixel((65, 60))[:3] == (0, 0, 255)
    assert shifted.getpixel((65, 60))[:3] == (255, 0, 0)
    assert shifted.getpixel((12, 12))[:3] == (255, 255, 255)
    template["slots"][0]["anchor"] = [1, 0.5]
    assert _render_layers(package, template, prepared).tobytes() != shifted.tobytes()


def test_cache_invalidates_for_asset_and_layout_changes(framed_package):
    package, template, prepared, frame = framed_package
    first = _render_layers(package, template, prepared)
    ImageDraw.Draw(frame).rectangle((15, 15, 84, 74), fill="white")
    frame.save(package / "assets/decoration.png")
    second = _render_layers(package, template, prepared)
    assert first.getpixel((12, 12)) != second.getpixel((12, 12))
    ImageDraw.Draw(frame).rectangle((15, 15, 84, 74), fill=(0, 0, 0, 0))
    frame.save(package / "assets/decoration.png")
    template["overlays"][0]["rect"][0] += 5
    moved = _render_layers(package, template, prepared)
    assert moved.getpixel((25, 50))[:3] == (0, 0, 255)
    assert first.getpixel((25, 50))[:3] == (255, 0, 0)


def test_decorative_pinholes_do_not_create_extra_windows(framed_package):
    _, template, _, frame = framed_package
    ImageDraw.Draw(frame).rectangle((7, 7, 9, 9), fill=(0, 0, 0, 0))
    window, reason = detect_window(frame)
    assert window is not None and reason == "matched"
    assert window.mask.getpixel((8, 8)) == 0
    # Small attached decorations are screened before any decoding or detection.
    template["overlays"][0]["rect"] = [0, 0, 10, 10]
    assert (
        plan_photo_windows(template, lambda _: pytest.fail("small decoration decoded"))
        == {}
    )


@pytest.mark.parametrize("angle", [27, -38])
def test_arbitrary_rotation_keeps_photo_inside_rotated_opening(framed_package, angle):
    package, template, prepared, _ = framed_package
    template["overlays"][0]["rotation_deg"] = angle
    template["slots"][0]["rotation_deg"] = angle
    actual = _render_layers(package, template, prepared).convert("RGB")
    # Independently draw the known source opening and transform its support;
    # allow two edge pixels for bicubic sampling and the covered inner seam.
    opening = Image.new("RGBA", (100, 120), (0, 0, 0, 0))
    ImageDraw.Draw(opening).rectangle((15, 25, 84, 84), fill="white")
    allowed = Image.new("RGBA", (140, 160), (0, 0, 0, 0))
    _rotate_and_place(allowed, opening, [10, 10, 100, 120], angle)
    support = (
        allowed.getchannel("A")
        .filter(ImageFilter.MaxFilter(5))
        .point(lambda v: 255 if v else 0)
    )
    red, green, blue = actual.split()
    photo = ImageChops.multiply(
        red.point(lambda v: 255 if v > 200 else 0),
        ImageChops.darker(
            green.point(lambda v: 255 if v < 40 else 0),
            blue.point(lambda v: 255 if v < 40 else 0),
        ),
    )
    assert photo.histogram()[255] > 3000
    assert ImageChops.subtract(photo, support).getbbox() is None
