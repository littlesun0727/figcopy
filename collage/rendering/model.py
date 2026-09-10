"""Data prepared for deterministic template rendering."""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image


@dataclass(slots=True)
class PreparedBinding:
    """Renderer 的唯一动态输入：已规范化图片或已确认文字。"""

    image: Image.Image | None = None
    text: str | None = None
    scale: float = 1.0
    offset_px: tuple[float, float] = (0.0, 0.0)
