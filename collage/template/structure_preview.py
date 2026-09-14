"""Render a model-free structure preview using the production attachment order."""

from PIL import Image, ImageDraw

from ..rendering.layout import _rotate_and_place
from ..schemas.background import background_slot_id
from ..schemas.shapes import compile_shape
from .build.overlays import _make_basic_shape
from .layout import expanded_order


def render_structure(draft: dict, options: dict | None = None) -> Image.Image:
    """Use numbered opaque slots and explicit decoration placeholders, not customer images."""
    options = options or {}
    canvas = Image.new(
        "RGBA", (draft["canvas"]["width"], draft["canvas"]["height"]), "#DDD8CF"
    )
    slots = {item["id"]: item for item in draft["slots"]}
    overlays = {item["id"]: item for item in draft["overlays"]}
    colors = ("#628BAB", "#D4A45F", "#67A08E", "#B07AA1", "#8580B2")
    slot_indexes = {item["id"]: i for i, item in enumerate(draft["slots"])}
    for layer in expanded_order(draft):
        if layer["type"] == "background":
            continue
        is_slot = layer["type"] == "slot"
        item = (slots if is_slot else overlays)[layer["id"]]
        fields = options.get("slots" if is_slot else "overlays", {}).get(item["id"], {})
        rect = item["target_rect"]
        size = (max(1, round(rect[2])), max(1, round(rect[3])))
        if is_slot:
            index = slot_indexes[item["id"]]
            local = Image.new("RGBA", size, colors[index % len(colors)])
            label = (
                "BACKGROUND"
                if item["id"] == background_slot_id(draft)
                else "PHOTO"
                if item["type"] == "image"
                else "TEXT"
            )
            draw = ImageDraw.Draw(local)
            draw.text(
                (8, 8),
                f"{label} {index + 1}",
                fill="white",
                font_size=max(10, min(size) // 12),
            )
        elif item["action"] == "basic_shape":
            local = _make_basic_shape(
                {**item, "shape": compile_shape(fields.get("shape") or item["shape"])}
            )
        else:
            local = Image.new("RGBA", size, (153, 68, 136, 100))
            draw = ImageDraw.Draw(local)
            draw.rectangle((0, 0, size[0] - 1, size[1] - 1), outline="#FFB0DC", width=2)
            draw.text((3, 3), "DECOR", fill="white", font_size=max(10, min(size) // 6))
        _rotate_and_place(canvas, local, rect, fields.get("rotation_deg", 0))
    return canvas
