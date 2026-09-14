"""实现图片规范化、裁切、遮罩、受保护背景合成与透明素材整理。"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageFilter, ImageOps

from ..core.errors import CollageError
from ..core.io import atomic_save_image, decode_image
from .chroma import remove_background

LOGGER = logging.getLogger(__name__)


def normalize_image(input_path: Path, output_path: Path | None = None) -> Image.Image:
    """应用 EXIF 方向并统一到 RGB/RGBA 工作色彩模式，不修改原文件。"""

    LOGGER.info("规范化图片 | input=%s", input_path.name)
    image = decode_image(input_path)
    image = ImageOps.exif_transpose(image)
    has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
    normalized = image.convert("RGBA" if has_alpha else "RGB")
    if output_path is not None:
        atomic_save_image(normalized, output_path)
        LOGGER.info(
            "规范化图片已保存 | file=%s size=%sx%s", output_path.name, *normalized.size
        )
    return normalized


def rect_to_box(rect: Iterable[float]) -> tuple[int, int, int, int]:
    """将 xywh 转成 Pillow box，采用一致的四舍五入规则。"""

    x, y, width, height = (float(value) for value in rect)
    left, top = round(x), round(y)
    right, bottom = round(x + width), round(y + height)
    if right <= left or bottom <= top:
        raise CollageError("INVALID_RECT", "取整后的 rect 宽高必须大于 0")
    return left, top, right, bottom


def crop_source(image: Image.Image, rect: Iterable[float]) -> Image.Image:
    """严格裁切 source_rect；与 target_rect 不同，它不能越过源图边界。"""

    box = rect_to_box(rect)
    if box[0] < 0 or box[1] < 0 or box[2] > image.width or box[3] > image.height:
        raise CollageError(
            "SOURCE_RECT_OUT_OF_BOUNDS",
            "source_rect 超出规范化参考图",
            details={"box": box, "image_size": image.size},
        )
    return image.crop(box)


def load_mask(path: Path, expected_size: tuple[int, int], *, name: str) -> Image.Image:
    """加载单通道遮罩并拒绝尺寸错位。"""

    mask = decode_image(path, mode="L")
    if mask.size != expected_size:
        raise CollageError(
            "MASK_SIZE_MISMATCH",
            f"{name} 尺寸与其坐标空间不一致",
            details={"expected": expected_size, "actual": mask.size, "path": str(path)},
        )
    return mask


def convert_remove_mask_polarity(
    remove_mask: Image.Image, provider_polarity: str
) -> Image.Image:
    """把内部白=删除转换为 provider 明确声明的遮罩极性。"""

    mask = remove_mask.convert("L")
    if provider_polarity == "white_edit":
        return mask.copy()
    if provider_polarity == "white_preserve":
        return ImageOps.invert(mask)
    raise CollageError(
        "UNSUPPORTED_MASK_POLARITY",
        "provider 未声明可识别的 mask 极性",
        details={"polarity": provider_polarity},
    )


def make_blend_mask(
    core_mask: Image.Image,
    *,
    allowed_mask: Image.Image | None = None,
    expand_px: int = 0,
    feather_px: int = 0,
) -> Image.Image:
    """由删除核心构造柔边 mask，并保证允许范围外严格为零。"""

    core = core_mask.convert("L")
    allowed = (
        allowed_mask.convert("L")
        if allowed_mask is not None
        else Image.new("L", core.size, 255)
    )
    if allowed.size != core.size:
        raise CollageError("MASK_SIZE_MISMATCH", "M_allowed 与 M_core 尺寸不一致")
    # 核心不允许越过 allowed，否则旧内容可能因保护限制而残留，直接要求人工修正。
    forbidden_core = ImageChops.multiply(core, ImageOps.invert(allowed))
    if forbidden_core.getbbox() is not None:
        raise CollageError("CORE_OUTSIDE_ALLOWED_MASK", "删除核心超出允许修改范围")
    expanded = core
    if expand_px:
        expanded = core.filter(ImageFilter.MaxFilter(expand_px * 2 + 1))
    blend = (
        expanded.filter(ImageFilter.GaussianBlur(feather_px))
        if feather_px
        else expanded.copy()
    )
    # 核心必须保持完全由候选图提供，不能在旧内容边缘混回原像素。
    blend = Image.composite(Image.new("L", core.size, 255), blend, core)
    return ImageChops.multiply(blend, allowed)


def protected_background_compose(
    reference: Image.Image,
    generated: Image.Image,
    blend_mask: Image.Image,
) -> Image.Image:
    """执行 C = I×(1-M)+G×M，非编辑区逐通道保持原值。"""

    source = reference.convert("RGBA")
    candidate = generated.convert("RGBA")
    mask = blend_mask.convert("L")
    if candidate.size != source.size or mask.size != source.size:
        raise CollageError(
            "BACKGROUND_MAPPING_FAILED",
            "背景候选、参考图和 blend mask 必须映射到同一画布",
            details={
                "reference": source.size,
                "candidate": candidate.size,
                "mask": mask.size,
            },
        )
    return Image.composite(candidate, source, mask)


def alpha_is_meaningful(image: Image.Image) -> bool:
    """只有至少一个非 255 像素时，才把 alpha 视为真实透明信息。"""

    if image.mode != "RGBA":
        return False
    minimum, maximum = image.getchannel("A").getextrema()
    return minimum < 255 and maximum > 0


def choose_chroma_key(reference_crop: Image.Image) -> tuple[int, int, int]:
    """从多种高饱和候选色中选与参考素材像素距离最大的颜色。"""

    candidates = [
        (255, 0, 255),
        (0, 255, 255),
        (255, 255, 0),
        (0, 255, 0),
        (255, 64, 0),
        (64, 0, 255),
    ]
    sample = reference_crop.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
    pixels = [
        sample.getpixel((x, y))
        for y in range(sample.height)
        for x in range(sample.width)
    ]

    def separation(candidate: tuple[int, int, int]) -> float:
        # 用靠近候选色的低分位距离，避免均值掩盖少量同色描边。
        distances = sorted(
            math.sqrt(sum((pixel[index] - candidate[index]) ** 2 for index in range(3)))
            for pixel in pixels
        )
        return distances[max(0, len(distances) // 20)]

    return max(candidates, key=separation)


def remove_chroma_key(
    image: Image.Image,
    key_rgb: tuple[int, int, int],
    *,
    tolerance: int = 40,
    softness: int = 24,
) -> Image.Image:
    """本地去除单次生成请求中的纯色背景，并保留软边 alpha。"""

    rgba = image.convert("RGBA")
    output: list[tuple[int, int, int, int]] = []
    hard = max(0, tolerance)
    soft_end = hard + max(1, softness)
    for y in range(rgba.height):
        for x in range(rgba.width):
            red, green, blue, source_alpha = rgba.getpixel((x, y))
            distance = math.sqrt(
                (red - key_rgb[0]) ** 2
                + (green - key_rgb[1]) ** 2
                + (blue - key_rgb[2]) ** 2
            )
            if distance <= hard:
                alpha = 0
            elif distance >= soft_end:
                alpha = source_alpha
            else:
                alpha = round(source_alpha * (distance - hard) / (soft_end - hard))
            output.append((red, green, blue, alpha))
    rgba.putdata(output)
    return rgba


def remove_chroma_background(
    image: Image.Image,
    key_rgb: tuple[int, int, int],
    *,
    tolerance: int = 40,
    softness: int = 24,
    hue_tolerance: int = 24,
    minimum_saturation: int = 32,
) -> Image.Image:
    """通过 PyAV 去除色键背景与绿/蓝污染，保持旧调用接口及输入 alpha。"""

    return remove_background(
        image,
        key_rgb,
        tolerance=tolerance,
        softness=softness,
        hue_tolerance=hue_tolerance,
        minimum_saturation=minimum_saturation,
    )


def chroma_alpha_is_clean(
    image: Image.Image,
    *,
    transparent_threshold: int = 16,
    minimum_transparent_fraction: float = 0.01,
    minimum_visible_fraction: float = 0.01,
) -> bool:
    """拒绝只有零星透明像素、边缘仍被色键背景占满的 overlay。"""

    if not alpha_is_meaningful(image):
        return False
    alpha = image.convert("RGBA").getchannel("A")
    width, height = alpha.size
    values = alpha.tobytes()
    total = len(values)
    transparent = sum(value <= transparent_threshold for value in values) / total
    visible = sum(value > transparent_threshold for value in values) / total
    if transparent < minimum_transparent_fraction or visible < minimum_visible_fraction:
        return False

    sides = [
        [alpha.getpixel((x, 0)) for x in range(width)],
        [alpha.getpixel((x, height - 1)) for x in range(width)],
        [alpha.getpixel((0, y)) for y in range(height)],
        [alpha.getpixel((width - 1, y)) for y in range(height)],
    ]
    clear_sides = sum(
        sum(value <= transparent_threshold for value in side) / len(side) >= 0.5
        for side in sides
    )
    return clear_sides >= 2


def clean_chroma_edges(
    image: Image.Image, *, inset_px: int | None = None
) -> Image.Image:
    """向内收缩色键 alpha，去掉模型抗锯齿产生的高饱和细边。"""

    rgba = image.convert("RGBA")
    if inset_px is None:
        # 小贴纸收 1px，大贴纸收 2px；限制上限以免损伤细线装饰。
        inset_px = max(1, min(2, round(min(rgba.size) * 0.003)))
    if inset_px < 0:
        raise CollageError("INVALID_ALPHA_INSET", "alpha 内缩像素不能小于 0")
    if inset_px == 0:
        return rgba

    alpha = rgba.getchannel("A")
    # 显式补透明边，确保贴到画布边缘的素材也会向内收缩。
    padded = ImageOps.expand(alpha, border=inset_px, fill=0)
    eroded = padded.filter(ImageFilter.MinFilter(inset_px * 2 + 1))
    alpha = eroded.crop(
        (
            inset_px,
            inset_px,
            inset_px + rgba.width,
            inset_px + rgba.height,
        )
    )
    rgba.putalpha(alpha)
    return rgba


def trim_transparent(
    image: Image.Image,
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """裁去透明留白并返回原图中的有效外轮廓 bbox。"""

    rgba = image.convert("RGBA")
    bbox = rgba.getchannel("A").getbbox()
    if bbox is None:
        raise CollageError("EMPTY_OVERLAY", "overlay 没有任何可见像素")
    return rgba.crop(bbox), bbox


def edge_fade_mask(size: tuple[int, int], fade_px: int) -> Image.Image:
    """构造槽位局部坐标的四边线性渐隐 mask。"""

    width, height = size
    if fade_px <= 0:
        return Image.new("L", size, 255)
    horizontal = [
        min(255, round(255 * min(x + 1, width - x) / fade_px)) for x in range(width)
    ]
    vertical = [
        min(255, round(255 * min(y + 1, height - y) / fade_px)) for y in range(height)
    ]
    x_mask = Image.new("L", (width, 1))
    x_mask.putdata(horizontal)
    x_mask = x_mask.resize(size)
    y_mask = Image.new("L", (1, height))
    y_mask.putdata(vertical)
    y_mask = y_mask.resize(size)
    return ImageChops.darker(x_mask, y_mask)


def multiply_alpha(image: Image.Image, masks: Iterable[Image.Image]) -> Image.Image:
    """将各语义 mask 各乘一次，防止重复处理软边。"""

    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    for mask in masks:
        if mask.size != rgba.size:
            raise CollageError("MASK_SIZE_MISMATCH", "局部 mask 与待处理图层尺寸不一致")
        alpha = ImageChops.multiply(alpha, mask.convert("L"))
    rgba.putalpha(alpha)
    return rgba


def parse_color(
    value: str | None, *, default: tuple[int, int, int, int] = (0, 0, 0, 0)
) -> tuple[int, int, int, int]:
    """解析 CSS/Pillow 色值并统一成 RGBA。"""

    if value is None:
        return default
    try:
        color = ImageColor.getcolor(value, "RGBA")
    except ValueError as exc:
        raise CollageError("INVALID_COLOR", f"无法解析颜色：{value}") from exc
    return color
