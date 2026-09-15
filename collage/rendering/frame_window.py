"""Find closed alpha windows on attached overlays for local photo fitting."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from ..imaging.operations import rect_to_box
from ..schemas.background import background_slot_id

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowGeometry:
    mask: Image.Image
    box: tuple[int, int, int, int]
    area: int


@dataclass(frozen=True)
class PhotoWindow:
    geometry: WindowGeometry
    rect: tuple[float, ...]
    rotation_deg: float
    overlay_id: str


def _transparent_regions(alpha: Image.Image):
    """Yield enclosed regions as horizontal spans, without a per-pixel Python BFS."""
    width, height = alpha.size
    # Opaque pixels stop the search. Runs use bytearray's C-level find/rfind;
    # large empty photo windows therefore cost rows rather than Python pixels.
    pixels = bytearray(alpha.point(lambda value: 255 if value >= 128 else 0).tobytes())
    cursor = 0
    while (seed := pixels.find(b"\x00", cursor)) != -1:
        pending = [seed]
        spans = []
        area = 0
        exterior = False
        while pending:
            point = pending.pop()
            if pixels[point]:
                continue
            y, x = divmod(point, width)
            row = y * width
            left = pixels.rfind(b"\xff", row, point) + 1
            left = max(row, left)
            right = pixels.find(b"\xff", point, row + width)
            if right == -1:
                right = row + width
            pixels[left:right] = b"\xff" * (right - left)
            spans.append((y, left - row, right - row))
            area += right - left
            exterior |= y in (0, height - 1) or left == row or right == row + width
            for adjacent in (y - 1, y + 1):
                if not 0 <= adjacent < height:
                    continue
                start = adjacent * width + left - row
                end = adjacent * width + right - row
                while (next_seed := pixels.find(b"\x00", start, end)) != -1:
                    pending.append(next_seed)
                    stop = pixels.find(b"\xff", next_seed, end)
                    if stop == -1:
                        break
                    start = stop + 1
        cursor = seed + 1
        if not exterior:
            yield area, spans


@lru_cache(maxsize=16)
def _cached_window(size: tuple[int, int], alpha_bytes: bytes):
    alpha = Image.frombytes("L", size, alpha_bytes)
    minimum = max(64, round(size[0] * size[1] * 0.02))
    regions = sorted(
        (region for region in _transparent_regions(alpha) if region[0] >= minimum),
        key=lambda region: region[0],
        reverse=True,
    )
    if not regions or regions[0][0] < size[0] * size[1] * 0.10:
        return None, "no_large_closed_window"
    area, spans = regions[0]
    if len(regions) > 1 and regions[1][0] >= area * 0.10:
        return None, "multiple_windows"
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for y, left, right in spans:
        draw.line((left, y, right - 1, y), fill=255)
    box = mask.getbbox()
    assert box is not None
    if area < (box[2] - box[0]) * (box[3] - box[1]) * 0.35:
        return None, "sparse_window"
    # Extend only under the solid inner edge, never into exterior transparency.
    # The photo supports the frame's antialiased edge without a pale seam.
    solid = alpha.point(lambda value: 255 if value >= 128 else 0)
    edge = ImageChops.multiply(mask.filter(ImageFilter.MaxFilter(3)), solid)
    mask = ImageChops.lighter(mask, edge)
    return WindowGeometry(mask, mask.getbbox(), area), "matched"


def detect_window(local: Image.Image):
    """Analyze the exact fitted raster; cache only modest masks (at most ~32 MiB)."""
    alpha = local.getchannel("A")
    # Alpha bytes invalidate automatically after regeneration or resizing, even
    # when a caller renders an in-memory template without validating its hash.
    detect = (
        _cached_window
        if local.width * local.height <= 1024**2
        else _cached_window.__wrapped__
    )
    return detect(alpha.size, alpha.tobytes())


def _matches_photo(window: WindowGeometry, overlay: dict, slot: dict) -> bool:
    """Check ownership geometry in the unrotated frame's local coordinates."""
    left, top, right, bottom = rect_to_box(overlay["rect"])
    cx, cy = (left + right) / 2, (top + bottom) / 2
    sl, st, sr, sb = rect_to_box(slot["rect"])
    sx, sy = (sl + sr) / 2, (st + sb) / 2
    angle = math.radians(slot["rotation_deg"])
    inverse = math.radians(-overlay["rotation_deg"])
    corners = []
    for x, y in ((sl, st), (sr, st), (sr, sb), (sl, sb)):
        dx, dy = x - sx, y - sy
        wx = sx + dx * math.cos(angle) - dy * math.sin(angle) - cx
        wy = sy + dx * math.sin(angle) + dy * math.cos(angle) - cy
        corners.append(
            (
                wx * math.cos(inverse) - wy * math.sin(inverse) + (right - left) / 2,
                wx * math.sin(inverse) + wy * math.cos(inverse) + (bottom - top) / 2,
            )
        )
    photo_area = (sr - sl) * (sb - st)
    if not 0.25 <= window.area / photo_area <= 2.0:
        return False
    footprint = Image.new("L", window.mask.size, 0)
    ImageDraw.Draw(footprint).polygon(corners, fill=255)
    overlap = ImageChops.multiply(window.mask, footprint).histogram()[255]
    return overlap >= min(window.area, photo_area) * 0.60


def plan_photo_windows(
    template: dict, load_overlay: Callable[[dict], Image.Image]
) -> dict[str, PhotoWindow]:
    """Use explicit ownership and geometry; never infer frame roles from names."""
    slots = {
        slot["id"]: slot
        for slot in template["slots"]
        if slot["type"] == "image"
        and slot["id"] != background_slot_id(template)
        and slot["mode"] == "photo"
        and slot["fit"] == "cover"
        and slot["clip_mask"] is None
    }
    assets = {asset["id"] for asset in template["assets"]}
    candidates: dict[str, list[PhotoWindow]] = {}
    for overlay in template["overlays"]:
        attachment = overlay["attachment"]
        if (
            not attachment
            or attachment["position"] != "above"
            or overlay["id"] not in assets
        ):
            continue
        owner = attachment["slot_id"]
        if owner not in slots:
            continue
        slot = slots[owner]
        if (
            overlay["rect"][2] * overlay["rect"][3]
            < slot["rect"][2] * slot["rect"][3] * 0.25
        ):
            continue
        geometry, reason = detect_window(load_overlay(overlay))
        if geometry is not None and not _matches_photo(geometry, overlay, slot):
            geometry, reason = None, "window_outside_photo"
        LOGGER.debug("相框内窗检测 | overlay=%s reason=%s", overlay["id"], reason)
        if geometry is not None:
            candidates.setdefault(owner, []).append(
                PhotoWindow(
                    geometry,
                    tuple(overlay["rect"]),
                    overlay["rotation_deg"],
                    overlay["id"],
                )
            )
    result = {}
    for owner, windows in candidates.items():
        if len(windows) == 1:
            result[owner] = windows[0]
        else:
            LOGGER.debug("相框内窗跳过 | slot=%s reason=multiple_frames", owner)
    if result:
        LOGGER.info("本地相框内窗适配 | slots=%s", len(result))
    return result
