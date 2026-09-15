"""Render an image binding into its configured template slot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from ..core.errors import CollageError
from ..core.io import decode_image, safe_package_path
from ..imaging.operations import edge_fade_mask, multiply_alpha, rect_to_box
from .frame_window import PhotoWindow
from .layout import _fit_to_rect, _rotate_and_place
from .model import PreparedBinding


def _render_image_slot(
    root: Path,
    canvas: Image.Image,
    slot: dict[str, Any],
    binding: PreparedBinding,
    *,
    window: PhotoWindow | None = None,
) -> None:
    if binding.image is None:
        raise CollageError("INVALID_BINDING", f"图片槽 {slot['id']} 没有图片")
    if window is not None:
        left, top, right, bottom = window.geometry.box
        photo = _fit_to_rect(
            binding.image,
            (right - left, bottom - top),
            fit=slot["fit"],
            anchor=tuple(slot["anchor"]),
            scale_adjustment=binding.scale,
            offset_px=binding.offset_px,
        )
        # Fit and mask inside the frame's own local canvas, then use its exact
        # rotation center. The photo still renders at its original layer order.
        local = Image.new("RGBA", window.geometry.mask.size, (0, 0, 0, 0))
        local.alpha_composite(photo, (left, top))
        local = multiply_alpha(local, [window.geometry.mask])
        _rotate_and_place(canvas, local, window.rect, window.rotation_deg)
        return
    left, top, right, bottom = rect_to_box(slot["rect"])
    local_size = (right - left, bottom - top)
    source = binding.image
    if slot["mode"] == "cutout":
        # cutout 的目标框描述可见主体，透明留白不应参与缩放。
        visible_bbox = source.convert("RGBA").getchannel("A").getbbox()
        if visible_bbox is None:
            raise CollageError("CUTOUT_EMPTY_OR_OPAQUE", "抠图结果没有可见主体")
        source = source.crop(visible_bbox)
    local = _fit_to_rect(
        source,
        local_size,
        fit=slot["fit"],
        anchor=tuple(slot["anchor"]),
        scale_adjustment=binding.scale,
        offset_px=binding.offset_px,
    )
    masks: list[Image.Image] = []
    if slot["clip_mask"] is not None:
        # A slot-local mask follows the photo's resize. Its original bytes/hash
        # stay fixed; sampling here keeps previews and saved revisions identical.
        mask = decode_image(safe_package_path(root, slot["clip_mask"]), mode="L")
        masks.append(mask.resize(local_size, Image.Resampling.LANCZOS))
    if slot["mode"] == "photo_feather":
        masks.append(edge_fade_mask(local_size, slot["edge_fade_px"]))
    if masks:
        local = multiply_alpha(local, masks)
    _rotate_and_place(canvas, local, slot["rect"], slot["rotation_deg"])
