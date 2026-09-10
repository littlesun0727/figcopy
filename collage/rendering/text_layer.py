"""Load fonts, wrap text, and render text slots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..core.errors import CollageError
from ..core.io import safe_package_path
from ..imaging.operations import parse_color, rect_to_box
from .layout import _rotate_and_place
from .model import PreparedBinding


def _load_font(
    root: Path, slot: dict[str, Any]
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if slot["font_path"] is not None:
        font_path = safe_package_path(root, slot["font_path"])
        try:
            return ImageFont.truetype(str(font_path), slot["font_size"])
        except OSError as exc:
            raise CollageError(
                "FONT_LOAD_FAILED", f"字体无法加载：{slot['font_path']}"
            ) from exc
    if not slot["fallback_approved"]:
        raise CollageError("FONT_REQUIRED", f"文字槽 {slot['id']} 没有获批字体")
    return ImageFont.load_default(size=slot["font_size"])


def _wrap_text(
    text: str, draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, max_width: int
) -> list[str]:
    """按实际像素宽度换行；同时适用于空格语言与中日韩文本。"""

    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        current = ""
        for character in paragraph:
            candidate = current + character
            if current and draw.textlength(candidate, font=font) > max_width:
                lines.append(current.rstrip())
                current = character.lstrip() if character.isspace() else character
            else:
                current = candidate
        lines.append(current)
    return lines


def _render_text_slot(
    root: Path,
    canvas: Image.Image,
    slot: dict[str, Any],
    binding: PreparedBinding,
) -> None:
    left, top, right, bottom = rect_to_box(slot["rect"])
    size = (right - left, bottom - top)
    local = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(local)
    font = _load_font(root, slot)
    lines = _wrap_text(binding.text or "", draw, font, size[0])[: slot["max_lines"]]
    y = 0
    color = parse_color(slot["color"])
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_width = bbox[2] - bbox[0]
        line_height = bbox[3] - bbox[1]
        if y + line_height > size[1]:
            break
        if slot["align"] == "center":
            x = (size[0] - line_width) / 2
        elif slot["align"] == "right":
            x = size[0] - line_width
        else:
            x = 0
        draw.text((x, y - bbox[1]), line, fill=color, font=font)
        y += line_height + slot["line_spacing"]
    _rotate_and_place(canvas, local, slot["rect"], slot["rotation_deg"])
