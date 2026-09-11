"""Refine model-proposed straight photo windows and isolate light foreground strokes locally."""

from __future__ import annotations

import math
import re
from statistics import median

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

from ..core.errors import CollageError

GEOMETRY_VERSION = "auto-straight-windows/1"


def normalized_rect(value: object, size: tuple[int, int]) -> list[int]:
    """Convert normalized xywh into bounded working pixels; never silently clip invalid model data."""
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in value
        )
    ):
        raise CollageError("AUTO_GEOMETRY_INVALID", "模型矩形必须包含四个有限数值")
    x, y, w, h = value
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > 1.000001 or y + h > 1.000001:
        raise CollageError("AUTO_GEOMETRY_INVALID", "模型矩形超出规范化画布")
    left, top = round(x * size[0]), round(y * size[1])
    right, bottom = round((x + w) * size[0]), round((y + h) * size[1])
    if right <= left or bottom <= top:
        raise CollageError("AUTO_GEOMETRY_INVALID", "模型窗口取整后为空")
    return [left, top, right - left, bottom - top]


def validate_structure(value: dict) -> None:
    """Limit this experimental compiler to flat photographs over a full-canvas photograph."""
    if value.get("family") != "full_canvas_photo_with_insets":
        raise CollageError(
            "AUTO_LAYOUT_NOT_IMPLEMENTED",
            "当前真实试验编译器只支持满版照片和普通插图窗口",
        )
    slots, foreground = value.get("slots"), value.get("foreground")
    if not isinstance(slots, list) or not slots or not isinstance(foreground, list):
        raise CollageError("AUTO_STRUCTURE_INVALID", "自动结构缺少照片或前景清单")
    items = slots + foreground
    ids = [item.get("id") for item in items if isinstance(item, dict)]
    if len(ids) != len(items) or any(
        not isinstance(i, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", i)
        for i in ids
    ):
        raise CollageError("AUTO_STRUCTURE_INVALID", "自动元素 ID 不安全")
    order = value.get("layer_order")
    if (
        len(set(ids)) != len(ids)
        or not isinstance(order, list)
        or len(order) != len(ids)
        or set(order) != set(ids)
    ):
        raise CollageError(
            "AUTO_LAYER_ORDER_INVALID", "自动图层顺序必须无重复地包含全部元素"
        )
    mains = [s for s in slots if s.get("role") == "main"]
    if len(mains) != 1 or order[0] != mains[0]["id"]:
        raise CollageError("AUTO_MAIN_SLOT_INVALID", "必须有且仅有一个最底层满版照片槽")
    for slot in slots:
        if slot.get("mode") != "photo" or abs(float(slot.get("rotation_deg", 0))) > 0.5:
            raise CollageError(
                "AUTO_WINDOW_MODE_UNSUPPORTED",
                "该照片窗口需要尚未接入的旋转或人物处理能力",
            )
        if slot.get("frame", {}).get("style") not in {
            "none",
            "rectangle",
            "dashed_rectangle",
        }:
            raise CollageError("AUTO_FRAME_UNSUPPORTED", "当前试验不支持该相框类型")


def _white_mask(image: Image.Image, threshold: int = 230) -> Image.Image:
    red, green, blue = image.convert("RGB").split()
    low = ImageChops.darker(ImageChops.darker(red, green), blue)
    high = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    neutral = ImageChops.subtract(high, low).point(lambda v: 255 if v <= 28 else 0)
    return ImageChops.multiply(
        low.point(lambda v: 255 if v >= threshold else 0), neutral
    )


def refine_window(
    image: Image.Image, rect: list[int], frame: dict, *, main: bool
) -> tuple[list[int], dict]:
    """Snap near predicted white borders only where local pixel measurements support a line."""
    if main:
        if rect != [0, 0, *image.size]:
            raise CollageError(
                "AUTO_MAIN_SLOT_INVALID", "满版图片槽没有覆盖整个工作画布"
            )
        return rect, {
            "method": "model_full_canvas_role",
            "frame_rect": None,
            "edges": [],
        }
    x, y, w, h = rect
    if frame.get("style") == "none":
        return rect, {
            "method": "model_refined_without_line_evidence",
            "frame_rect": None,
            "edges": [],
        }
    white = _white_mask(image)
    radius = max(5, round(min(image.size) * 0.025))
    candidates = [x, y, x + w - 1, y + h - 1]
    edges = []
    snapped = []
    for side, position in enumerate(candidates):
        vertical = side % 2 == 0
        lower, upper = (
            (y + round(h * 0.12), y + round(h * 0.88))
            if vertical
            else (x + round(w * 0.12), x + round(w * 0.88))
        )
        limit = image.width if vertical else image.height
        measurements = []
        for p in range(max(0, position - radius), min(limit, position + radius + 1)):
            box = (p, lower, p + 1, upper) if vertical else (lower, p, upper, p + 1)
            fraction = ImageStat.Stat(white.crop(box)).mean[0] / 255
            measurements.append((p, fraction))
        best = max(measurements, key=lambda t: (t[1], -abs(t[0] - position)))
        baseline = median(v for _, v in measurements)
        # This is a candidate-edge detector, not a calibrated production acceptance threshold.
        supported = best[1] >= 0.12 and best[1] >= max(0.01, baseline) * 1.8
        selected = best[0] if supported else position
        snapped.append(selected)
        edges.append(
            {
                "side": ("left", "top", "right", "bottom")[side],
                "predicted": position,
                "selected": selected,
                "white_fraction": best[1],
                "background_fraction": baseline,
                "pixel_evidence": supported,
            }
        )
    left, top, right, bottom = snapped
    thickness = max(
        1, round(float(frame.get("width_fraction", 0.002)) * min(image.size))
    )
    inset = max(1, math.ceil(thickness / 2))
    if right - left <= 2 * inset or bottom - top <= 2 * inset:
        raise CollageError("AUTO_WINDOW_EMPTY", "细化后照片窗口为空")
    # White-border centers surround the replaceable photo; the original source image is never kept underneath.
    photo = [
        left + inset,
        top + inset,
        right - left + 1 - 2 * inset,
        bottom - top + 1 - 2 * inset,
    ]
    return photo, {
        "method": GEOMETRY_VERSION,
        "frame_rect": [left, top, right - left + 1, bottom - top + 1],
        "line_width": thickness,
        "edges": edges,
    }


def isolate_light_strokes(
    image: Image.Image, rect: list[int]
) -> tuple[Image.Image, dict]:
    """Estimate white-stroke alpha from local contrast; emit neutral white RGB rather than source-photo RGB."""
    x, y, w, h = rect
    crop = image.convert("RGB").crop((x, y, x + w, y + h))
    r, g, b = crop.split()
    low = ImageChops.darker(ImageChops.darker(r, g), b)
    high = ImageChops.lighter(ImageChops.lighter(r, g), b)
    neutral = ImageChops.subtract(high, low).point(lambda v: 255 if v < 36 else 0)
    # A local opening estimates the photographed backdrop behind thin handwriting.
    # Flattened JPEG cannot reveal the original alpha exactly, so this estimate must undergo visual review.
    background = low.filter(ImageFilter.MinFilter(11)).filter(ImageFilter.MaxFilter(11))
    original, ground = low.tobytes(), background.tobytes()
    alpha = bytes(
        max(0, min(255, round((p - base) * 255 / max(1, 255 - base))))
        if p >= 160 and p - base >= 12
        else 0
        for p, base in zip(original, ground, strict=True)
    )
    mask = ImageChops.multiply(Image.frombytes("L", crop.size, alpha), neutral)
    if mask.getbbox() is None:
        raise CollageError("AUTO_FOREGROUND_EMPTY", "未能分离可见前景笔画")
    output = Image.new("RGBA", crop.size, "white")
    output.putalpha(mask)
    return output, {
        "method": "local_white_stroke_alpha/1",
        "source_rgb_copied": False,
        "alpha_pixels": sum(mask.histogram()[1:]),
        "alpha_bbox": list(mask.getbbox()),
        "exact_original_alpha_recovered": False,
        "requires_visual_residue_check": True,
    }


def local_clip(size: tuple[int, int], radius_fraction: float = 0) -> Image.Image:
    """Create the pre-rotation photo window, keeping masks in slot-local coordinates."""
    result = Image.new("L", size, 0)
    ImageDraw.Draw(result).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1),
        radius=max(0, round(min(size) * min(0.25, max(0, radius_fraction)))),
        fill=255,
    )
    return result
