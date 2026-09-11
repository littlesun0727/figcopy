"""Remove color-key backgrounds with local PyAV filters while retaining canvas and soft alpha."""

from __future__ import annotations

import importlib
import logging
import math
import time
from fractions import Fraction

from PIL import Image, ImageChops

from ..core.errors import CollageError

LOGGER = logging.getLogger(__name__)
CHROMA_PROCESSING_VERSION = "pyav-colorkey-despill/1"


def require_chroma_backend():
    """Check local dependencies before any paid image-generation request is made."""
    try:
        backend = importlib.import_module("av")
    except (ImportError, OSError) as exc:
        raise CollageError(
            "CHROMA_DEPENDENCY_MISSING",
            '无法加载 PyAV；请运行 python -m pip install -e "." 更新核心依赖',
            details={"reason": type(exc).__name__},
        ) from exc
    try:
        backend.filter.Filter("colorkey")
        backend.filter.Filter("despill")
    except Exception as exc:
        raise CollageError(
            "CHROMA_FILTER_UNAVAILABLE", "当前 PyAV 缺少 colorkey 或 despill 滤镜"
        ) from exc
    return backend


def _quantile(histogram: list[int], fraction: float) -> int:
    target = max(1, math.ceil(sum(histogram) * fraction))
    accumulated = 0
    for value, count in enumerate(histogram):
        accumulated += count
        if accumulated >= target:
            return value
    return 0


def _sample_background(
    rgba: Image.Image,
    key: tuple[int, int, int],
    hue_tolerance: int,
    minimum_saturation: int,
) -> tuple[tuple[int, int, int], float]:
    """Estimate the actual key from the outer margin, excluding transparent and unrelated pixels."""
    width, height = rgba.size
    margin = max(1, round(min(width, height) * 0.08))
    rectangles = [
        (0, 0, width, margin),
        (0, height - margin, width, height),
        (0, 0, margin, height),
        (width - margin, 0, width, height),
    ]
    hue_key, saturation_key, _ = (
        Image.new("RGB", (1, 1), key).convert("HSV").getpixel((0, 0))
    )
    if saturation_key < minimum_saturation:
        return key, 0.0
    hue_table = [
        255
        if min(abs(value - hue_key), 256 - abs(value - hue_key)) <= hue_tolerance
        else 0
        for value in range(256)
    ]
    histograms = [[0] * 256 for _ in range(3)]
    for rectangle in rectangles:
        part = rgba.crop(rectangle)
        rgb = part.convert("RGB")
        hue, saturation, _ = rgb.convert("HSV").split()
        mask = ImageChops.multiply(
            hue.point(hue_table),
            saturation.point(lambda value: 255 if value >= minimum_saturation else 0),
        )
        # Hidden RGB in existing transparent PNGs is not evidence of a screen color.
        mask = ImageChops.multiply(
            mask, part.getchannel("A").point(lambda value: 255 if value >= 16 else 0)
        )
        for channel, histogram in zip(rgb.split(), histograms, strict=True):
            for value, count in enumerate(channel.histogram(mask)):
                histogram[value] += count
    if not sum(histograms[0]):
        return key, 0.0
    sampled = tuple(_quantile(histogram, 0.5) for histogram in histograms)
    # Background-only variation can widen the key radius without a global hue flood fill.
    spread = math.sqrt(
        sum(
            max(
                abs(_quantile(histogram, 0.01) - center),
                abs(_quantile(histogram, 0.99) - center),
            )
            ** 2
            for histogram, center in zip(histograms, sampled, strict=True)
        )
    ) / (255 * math.sqrt(3))
    return sampled, spread


def remove_background(
    image: Image.Image,
    key_rgb: tuple[int, int, int],
    *,
    tolerance: int = 40,
    softness: int = 24,
    hue_tolerance: int = 24,
    minimum_saturation: int = 32,
) -> Image.Image:
    """Use sampled colorkey plus green/blue despill, without erosion or foreground recoloring."""
    if (
        not isinstance(key_rgb, (tuple, list))
        or len(key_rgb) != 3
        or any(type(value) is not int or not 0 <= value <= 255 for value in key_rgb)
        or any(
            type(value) is not int or not 0 <= value <= 255
            for value in (tolerance, softness, minimum_saturation)
        )
        or type(hue_tolerance) is not int
        or not 0 <= hue_tolerance <= 128
    ):
        raise CollageError(
            "INVALID_CHROMA_SETTINGS", "色键与去底参数必须在有效整数范围内"
        )
    rgba = image.convert("RGBA")
    if not rgba.width or not rgba.height:
        return rgba
    backend = require_chroma_backend()
    started = time.perf_counter()
    sampled, spread = _sample_background(
        rgba, key_rgb, hue_tolerance, minimum_saturation
    )
    # The existing default tolerance/softness map to the user-reviewed trial's 0.12/0.16.
    similarity = min(
        1.0, max(0.00001, tolerance * 0.003, spread + 0.01 if spread else 0)
    )
    blend = min(1.0, softness * (0.16 / 24))
    color = "0x" + "".join(f"{channel:02X}" for channel in sampled)
    despill = None
    if key_rgb[1] - max(key_rgb[0], key_rgb[2]) >= 32:
        despill = "type=green:mix=0.5:expand=0:green=-1:brightness=0:alpha=0"
    elif key_rgb[2] - max(key_rgb[0], key_rgb[1]) >= 32:
        despill = "type=blue:mix=0.5:expand=0:green=0:blue=-1:brightness=0:alpha=0"
    LOGGER.info("PyAV 色键去底开始 | size=%sx%s", *rgba.size)
    LOGGER.debug(
        "PyAV 色键参数 | requested=%s sampled=%s similarity=%.5f blend=%.5f despill=%s",
        key_rgb,
        sampled,
        similarity,
        blend,
        despill,
    )
    try:
        frame = backend.VideoFrame(rgba.width, rgba.height, "rgba")
        frame.pts = 0
        frame.time_base = Fraction(1, 1)
        # PyAV may pad each row (especially odd widths). Respect its stride in both directions.
        frame.planes[0].update(rgba.tobytes("raw", "RGBA", frame.planes[0].line_size))
        graph = backend.filter.Graph()
        graph.threads = 4
        nodes = [
            graph.add_buffer(template=frame),
            graph.add(
                "colorkey", f"color={color}:similarity={similarity}:blend={blend}"
            ),
        ]
        if despill:
            nodes.append(graph.add("despill", despill))
        nodes.extend([graph.add("format", "pix_fmts=rgba"), graph.add("buffersink")])
        graph.link_nodes(*nodes)
        graph.configure()
        graph.push(frame)
        output = graph.pull()
        result = Image.frombytes(
            "RGBA",
            (output.width, output.height),
            bytes(output.planes[0]),
            "raw",
            "RGBA",
            output.planes[0].line_size,
        )
    except Exception as exc:
        raise CollageError(
            "CHROMA_PROCESSING_FAILED",
            "PyAV 色键去底失败",
            details={"reason": type(exc).__name__},
        ) from exc
    if result.size != rgba.size:
        raise CollageError("CHROMA_SIZE_MISMATCH", "PyAV 去底改变了素材画幅")
    # colorkey creates alpha independently of input alpha. Never revive hidden pixels or
    # turn an already translucent foreground opaque when processing an imported PNG.
    result.putalpha(ImageChops.multiply(result.getchannel("A"), rgba.getchannel("A")))
    LOGGER.info(
        "PyAV 色键去底完成 | elapsed_ms=%.2f", (time.perf_counter() - started) * 1000
    )
    return result
