"""Export one rendered Figcopy template as a self-contained layered SVG.

Each template layer is rendered to a transparent full-canvas PNG and embedded in
an Inkscape layer group. This preserves Figcopy's crop, alpha, feathering, and
z-order while keeping every visual layer independently toggleable.
"""

from __future__ import annotations

import argparse
import base64
import io
import logging
import re
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageChops

from collage.io_utils import decode_image, read_json, safe_package_path
from collage.prepare import rect_to_box
from collage.render import (
    _fit_to_rect,
    _render_image_slot,
    _rotate_and_place,
    prepare_bindings,
)
from collage.schema import validate_bindings
from collage.validate import validate_package

LOGGER = logging.getLogger("figcopy.svg_export")


def _safe_id(value: str) -> str:
    """Return a stable XML-compatible identifier."""

    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return cleaned or "layer"


def _render_layer(
    root: Path,
    template: dict,
    prepared: dict,
    layer: dict,
) -> tuple[str, Image.Image]:
    """Render exactly one TemplateSpec layer on a transparent canvas."""

    canvas = Image.new(
        "RGBA",
        (template["canvas"]["width"], template["canvas"]["height"]),
        (0, 0, 0, 0),
    )
    assets = {item["id"]: item for item in template["assets"]}
    slots = {item["id"]: item for item in template["slots"]}

    if layer["type"] == "asset":
        layer_id = layer["asset_id"]
        asset = assets[layer_id]
        source = decode_image(
            safe_package_path(root, asset["path"]), mode="RGBA"
        )
        left, top, right, bottom = rect_to_box(layer["rect"])
        local = _fit_to_rect(
            source,
            (right - left, bottom - top),
            fit=layer["fit"],
            anchor=tuple(layer["anchor"]),
        )
        _rotate_and_place(canvas, local, layer["rect"], layer["rotation_deg"])
        return layer_id, canvas

    layer_id = layer["slot_id"]
    binding = prepared.get(layer_id)
    if binding is None:
        raise RuntimeError(f"missing prepared binding: {layer_id}")
    _render_image_slot(root, canvas, slots[layer_id], binding)
    return layer_id, canvas


def export_layered_svg(
    template_dir: Path,
    bindings_path: Path,
    output_path: Path,
    expected_render: Path | None = None,
) -> Path:
    """Build a standalone SVG whose groups follow TemplateSpec layer order."""

    root = template_dir.resolve()
    bindings_path = bindings_path.resolve()
    output_path = output_path.resolve()
    template = validate_package(root, require_ready=False)
    bindings = validate_bindings(read_json(bindings_path), template)
    prepared = prepare_bindings(template, bindings, bindings_path)

    width = template["canvas"]["width"]
    height = template["canvas"]["height"]
    composite = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    groups: list[str] = []

    LOGGER.info("开始生成 SVG 图层 | layers=%s canvas=%sx%s", len(template["layers"]), width, height)
    for index, layer in enumerate(template["layers"], start=1):
        layer_id, layer_image = _render_layer(root, template, prepared, layer)
        composite.alpha_composite(layer_image)

        encoded = io.BytesIO()
        layer_image.save(encoded, format="PNG", optimize=True)
        payload = base64.b64encode(encoded.getvalue()).decode("ascii")
        xml_id = f"layer-{index:02d}-{_safe_id(layer_id)}"
        label = escape(f"{index:02d} {layer_id}")
        groups.append(
            f'  <g inkscape:groupmode="layer" inkscape:label="{label}" id="{xml_id}">\n'
            f'    <image x="0" y="0" width="{width}" height="{height}" '
            f'preserveAspectRatio="none" href="data:image/png;base64,{payload}" />\n'
            "  </g>"
        )
        LOGGER.info("SVG 图层已生成 | index=%s/%s id=%s", index, len(template["layers"]), layer_id)

    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        "<!-- Raster-backed Figcopy layers; no SVG text elements are present. -->\n"
        + "\n".join(groups)
        + "\n</svg>\n"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(svg, encoding="utf-8", newline="\n")
    temporary.replace(output_path)

    if expected_render is not None:
        expected = Image.open(expected_render).convert("RGBA")
        difference = ImageChops.difference(composite, expected)
        if difference.getbbox() is not None:
            raise RuntimeError("layer composite does not match the expected render")
        LOGGER.info("SVG 图层复合与 PNG 基准逐像素一致")

    LOGGER.info("分层 SVG 已保存 | output=%s", output_path)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="导出自包含的 Inkscape 分层 SVG")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected-render", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    export_layered_svg(args.template, args.bindings, args.out, args.expected_render)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
