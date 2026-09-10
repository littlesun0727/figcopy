"""实现完全本地、确定性的客户素材准备与有序 PNG 图层合成。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .errors import CollageError
from .io_utils import (
    atomic_save_image,
    atomic_write_json,
    decode_image,
    read_json,
    resolve_input_path,
    safe_package_path,
    sha256_file,
)
from .prepare import (
    alpha_is_meaningful,
    edge_fade_mask,
    load_mask,
    multiply_alpha,
    normalize_image,
    parse_color,
    rect_to_box,
)
from .schema import validate_bindings
from .validate import validate_package

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PreparedBinding:
    """Renderer 的唯一动态输入：已规范化图片或已确认文字。"""

    image: Image.Image | None = None
    text: str | None = None
    scale: float = 1.0
    offset_px: tuple[float, float] = (0.0, 0.0)


def _safe_alpha_composite(
    canvas: Image.Image, layer: Image.Image, x: int, y: int
) -> None:
    """将可能越出画布的图层裁到交集后合成。"""

    left = max(0, x)
    top = max(0, y)
    right = min(canvas.width, x + layer.width)
    bottom = min(canvas.height, y + layer.height)
    if right <= left or bottom <= top:
        return
    crop = layer.crop((left - x, top - y, right - x, bottom - y))
    canvas.alpha_composite(crop, (left, top))


def _fit_to_rect(
    image: Image.Image,
    size: tuple[int, int],
    *,
    fit: str,
    anchor: tuple[float, float],
    scale_adjustment: float = 1.0,
    offset_px: tuple[float, float] = (0.0, 0.0),
) -> Image.Image:
    """按 cover/contain 等比缩放到透明的槽位局部画布。"""

    target_width, target_height = size
    base_scale = (
        max(target_width / image.width, target_height / image.height)
        if fit == "cover"
        else min(target_width / image.width, target_height / image.height)
    )
    if fit == "cover" and scale_adjustment < 1:
        raise CollageError(
            "INVALID_BINDING_SCALE", "cover 槽的用户缩放不能小于 1，以免露出空白"
        )
    scale = base_scale * scale_adjustment
    resized_size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    resized = image.convert("RGBA").resize(resized_size, Image.Resampling.LANCZOS)
    x = round((target_width - resized.width) * anchor[0] + offset_px[0])
    y = round((target_height - resized.height) * anchor[1] + offset_px[1])
    output = Image.new("RGBA", size, (0, 0, 0, 0))
    _safe_alpha_composite(output, resized, x, y)
    return output


def _rotate_and_place(
    canvas: Image.Image, local: Image.Image, rect: list[float], rotation_deg: float
) -> None:
    """以目标矩形中心为原点顺时针旋转并允许画布裁切。"""

    left, top, right, bottom = rect_to_box(rect)
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    # Pillow 正角度为逆时针，因此对产品的顺时针角度取负。
    rotated = (
        local.rotate(-rotation_deg, resample=Image.Resampling.BICUBIC, expand=True)
        if rotation_deg
        else local
    )
    x = round(center_x - rotated.width / 2)
    y = round(center_y - rotated.height / 2)
    _safe_alpha_composite(canvas, rotated, x, y)


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


def _load_font(
    root: Path, slot: dict[str, Any]
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if slot["font_path"] is not None:
        font_path = safe_package_path(root, slot["font_path"])
        try:
            return ImageFont.truetype(str(font_path), slot["font_size"])
        except OSError as exc:
            raise CollageError(
                "FONT_LOAD_FAILED", f"字体无法加载：{slot['font_path']}"
            ) from exc
    if not slot["fallback_approved"]:
        raise CollageError("FONT_REQUIRED", f"文字槽 {slot['id']} 没有获批字体")
    return ImageFont.load_default(size=slot["font_size"])


def _wrap_text(
    text: str, draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, max_width: int
) -> list[str]:
    """按实际像素宽度换行；同时适用于空格语言与中日韩文本。"""

    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        current = ""
        for character in paragraph:
            candidate = current + character
            if current and draw.textlength(candidate, font=font) > max_width:
                lines.append(current.rstrip())
                current = character.lstrip() if character.isspace() else character
            else:
                current = candidate
        lines.append(current)
    return lines


def _render_text_slot(
    root: Path,
    canvas: Image.Image,
    slot: dict[str, Any],
    binding: PreparedBinding,
) -> None:
    left, top, right, bottom = rect_to_box(slot["rect"])
    size = (right - left, bottom - top)
    local = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(local)
    font = _load_font(root, slot)
    lines = _wrap_text(binding.text or "", draw, font, size[0])[: slot["max_lines"]]
    y = 0
    color = parse_color(slot["color"])
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_width = bbox[2] - bbox[0]
        line_height = bbox[3] - bbox[1]
        if y + line_height > size[1]:
            break
        if slot["align"] == "center":
            x = (size[0] - line_width) / 2
        elif slot["align"] == "right":
            x = size[0] - line_width
        else:
            x = 0
        draw.text((x, y - bbox[1]), line, fill=color, font=font)
        y += line_height + slot["line_spacing"]
    _rotate_and_place(canvas, local, slot["rect"], slot["rotation_deg"])


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
