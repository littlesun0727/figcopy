"""Create a fully synthetic four-window benchmark for A1 geometry checks."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

from ..core.errors import CollageError
from ..core.io import atomic_save_image, atomic_write_json, sha256_file, stable_hash
from ..template.build import build_template
from ..template.probes import probe_template

LOGGER = logging.getLogger(__name__)


def create_window_demo(output_dir: Path, *, run_probe: bool = True) -> Path:
    """构建固定规格 fixture；评测 mask 独立保存，绝不作为自动识别输入。"""

    root = output_dir.resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise CollageError("OUTPUT_EXISTS", "窗口演示需要新的输出目录")
    root.mkdir(parents=True, exist_ok=True)
    size = (480, 360)
    background = Image.new("RGB", size, "#EAE4D9")
    atomic_save_image(background, root / "inputs" / "reference.png")
    atomic_save_image(background, root / "inputs" / "background.png")
    atomic_save_image(Image.new("L", size, 0), root / "inputs" / "remove.png")

    rects = {
        "main_photo": [0, 0, 480, 360],
        "photo_left": [32, 48, 140, 104],
        "photo_right": [294, 38, 144, 108],
        "photo_bottom": [250, 222, 160, 104],
    }
    windows: dict[str, Image.Image] = {}
    slots = []
    for slot_id, rect in rects.items():
        x, y, width, height = rect
        local = Image.new("L", (width, height), 255)
        if slot_id == "photo_bottom":
            # 用已知不规则纸边验证局部窗口，坐标不来源于任何真实样板。
            ImageDraw.Draw(local).polygon(((0, 0), (22, 0), (0, 22)), fill=0)
        atomic_save_image(local, root / "inputs" / f"{slot_id}_clip.png")
        window = Image.new("L", size, 0)
        window.paste(local, (x, y))
        windows[slot_id] = window
        slots.append(
            {
                "id": slot_id,
                "label": slot_id,
                "type": "image",
                "required": True,
                "mode": "photo",
                "source_rect": rect,
                "target_rect": rect,
                "upload_hint": "程序探针，仅供离线评测",
                "review_notes": "synthetic fixture",
                "rotation_deg": 0,
                "fit": "cover",
                "anchor": [0.5, 0.5],
                "clip_mask": f"inputs/{slot_id}_clip.png",
                "edge_fade_px": 0,
            }
        )

    foreground = Image.new("RGBA", size)
    draw = ImageDraw.Draw(foreground)
    draw.rectangle((68, 42, 116, 64), fill="#F1C85D")
    draw.rectangle((330, 30, 385, 56), fill=(242, 221, 151, 128))
    atomic_save_image(foreground, root / "inputs" / "foreground.png")
    overlays = [
        {
            "id": "tape",
            "label": "opaque and translucent tape",
            "source_rect": [0, 0, *size],
            "target_rect": [0, 0, *size],
            "action": "preserve",
            "generation_brief": "",
            "requires_exact_content": True,
            "review_notes": "synthetic foreground",
            "rotation_deg": 0,
            "prepared_asset": "inputs/foreground.png",
            "background_mode": "alpha",
            "chroma_key": None,
            "chroma_tolerance": 40,
            "shape": None,
        }
    ]
    # 现有 basic_shape 制作可执行框线；不通过模型生成虚线。
    overlays.append(
        {
            "id": "frame",
            "label": "programmatic dashed frame",
            "source_rect": [24, 40, 156, 120],
            "target_rect": [24, 40, 156, 120],
            "action": "basic_shape",
            "generation_brief": "",
            "requires_exact_content": False,
            "review_notes": "synthetic frame",
            "rotation_deg": 0,
            "prepared_asset": None,
            "background_mode": "alpha",
            "chroma_key": None,
            "chroma_tolerance": 40,
            "shape": {
                "kind": "dashed_rectangle",
                "fill": None,
                "outline": "#FFFFFF",
                "width": 3,
                "radius": 0,
                "dash": 10,
                "gap": 6,
            },
        }
    )
    spec = {
        "version": "collage-build/2",
        "status": "planned",
        "provenance": {
            "kind": "fixture",
            "policy_version": "auto-rebuild-policy/1",
            "evidence_sha256": [
                stable_hash({"fixture": "four-windows/1", "rects": rects})
            ],
            "unresolved": [],
        },
        "reference": {
            "path": "inputs/reference.png",
            "sha256": sha256_file(root / "inputs/reference.png"),
        },
        "canvas": {
            "width": size[0],
            "height": size[1],
            "coordinate_space": "canvas_px",
            "rect_format": "xywh",
        },
        "slots": slots,
        "overlays": overlays,
        "background": {
            "background_brief": "synthetic paper",
            "review_notes": "fixture",
            "remove_mask": "inputs/remove.png",
            "allowed_mask": None,
            "candidate_path": "inputs/background.png",
            "expand_px": 0,
            "feather_px": 0,
        },
        "layer_order": [
            {"type": "background"},
            *({"type": "slot", "id": slot_id} for slot_id in rects),
            {"type": "overlay", "id": "frame"},
            {"type": "overlay", "id": "tape"},
        ],
    }
    atomic_write_json(root / "build.json", spec)
    build_template(root / "build.json", root / "template", work_dir=root / "workspace")

    # 评测基准来自已知 fixture 设计；不读取探针测量结果来制造“预期值”。
    # 框线在小窗口之外，只影响 main_photo。独立绘制其设计覆盖范围。
    frame_alpha = Image.new("L", size, 0)
    frame_draw = ImageDraw.Draw(frame_alpha)
    for start in range(24, 180, 16):
        frame_draw.line((start, 40, min(start + 10, 179), 40), fill=255, width=3)
        frame_draw.line((start, 159, min(start + 10, 179), 159), fill=255, width=3)
    for start in range(40, 160, 16):
        frame_draw.line((24, start, 24, min(start + 10, 159)), fill=255, width=3)
        frame_draw.line((179, start, 179, min(start + 10, 159)), fill=255, width=3)
    # 局部框线的笔画在素材边界裁切；画布上的评测边界也必须遵守这个约定。
    frame_extent = Image.new("L", size, 0)
    ImageDraw.Draw(frame_extent).rectangle((24, 40, 179, 159), fill=255)
    frame_alpha = ImageChops.multiply(frame_alpha, frame_extent)
    allowed_foreground = ImageChops.lighter(frame_alpha, foreground.getchannel("A"))
    ids = list(rects)
    expectations = {}
    for index, slot_id in enumerate(ids):
        visible = windows[slot_id].copy()
        for later in ids[index + 1 :]:
            visible = ImageChops.multiply(visible, ImageChops.invert(windows[later]))
        visible = ImageChops.multiply(visible, ImageChops.invert(allowed_foreground))
        relative = f"masks/{slot_id}.png"
        atomic_save_image(visible, root / "evaluation" / relative)
        expectations[slot_id] = relative
    atomic_write_json(
        root / "evaluation" / "expectations.json",
        {
            "version": "collage-probe-expectations/1",
            "purpose": "offline_evaluation",
            "canvas_size": list(size),
            "alpha_tolerance": 1,
            "slots": expectations,
        },
    )
    LOGGER.info("A1 四窗口 fixture 已构建 | windows=4 model_calls=0")
    if run_probe:
        return probe_template(
            root / "template",
            root / "probes",
            expectations_path=root / "evaluation" / "expectations.json",
        )
    return root / "template" / "template.json"
