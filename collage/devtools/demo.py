"""生成无需外部凭据的 M1 合成演示及可重复验收产物。"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw

from ..core.io import atomic_save_image, atomic_write_json, sha256_file
from ..rendering import render_from_files
from ..template.build import build_template

LOGGER = logging.getLogger(__name__)


def _paper_background(size: tuple[int, int]) -> Image.Image:
    image = Image.new("RGB", size, "#E8DCC5")
    draw = ImageDraw.Draw(image)
    for y in range(0, size[1], 12):
        shade = 214 + (y // 12) % 4 * 3
        draw.line((0, y, size[0], y), fill=(shade, shade - 8, shade - 20), width=1)
    return image


def _customer_photo(size: tuple[int, int], colors: tuple[str, str]) -> Image.Image:
    image = Image.new("RGB", size, colors[0])
    draw = ImageDraw.Draw(image)
    for offset in range(-size[1], size[0], 24):
        draw.line((offset, 0, offset + size[1], size[1]), fill=colors[1], width=10)
    return image


def _cutout(size: tuple[int, int]) -> Image.Image:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        (35, 8, size[0] - 35, size[0] - 62), fill="#FFD166", outline="#513B56", width=5
    )
    draw.rounded_rectangle(
        (18, size[0] - 78, size[0] - 18, size[1] - 2),
        radius=35,
        fill="#EF476F",
        outline="#513B56",
        width=5,
    )
    return image


def _star(size: tuple[int, int]) -> Image.Image:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    points = [
        (60, 3),
        (74, 35),
        (116, 38),
        (83, 62),
        (94, 97),
        (60, 77),
        (26, 97),
        (37, 62),
        (4, 38),
        (46, 35),
    ]
    draw.polygon(points, fill="#8A5CF6", outline="#FFFFFF", width=5)
    return image


def create_demo(output_dir: Path) -> Path:
    """构建并试拼完全本地的模板；保留 needs_review 等待真人批准。"""

    root = output_dir.resolve()
    inputs = root / "inputs"
    review_dir = root / "review"
    renders_dir = root / "renders"
    workspace_dir = root / "workspace"
    for directory in (inputs, review_dir, renders_dir, workspace_dir):
        directory.mkdir(parents=True, exist_ok=True)
    canvas = (480, 360)
    clean_background = _paper_background(canvas)
    reference = clean_background.copy()
    draw = ImageDraw.Draw(reference)
    draw.rectangle((20, 60, 279, 299), fill="#8093F1")
    draw.rectangle((190, 40, 439, 339), fill="#A8D5BA")
    draw.text((25, 65), "OLD CONTENT", fill="white")
    star = _star((120, 100))
    reference.paste(star, (170, 140), star)
    remove_mask = Image.new("L", canvas, 0)
    mask_draw = ImageDraw.Draw(remove_mask)
    mask_draw.rectangle((18, 38, 442, 342), fill=255)
    photo = _customer_photo((360, 260), ("#118AB2", "#06D6A0"))
    cutout = _cutout((230, 300))
    paths = {
        "reference": inputs / "reference.png",
        "background": inputs / "background_candidate.png",
        "mask": inputs / "remove_mask.png",
        "overlay": inputs / "overlay_star.png",
        "photo": inputs / "customer_photo.png",
        "cutout": inputs / "customer_cutout.png",
    }
    for key, image in {
        "reference": reference,
        "background": clean_background,
        "mask": remove_mask,
        "overlay": star,
        "photo": photo,
        "cutout": cutout,
    }.items():
        atomic_save_image(image, paths[key])

    reviewed = {
        "version": "collage-build/4",
        "status": "planned",
        "reference": {
            "path": "../inputs/reference.png",
            "sha256": sha256_file(paths["reference"]),
        },
        "canvas": {
            "width": canvas[0],
            "height": canvas[1],
            "coordinate_space": "canvas_px",
            "rect_format": "xywh",
        },
        "slots": [
            {
                "id": "photo_left",
                "label": "左侧照片",
                "type": "image",
                "required": True,
                "mode": "photo",
                "source_rect": [20, 60, 260, 240],
                "target_rect": [20, 60, 260, 240],
                "upload_hint": "上传横向或方形照片",
                "review_notes": "演示槽",
                "rotation_deg": -3,
                "fit": "cover",
                "anchor": [0.5, 0.5],
                "clip_mask": None,
                "edge_fade_px": 0,
            },
            {
                "id": "person_main",
                "label": "右侧透明主体",
                "type": "image",
                "required": True,
                "mode": "cutout",
                "source_rect": [190, 40, 250, 300],
                "target_rect": [190, 40, 250, 300],
                "upload_hint": "上传透明 PNG",
                "review_notes": "演示槽",
                "rotation_deg": 0,
                "fit": "contain",
                "anchor": [0.5, 1.0],
                "clip_mask": None,
                "edge_fade_px": 0,
            },
        ],
        "overlays": [
            {
                "id": "star",
                "label": "紫色星形",
                "source_rect": [170, 140, 120, 100],
                "target_rect": [170, 140, 120, 100],
                "attachment": None,
                "action": "reference_generate",
                "generation_brief": "保持星形、紫色和白色描边；这是预制测试素材。",
                "requires_exact_content": False,
                "review_notes": "导入预制透明素材",
                "rotation_deg": 0,
                "prepared_asset": "../inputs/overlay_star.png",
                "background_mode": "alpha",
                "chroma_key": None,
                "chroma_tolerance": 40,
                "shape": None,
            }
        ],
        "background": {
            "background_brief": "延续米色纸纹，不保留旧色块。",
            "review_notes": "导入确定性测试底图",
            "remove_mask": "../inputs/remove_mask.png",
            "allowed_mask": None,
            "candidate_path": "../inputs/background_candidate.png",
            "expand_px": 0,
            "feather_px": 0,
        },
        "layer_order": [
            {"type": "background"},
            {"type": "slot", "id": "photo_left"},
            {"type": "overlay", "id": "star"},
            {"type": "slot", "id": "person_main"},
        ],
        "review": {
            "reviewer": "demo-author",
            "reviewed_at": "2026-09-09T00:00:00+00:00",
            "notes": "程序构造的 M1 测试",
            "questions_resolved": True,
        },
        "audit": {"fixture": False, "source": "programmatic-demo"},
    }
    reviewed_path = review_dir / "reviewed.json"
    bindings_path = renders_dir / "bindings.json"
    atomic_write_json(reviewed_path, reviewed)
    atomic_write_json(
        bindings_path,
        {
            "version": "collage-bindings/1",
            "slots": {
                "photo_left": {
                    "path": "../inputs/customer_photo.png",
                    "scale": 1.0,
                    "offset_px": [0, 0],
                },
                "person_main": {
                    "path": "../inputs/customer_cutout.png",
                    "scale": 1.0,
                    "offset_px": [0, 0],
                },
            },
        },
    )
    template_dir = root / "template"
    build_template(reviewed_path, template_dir, work_dir=workspace_dir)
    result_path = renders_dir / "result.png"
    render_from_files(template_dir, bindings_path, result_path, require_ready=False)
    LOGGER.info("M1 演示完成，模板仍等待人工批准 | result=%s", result_path)
    return result_path
