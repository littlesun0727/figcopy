"""Describe local overlay issues and find placement bounds without model review."""

from __future__ import annotations

import hashlib
from typing import Any

from PIL import Image, ImageChops

from ...imaging.operations import alpha_is_meaningful


def asset_fingerprint(image: Image.Image) -> str:
    """Bind cached processing to dimensions and decoded RGBA pixels."""
    rgba = image.convert("RGBA")
    return hashlib.sha256(str(rgba.size).encode("ascii") + rgba.tobytes()).hexdigest()


def alpha_completeness(image: Image.Image) -> dict[str, Any]:
    """Report simple alpha findings; edge contact is advisory, not a clipping verdict."""
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    box = alpha.point(lambda value: 255 if value >= 16 else 0).getbbox()
    issues = []
    if alpha.getbbox() is None:
        issues.append("EMPTY_OVERLAY")
    elif not alpha_is_meaningful(rgba):
        issues.append("OPAQUE_OVERLAY")
    clearance = max(1, round(min(image.size) * 0.015))
    if box and (
        box[0] < clearance
        or box[1] < clearance
        or box[2] > image.width - clearance
        or box[3] > image.height - clearance
    ):
        issues.append("OVERLAY_EDGE_CLIPPED")
    return {
        "bbox": list(box) if box else None,
        "size": list(image.size),
        "required_clearance_px": clearance,
        "issues": issues,
    }


def content_box(
    image: Image.Image,
    key: tuple[int, int, int] | None = None,
    original: Image.Image | None = None,
) -> tuple[int, int, int, int] | None:
    """Find all foreground parts, keeping punctuation and disconnected fine strokes."""
    visible = (
        image.convert("RGBA").getchannel("A").point(lambda value: 255 if value else 0)
    )
    if key is not None:
        # Despill changes green pixels to gray. Use pre-despill color only to
        # exclude screen residue from placement bounds, never to erode the artwork.
        hue, saturation, _ = (
            (original if original is not None else image).convert("HSV").split()
        )
        key_hue = Image.new("RGB", (1, 1), key).convert("HSV").getpixel((0, 0))[0]
        screen = ImageChops.multiply(
            hue.point(
                lambda value: (
                    255
                    if min(abs(value - key_hue), 256 - abs(value - key_hue)) <= 24
                    else 0
                )
            ),
            saturation.point(lambda value: 255 if value >= 32 else 0),
        )
        rgb = (original if original is not None else image).convert("RGB")
        red, green, blue = rgb.split()
        chroma = ImageChops.subtract(
            ImageChops.lighter(ImageChops.lighter(red, green), blue),
            ImageChops.darker(ImageChops.darker(red, green), blue),
        )
        # Dark, low-chroma strokes can share the screen hue without being screen.
        screen = ImageChops.multiply(
            screen, chroma.point(lambda value: 255 if value >= 64 else 0)
        )
        visible = ImageChops.multiply(visible, ImageChops.invert(screen))
    box = visible.getbbox()
    if box is None:
        return None
    # One source pixel keeps soft antialiasing at the crop boundary.
    return (
        max(0, box[0] - 1),
        max(0, box[1] - 1),
        min(image.width, box[2] + 1),
        min(image.height, box[3] + 1),
    )


def overlay_warning(overlay: dict, code: str, *, skipped: bool = False) -> dict:
    """Keep portable, concise findings separate from provider exception payloads."""
    message = {
        "OVERLAY_EDGE_CLIPPED": "边缘存在可见像素，请结合整图检查",
        "OPAQUE_OVERLAY": "素材底色可能未去净，请结合整图检查",
        "EMPTY_OVERLAY": "素材没有可见内容，已跳过",
    }.get(code, "该件制作未完成，已跳过" if skipped else "请检查该件素材")
    return {
        "overlay_id": overlay["id"],
        "label": overlay.get("label", overlay["id"]),
        "code": code,
        "message": message,
        "skipped": skipped,
    }
