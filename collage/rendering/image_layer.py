"""Render an image binding into its configured template slot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from ..core.errors import CollageError
from ..core.io import safe_package_path
from ..imaging.operations import edge_fade_mask, load_mask, multiply_alpha, rect_to_box
from .layout import _fit_to_rect, _rotate_and_place
from .model import PreparedBinding


def _render_image_slot(
    root: Path,
    canvas: Image.Image,
    slot: dict[str, Any],
    binding: PreparedBinding,
) -> None:
    if binding.image is None:
        raise CollageError("INVALID_BINDING", f"图片槽 {slot['id']} 没有图片")
    left, top, right, bottom = rect_to_box(slot["rect"])
    local_size = (right - left, bottom - top)
    local = _fit_to_rect(
        binding.image,
        local_size,
        fit=slot["fit"],
        anchor=tuple(slot["anchor"]),
        scale_adjustment=binding.scale,
        offset_px=binding.offset_px,
    )
    masks: list[Image.Image] = []
    if slot["clip_mask"] is not None:
        masks.append(
            load_mask(
                safe_package_path(root, slot["clip_mask"]),
                local_size,
                name=f"{slot['id']} clip_mask",
            )
        )
    if slot["mode"] == "photo_feather":
        masks.append(edge_fade_mask(local_size, slot["edge_fade_px"]))
    if masks:
        local = multiply_alpha(local, masks)
    _rotate_and_place(canvas, local, slot["rect"], slot["rotation_deg"])
