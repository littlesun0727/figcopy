"""Fit, rotate, clip, and composite local layers on the output canvas."""

from __future__ import annotations

from PIL import Image

from ..core.errors import CollageError
from ..imaging.operations import rect_to_box


def _safe_alpha_composite(
    canvas: Image.Image, layer: Image.Image, x: int, y: int
) -> None:
    """将可能越出画布的图层裁到交集后合成。"""

    left = max(0, x)
    top = max(0, y)
    right = min(canvas.width, x + layer.width)
    bottom = min(canvas.height, y + layer.height)
    if right <= left or bottom <= top:
        return
    crop = layer.crop((left - x, top - y, right - x, bottom - y))
    canvas.alpha_composite(crop, (left, top))


def _fit_to_rect(
    image: Image.Image,
    size: tuple[int, int],
    *,
    fit: str,
    anchor: tuple[float, float],
    scale_adjustment: float = 1.0,
    offset_px: tuple[float, float] = (0.0, 0.0),
) -> Image.Image:
    """按 cover/contain 等比缩放到透明的槽位局部画布。"""

    target_width, target_height = size
    base_scale = (
        max(target_width / image.width, target_height / image.height)
        if fit == "cover"
        else min(target_width / image.width, target_height / image.height)
    )
    if fit == "cover" and scale_adjustment < 1:
        raise CollageError(
            "INVALID_BINDING_SCALE", "cover 槽的用户缩放不能小于 1，以免露出空白"
        )
    scale = base_scale * scale_adjustment
    resized_size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    resized = image.convert("RGBA").resize(resized_size, Image.Resampling.LANCZOS)
    x = round((target_width - resized.width) * anchor[0] + offset_px[0])
    y = round((target_height - resized.height) * anchor[1] + offset_px[1])
    output = Image.new("RGBA", size, (0, 0, 0, 0))
    _safe_alpha_composite(output, resized, x, y)
    return output


def _rotate_and_place(
    canvas: Image.Image, local: Image.Image, rect: list[float], rotation_deg: float
) -> None:
    """以目标矩形中心为原点顺时针旋转并允许画布裁切。"""

    left, top, right, bottom = rect_to_box(rect)
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    # Pillow 正角度为逆时针，因此对产品的顺时针角度取负。
    rotated = (
        local.rotate(-rotation_deg, resample=Image.Resampling.BICUBIC, expand=True)
        if rotation_deg
        else local
    )
    x = round(center_x - rotated.width / 2)
    y = round(center_y - rotated.height / 2)
    _safe_alpha_composite(canvas, rotated, x, y)
