"""Convert reviewed slots and layer order into a portable template package."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.errors import CollageError
from ...core.io import atomic_save_image, decode_image, resolve_input_path, sha256_file
from ...imaging.operations import rect_to_box
from PIL import Image
from .common import _copy_atomic


def _package_slots(
    spec: dict[str, Any], spec_path: Path, output_dir: Path
) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for source in spec["slots"]:
        common = {
            "id": source["id"],
            "type": source["type"],
            "label": source["label"],
            "required": source["required"],
            "upload_hint": source["upload_hint"],
            "rect": source["target_rect"],
            "rotation_deg": source["rotation_deg"],
        }
        if source["type"] == "image":
            clip_path = None
            if source["clip_mask"] is not None:
                input_path = resolve_input_path(spec_path, source["clip_mask"])
                box = rect_to_box(source["target_rect"])
                mask = decode_image(input_path, mode="L").resize(
                    (box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS
                )
                clip_path = f"masks/{source['id']}_clip.png"
                atomic_save_image(mask, output_dir / clip_path)
            slot = {
                **common,
                "mode": source["mode"],
                "fit": source["fit"],
                "anchor": source["anchor"],
                "clip_mask": clip_path,
                "edge_fade_px": source["edge_fade_px"],
            }
        else:
            font_path = None
            if source["font_path"] is not None:
                input_path = resolve_input_path(spec_path, source["font_path"])
                if not input_path.is_file():
                    raise CollageError(
                        "FILE_NOT_FOUND", f"字体文件不存在：{input_path}"
                    )
                safe_suffix = (
                    input_path.suffix.lower()
                    if input_path.suffix.lower() in {".ttf", ".otf", ".ttc"}
                    else ".font"
                )
                font_path = f"assets/fonts/{source['id']}{safe_suffix}"
                _copy_atomic(input_path, output_dir / font_path)
            slot = {
                **common,
                "default_text": source["default_text"],
                "font_path": font_path,
                "font_size": source["font_size"],
                "fallback_approved": source["fallback_approved"],
                "color": source["color"],
                "align": source["align"],
                "max_lines": source["max_lines"],
                "line_spacing": source["line_spacing"],
            }
        if source["type"] == "image":
            # mask 影响可见窗口，必须和固定素材一样绑定哈希，防止无声改变模板。
            slot["clip_mask_sha256"] = (
                sha256_file(output_dir / clip_path) if clip_path else None
            )
        slots.append(slot)
    return slots


def _package_overlays(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Retain geometry even when a candidate has no asset for an overlay yet."""
    return [
        {
            "id": overlay["id"],
            "attachment": overlay["attachment"],
            "rect": list(overlay["target_rect"]),
            "rotation_deg": overlay["rotation_deg"],
            "fit": "contain",
            "anchor": [0.5, 0.5],
        }
        for overlay in spec["overlays"]
    ]
