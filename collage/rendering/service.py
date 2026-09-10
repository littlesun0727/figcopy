"""Orchestrate deterministic local rendering and write its audit record."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PIL import Image

from ..core.errors import CollageError
from ..core.io import (
    atomic_save_image,
    atomic_write_json,
    decode_image,
    read_json,
    safe_package_path,
    sha256_file,
)
from ..imaging.operations import rect_to_box
from ..schemas import validate_bindings
from ..template.validation import validate_package
from .bindings import prepare_bindings
from .image_layer import _render_image_slot
from .layout import _fit_to_rect, _rotate_and_place
from .model import PreparedBinding
from .text_layer import _render_text_slot

LOGGER = logging.getLogger(__name__)


def render_template(
    template_dir: Path,
    prepared: dict[str, PreparedBinding],
    *,
    require_ready: bool = True,
) -> Image.Image:
    """只消费本地已准备素材，按 layers 从底到顶确定性合成。"""

    root = template_dir.resolve()
    template = validate_package(root, require_ready=require_ready)
    canvas = Image.new(
        "RGBA",
        (template["canvas"]["width"], template["canvas"]["height"]),
        (0, 0, 0, 0),
    )
    assets = {asset["id"]: asset for asset in template["assets"]}
    slots = {slot["id"]: slot for slot in template["slots"]}
    LOGGER.info(
        "开始本地合成 | canvas=%sx%s layers=%s",
        canvas.width,
        canvas.height,
        len(template["layers"]),
    )
    for index, layer in enumerate(template["layers"], start=1):
        if layer["type"] == "asset":
            asset = assets[layer["asset_id"]]
            source = decode_image(safe_package_path(root, asset["path"]), mode="RGBA")
            left, top, right, bottom = rect_to_box(layer["rect"])
            local = _fit_to_rect(
                source,
                (right - left, bottom - top),
                fit=layer["fit"],
                anchor=tuple(layer["anchor"]),
            )
            _rotate_and_place(canvas, local, layer["rect"], layer["rotation_deg"])
            LOGGER.debug(
                "已合成图层 %s/%s | asset=%s",
                index,
                len(template["layers"]),
                asset["id"],
            )
        else:
            slot = slots[layer["slot_id"]]
            binding = prepared.get(slot["id"])
            if binding is None:
                if slot["required"]:
                    raise CollageError("MISSING_BINDING", f"缺少必填槽位：{slot['id']}")
                continue
            if slot["type"] == "image":
                _render_image_slot(root, canvas, slot, binding)
            else:
                _render_text_slot(root, canvas, slot, binding)
            LOGGER.debug(
                "已合成图层 %s/%s | slot=%s", index, len(template["layers"]), slot["id"]
            )
    LOGGER.info("本地合成完成")
    return canvas


def render_from_files(
    template_dir: Path,
    bindings_file: Path,
    output_path: Path,
    *,
    require_ready: bool = True,
) -> Path:
    """读取 TemplateSpec/Bindings，准备输入并原子导出 PNG。"""

    started = time.monotonic()
    template = validate_package(template_dir, require_ready=require_ready)
    bindings = validate_bindings(read_json(bindings_file), template)
    prepared = prepare_bindings(template, bindings, bindings_file)
    result = render_template(template_dir, prepared, require_ready=require_ready)
    atomic_save_image(result, output_path)
    atomic_write_json(
        output_path.with_suffix(output_path.suffix + ".render.json"),
        {
            "template_manifest_sha256": sha256_file(template_dir / "template.json"),
            "bindings_sha256": sha256_file(bindings_file),
            "output_sha256": sha256_file(output_path),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "network_calls": 0,
        },
    )
    LOGGER.info("PNG 已导出 | output=%s", output_path.resolve())
    return output_path
