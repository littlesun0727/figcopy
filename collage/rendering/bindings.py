"""Normalize and validate customer media before deterministic rendering."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PIL import Image

from ..core.errors import CollageError
from ..core.io import resolve_input_path
from ..imaging.operations import (
    alpha_is_meaningful,
    load_mask,
    multiply_alpha,
    normalize_image,
)
from .model import PreparedBinding

LOGGER = logging.getLogger(__name__)


def _prepare_image_binding(
    slot: dict[str, Any],
    binding: dict[str, Any],
    bindings_file: Path,
) -> PreparedBinding:
    image_path = resolve_input_path(bindings_file, binding["path"])
    image = normalize_image(image_path).convert("RGBA")
    subject_alpha_value = binding.get("subject_alpha")
    if slot["mode"] != "cutout" and subject_alpha_value is not None:
        raise CollageError(
            "UNEXPECTED_SUBJECT_ALPHA",
            f"槽位 {slot['id']} 不是 cutout，不能传入 subject_alpha",
        )
    if slot["mode"] == "cutout":
        alpha_masks: list[Image.Image] = []
        if subject_alpha_value is not None:
            alpha_path = resolve_input_path(bindings_file, subject_alpha_value)
            alpha_masks.append(
                load_mask(alpha_path, image.size, name=f"{slot['id']} subject_alpha")
            )
        elif not alpha_is_meaningful(image):
            raise CollageError(
                "CUTOUT_PROVIDER_UNAVAILABLE",
                f"槽位 {slot['id']} 需要主体透明度；请提供透明 PNG 或 subject_alpha",
            )
        if alpha_masks:
            image = multiply_alpha(image, alpha_masks)
    return PreparedBinding(
        image=image,
        scale=float(binding.get("scale", 1.0)),
        offset_px=tuple(float(value) for value in binding.get("offset_px", [0, 0])),
    )


def prepare_bindings(
    template: dict[str, Any],
    bindings: dict[str, Any],
    bindings_file: Path,
) -> dict[str, PreparedBinding]:
    """在 Renderer 之外完成输入规范化和主体 alpha 装配。"""

    LOGGER.info("准备客户素材 | slots=%s", len(template["slots"]))
    prepared: dict[str, PreparedBinding] = {}
    raw_bindings = bindings["slots"]
    for slot in template["slots"]:
        raw = raw_bindings.get(slot["id"])
        if raw is None:
            if slot["type"] == "text" and slot.get("default_text") is not None:
                prepared[slot["id"]] = PreparedBinding(text=slot["default_text"])
            continue
        if slot["type"] == "image":
            prepared[slot["id"]] = _prepare_image_binding(slot, raw, bindings_file)
        else:
            text = raw.get("text", slot.get("default_text"))
            if text is None and slot["required"]:
                raise CollageError("MISSING_TEXT", f"文字槽 {slot['id']} 缺少文字")
            prepared[slot["id"]] = PreparedBinding(text=text or "")
    return prepared
