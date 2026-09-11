"""Convert reviewed slots and layer order into a portable template package."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.errors import CollageError
from ...core.io import atomic_save_image, resolve_input_path, sha256_file
from ...imaging.operations import load_mask, rect_to_box
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
                mask = load_mask(
                    input_path,
                    (box[2] - box[0], box[3] - box[1]),
                    name=f"{source['id']} clip_mask",
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
        if source["type"] == "image" and spec["version"] in {
            "collage-build/2",
            "collage-build/3",
        }:
            # mask 影响可见窗口，必须和固定素材一样绑定哈希，防止无声改变模板。
            slot["clip_mask_sha256"] = (
                sha256_file(output_dir / clip_path) if clip_path else None
            )
        slots.append(slot)
    return slots


def _template_layers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    overlays = {overlay["id"]: overlay for overlay in spec["overlays"]}
    canvas = spec["canvas"]
    layers: list[dict[str, Any]] = []
    for layer in spec["layer_order"]:
        if layer["type"] == "background":
            layers.append(
                {
                    "type": "asset",
                    "asset_id": "bg",
                    "rect": [0, 0, canvas["width"], canvas["height"]],
                    "rotation_deg": 0,
                    "fit": "contain",
                    "anchor": [0.5, 0.5],
                }
            )
        elif layer["type"] == "slot":
            layers.append({"type": "slot", "slot_id": layer["id"]})
        else:
            overlay = overlays[layer["id"]]
            layers.append(
                {
                    "type": "asset",
                    "asset_id": overlay["id"],
                    "rect": overlay["target_rect"],
                    "rotation_deg": overlay["rotation_deg"],
                    "fit": "contain",
                    "anchor": [0.5, 0.5],
                }
            )
    return layers
