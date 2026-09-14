"""Expand photo attachments and transform groups without changing raster composition."""

from __future__ import annotations

import math

from ..core.errors import CollageError


def expanded_order(spec: dict) -> list[dict]:
    """Expand each photo once; independent overlays retain their root position."""
    attached = {}
    for overlay in spec["overlays"]:
        attachment = overlay["attachment"]
        if attachment:
            attached.setdefault(
                (attachment["slot_id"], attachment["position"]), []
            ).append({"type": "overlay", "id": overlay["id"]})
    result = []
    for layer in spec["layer_order"]:
        if layer["type"] == "slot":
            result.extend(attached.get((layer["id"], "below"), []))
            result.append(dict(layer))
            result.extend(attached.get((layer["id"], "above"), []))
        else:
            result.append(dict(layer))
    return result


def compile_layers(template: dict, *, include_missing: bool = False) -> list[dict]:
    """Compile portable layout definitions into the renderer's flat draw commands."""
    assets = {asset["id"] for asset in template["assets"]}
    overlays = {overlay["id"]: overlay for overlay in template["overlays"]}
    result = []
    for layer in expanded_order(template):
        if layer["type"] == "background":
            identifier = next(
                asset["id"]
                for asset in template["assets"]
                if asset["role"] == "background"
            )
            result.append(
                {
                    "type": "asset",
                    "asset_id": identifier,
                    "rect": [
                        0,
                        0,
                        template["canvas"]["width"],
                        template["canvas"]["height"],
                    ],
                    "rotation_deg": 0,
                    "fit": "contain",
                    "anchor": [0.5, 0.5],
                }
            )
        elif layer["type"] == "slot":
            result.append({"type": "slot", "slot_id": layer["id"]})
        elif include_missing or layer["id"] in assets:
            overlay = overlays[layer["id"]]
            result.append(
                {
                    "type": "asset",
                    "asset_id": overlay["id"],
                    **{
                        key: overlay[key]
                        for key in ("rect", "rotation_deg", "fit", "anchor")
                    },
                }
            )
    return result


def transform_attachments(
    overlays,
    owner_id,
    old_rect,
    new_rect,
    old_rotation,
    new_rotation,
    *,
    rect_key="rect",
):
    """Apply a similarity transform around the owner's center, preserving outsets."""
    children = [
        item
        for item in overlays
        if item["attachment"] and item["attachment"]["slot_id"] == owner_id
    ]
    if not children:
        return
    scale = new_rect[2] / old_rect[2]
    if not math.isclose(scale, new_rect[3] / old_rect[3], rel_tol=1e-6, abs_tol=1e-6):
        raise CollageError("LAYOUT_GROUP_SCALE_REQUIRED", "照片与附属物请等比缩放")
    angle = new_rotation - old_rotation
    radians = math.radians(angle)
    cosine, sine = math.cos(radians), math.sin(radians)
    old_center = [old_rect[i] + old_rect[i + 2] / 2 for i in (0, 1)]
    new_center = [new_rect[i] + new_rect[i + 2] / 2 for i in (0, 1)]
    for child in children:
        x, y, width, height = child[rect_key]
        dx, dy = x + width / 2 - old_center[0], y + height / 2 - old_center[1]
        # Rotate attachment centers as well as their pixels; separate rotations
        # about each item's own center would detach an offset frame from its photo.
        cx = new_center[0] + scale * (dx * cosine - dy * sine)
        cy = new_center[1] + scale * (dx * sine + dy * cosine)
        width, height = width * scale, height * scale
        child[rect_key] = [cx - width / 2, cy - height / 2, width, height]
        child["rotation_deg"] = child.get("rotation_deg", 0) + angle
