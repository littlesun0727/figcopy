"""用一张真实拼贴参考图构建四照片槽模板，并生成可对照的试拼结果。"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from collage.core.io import (
    atomic_save_image,
    atomic_write_json,
    decode_image,
    sha256_file,
)
from collage.core.logging import configure_logging
from collage.rendering import render_from_files
from collage.template.build import build_template

LOGGER = logging.getLogger(__name__)

# 坐标均位于 1320 x 1767 的原始参考图空间，格式为 x/y/width/height。
SLOTS: tuple[dict[str, object], ...] = (
    {
        "id": "photo_background",
        "label": "全画布主照片",
        "rect": [0, 0, 1320, 1767],
        "photo": "photo_city.png",
        "anchor": [0.5, 0.5],
    },
    {
        "id": "photo_small_left",
        "label": "左上小照片",
        "rect": [70, 213, 340, 252],
        "photo": "photo_ocean.png",
        "anchor": [0.5, 0.5],
    },
    {
        "id": "photo_tall_right",
        "label": "右上竖照片",
        "rect": [598, 99, 602, 800],
        "photo": "photo_cafe.png",
        "anchor": [0.5, 0.5],
    },
    {
        "id": "photo_large_left",
        "label": "左下大照片",
        "rect": [106, 862, 559, 747],
        "photo": "photo_mountain.png",
        "anchor": [0.56, 0.5],
    },
)


def _align_clean_background(
    source: Image.Image, target_size: tuple[int, int]
) -> Image.Image:
    """用居中裁切加等比缩放恢复生成底图，避免不同宽高比直接拉伸。"""

    return ImageOps.fit(
        source.convert("RGB"),
        target_size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def _split_test_sheet(sheet: Image.Image, output_dir: Path) -> dict[str, Path]:
    """把严格 2x2 测试素材图拆为四张独立客户占位照片。"""

    midpoint_x = sheet.width // 2
    midpoint_y = sheet.height // 2
    boxes = {
        "photo_ocean.png": (0, 0, midpoint_x, midpoint_y),
        "photo_cafe.png": (midpoint_x, 0, sheet.width, midpoint_y),
        "photo_mountain.png": (0, midpoint_y, midpoint_x, sheet.height),
        "photo_city.png": (midpoint_x, midpoint_y, sheet.width, sheet.height),
    }
    paths: dict[str, Path] = {}
    for filename, box in boxes.items():
        path = output_dir / filename
        atomic_save_image(sheet.crop(box).convert("RGB"), path)
        paths[filename] = path
        LOGGER.info("已拆分测试照片 | file=%s box=%s", filename, box)
    return paths


def _make_remove_mask(size: tuple[int, int]) -> Image.Image:
    """清除完整旧主照片；成品会在其上放置新的全画布照片槽。"""

    return Image.new("L", size, 255)


def _frame_corridors(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    thickness: int,
    *,
    sides: str = "tblr",
) -> None:
    """在 ROI 蒙版中标出相框边线附近，避免提取整张旧照片。"""

    left, top, right, bottom = box
    half = thickness // 2
    if "t" in sides:
        draw.rectangle((left, top - half, right, top + half), fill=255)
    if "b" in sides:
        draw.rectangle((left, bottom - half, right, bottom + half), fill=255)
    if "l" in sides:
        draw.rectangle((left - half, top, left + half, bottom), fill=255)
    if "r" in sides:
        draw.rectangle((right - half, top, right + half, bottom), fill=255)


def _make_fixed_foreground(reference: Image.Image) -> tuple[Image.Image, list[int]]:
    """提取会压在新照片上方的白色虚线与涂鸦，返回紧边界透明图。"""

    rgb = reference.convert("RGB")
    red, green, blue = rgb.split()
    darkest = ImageChops.darker(ImageChops.darker(red, green), blue)
    lightest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    chroma = ImageChops.subtract(lightest, darkest)

    # 白色笔画同时满足高亮、低色差，并通常比局部背景更亮。
    bright = darkest.point(lambda value: max(0, min(255, (value - 205) * 6)))
    local_base = darkest.filter(ImageFilter.GaussianBlur(radius=6))
    detail = ImageChops.subtract(darkest, local_base).point(
        lambda value: min(255, value * 9)
    )
    visible = ImageChops.lighter(bright, ImageChops.multiply(detail, darkest))
    neutral = chroma.point(lambda value: max(0, 255 - value * 5))
    alpha = ImageChops.multiply(visible, neutral)

    roi = Image.new("L", reference.size, 0)
    roi_draw = ImageDraw.Draw(roi)
    _frame_corridors(roi_draw, (66, 206, 414, 472), 20)
    _frame_corridors(roi_draw, (590, 86, 1213, 909), 28)
    _frame_corridors(roi_draw, (97, 850, 681, 1624), 30)
    _frame_corridors(roi_draw, (678, 909, 1212, 1490), 30, sides="tl")
    # 主照片覆盖整张画布，因此文字、涂鸦和相框都必须独立恢复为前景。
    roi_draw.rectangle((44, 65, 550, 178), fill=255)
    roi_draw.rectangle((65, 575, 445, 840), fill=255)
    roi_draw.rectangle((844, 9, 969, 159), fill=255)
    roi_draw.rectangle((1060, 1570, 1319, 1766), fill=255)
    alpha = ImageChops.multiply(alpha, roi)

    rgba = rgb.convert("RGBA")
    rgba.putalpha(alpha)
    bounds = alpha.getbbox()
    if bounds is None:
        raise RuntimeError("固定前景提取为空，请检查坐标或白色阈值")
    cropped = rgba.crop(bounds)
    target_rect = [bounds[0], bounds[1], bounds[2] - bounds[0], bounds[3] - bounds[1]]
    LOGGER.info("固定前景已提取 | target_rect=%s", target_rect)
    return cropped, target_rect


def _slot_spec(slot: dict[str, object]) -> dict[str, object]:
    rect = list(slot["rect"])  # type: ignore[arg-type]
    return {
        "id": slot["id"],
        "label": slot["label"],
        "type": "image",
        "required": True,
        "mode": "photo",
        "source_rect": rect,
        "target_rect": rect,
        "upload_hint": "上传任意照片；渲染器会按 cover 等比裁切",
        "review_notes": "根据参考图虚线框内边缘人工量取",
        "rotation_deg": 0,
        "fit": "cover",
        "anchor": slot["anchor"],
        "clip_mask": None,
        "edge_fade_px": 0,
    }


def _write_specs(
    root: Path,
    reference_path: Path,
    foreground_path: Path,
    foreground_rect: list[int],
) -> tuple[Path, Path]:
    """写入已确认制作规格和本次测试素材绑定。"""

    reviewed_path = root / "reviewed.json"
    bindings_path = root / "bindings.json"
    width, height = decode_image(reference_path).size
    reviewed = {
        "version": "collage-reviewed/1",
        "status": "reviewed",
        "reference": {
            "path": "inputs/reference.png",
            "sha256": sha256_file(reference_path),
        },
        "canvas": {
            "width": width,
            "height": height,
            "coordinate_space": "canvas_px",
            "rect_format": "xywh",
        },
        "slots": [_slot_spec(slot) for slot in SLOTS],
        "overlays": [
            {
                "id": "fixed_foreground",
                "label": "虚线相框与前景涂鸦",
                "source_rect": foreground_rect,
                "target_rect": foreground_rect,
                "action": "reference_generate",
                "generation_brief": "保留参考图白色虚线相框、回形针和右下角手写字。",
                "requires_exact_content": True,
                "review_notes": "由原图像素阈值和受限 ROI 提取，不重新绘制文字。",
                "rotation_deg": 0,
                "prepared_asset": "inputs/fixed_foreground.png",
                "background_mode": "alpha",
                "chroma_key": None,
                "chroma_tolerance": 40,
                "shape": None,
            }
        ],
        "background": {
            "background_brief": (
                "移除四张旧照片，在原位置延续灰蓝到暖金色的柔和云层；"
                "不得修改照片区外像素。"
            ),
            "review_notes": "生成候选已居中裁切并等比缩放到原始画布。",
            "remove_mask": "inputs/remove_mask.png",
            "allowed_mask": "inputs/remove_mask.png",
            "candidate_path": "inputs/clean_background_aligned.png",
            "expand_px": 0,
            "feather_px": 0,
        },
        "layer_order": [
            {"type": "background"},
            {"type": "slot", "id": "photo_background"},
            {"type": "slot", "id": "photo_large_left"},
            {"type": "slot", "id": "photo_tall_right"},
            {"type": "slot", "id": "photo_small_left"},
            {"type": "overlay", "id": "fixed_foreground"},
        ],
        "review": {
            "reviewer": "codex-assisted-trial",
            "reviewed_at": datetime.now(UTC).isoformat(),
            "notes": "用户提供真实参考图后的首次试拼；等待用户视觉确认。",
            "questions_resolved": True,
        },
        "audit": {
            "fixture": False,
            "source": "user-reference-plus-generated-placeholder-photos",
            "foreground_asset": foreground_path.name,
        },
    }
    bindings = {
        "version": "collage-bindings/1",
        "slots": {
            str(slot["id"]): {
                "path": f"inputs/{slot['photo']}",
                "scale": 1.0,
                "offset_px": [0, 0],
            }
            for slot in SLOTS
        },
    }
    atomic_write_json(reviewed_path, reviewed)
    atomic_write_json(bindings_path, bindings)
    return reviewed_path, bindings_path


def _save_comparison(reference: Image.Image, result: Image.Image, path: Path) -> None:
    """输出半尺寸的参考/试拼对照图，便于快速视觉验收。"""

    thumb_size = (660, 884)
    header_height = 52
    gutter = 20
    canvas = Image.new(
        "RGB", (thumb_size[0] * 2 + gutter, thumb_size[1] + header_height), "#202124"
    )
    canvas.paste(reference.convert("RGB").resize(thumb_size), (0, header_height))
    canvas.paste(
        result.convert("RGB").resize(thumb_size),
        (thumb_size[0] + gutter, header_height),
    )
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=24)
    draw.text((16, 13), "REFERENCE", fill="white", font=font)
    draw.text(
        (thumb_size[0] + gutter + 16, 13), "TRIAL RENDER", fill="white", font=font
    )
    atomic_save_image(canvas, path)


def create_trial(
    reference_source: Path,
    clean_background_source: Path,
    photo_sheet_source: Path,
    output_dir: Path,
) -> Path:
    """准备输入、构建模板并运行一次完全本地渲染。"""

    root = output_dir.resolve()
    inputs = root / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    LOGGER.info("阶段 1/5：读取和规范化参考图")
    reference = decode_image(reference_source).convert("RGB")
    if reference.size != (1320, 1767):
        raise ValueError(f"当前试验坐标要求参考图为 1320x1767，实际为 {reference.size}")
    reference_path = inputs / "reference.png"
    atomic_save_image(reference, reference_path)

    LOGGER.info("阶段 2/5：对齐清版候选并生成受保护移除蒙版")
    generated_background = decode_image(clean_background_source)
    aligned_background = _align_clean_background(generated_background, reference.size)
    aligned_path = inputs / "clean_background_aligned.png"
    atomic_save_image(aligned_background, aligned_path)
    mask = _make_remove_mask(reference.size)
    atomic_save_image(mask, inputs / "remove_mask.png")
    atomic_write_json(
        root / "background_alignment.json",
        {
            "kind": "center_crop_then_uniform_resize",
            "source_size": list(generated_background.size),
            "target_size": list(reference.size),
            "centering": [0.5, 0.5],
            "direct_nonuniform_stretch": False,
        },
    )

    LOGGER.info("阶段 3/5：拆分四张新测试照片并提取固定前景")
    _split_test_sheet(decode_image(photo_sheet_source), inputs)
    foreground, foreground_rect = _make_fixed_foreground(reference)
    foreground_path = inputs / "fixed_foreground.png"
    atomic_save_image(foreground, foreground_path)

    LOGGER.info("阶段 4/5：构建 needs_review 模板包")
    reviewed_path, bindings_path = _write_specs(
        root, reference_path, foreground_path, foreground_rect
    )
    template_dir = root / "template"
    build_template(
        reviewed_path,
        template_dir,
        work_dir=root / "work",
        force=True,
    )

    LOGGER.info("阶段 5/5：绑定新素材并执行确定性本地渲染")
    result_path = root / "trial_render.png"
    render_from_files(template_dir, bindings_path, result_path, require_ready=False)
    _save_comparison(reference, decode_image(result_path), root / "comparison.png")
    LOGGER.info(
        "试拼完成 | result=%s comparison=%s", result_path, root / "comparison.png"
    )
    return result_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--clean-background", type=Path, required=True)
    parser.add_argument("--photo-sheet", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    configure_logging(args.verbose)
    create_trial(args.reference, args.clean_background, args.photo_sheet, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
