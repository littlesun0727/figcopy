"""为模板校验与渲染测试构造最小、完全本地的图片包。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from collage.io_utils import atomic_save_image, atomic_write_json, sha256_file


@pytest.fixture
def asset_package_factory():
    """返回可按指定 overlay 顺序构造模板包的工厂。"""

    def factory(root: Path, order: tuple[str, ...] = ("red", "blue")) -> Path:
        package = root / "template"
        assets_dir = package / "assets"
        assets_dir.mkdir(parents=True)
        images = {
            "bg": Image.new("RGBA", (20, 20), "white"),
            "red": Image.new("RGBA", (10, 10), "red"),
            "blue": Image.new("RGBA", (10, 10), "blue"),
        }
        paths: dict[str, Path] = {}
        for name, image in images.items():
            path = assets_dir / f"{name}.png"
            atomic_save_image(image, path)
            paths[name] = path
        assets = [
            {
                "id": "bg",
                "path": "assets/bg.png",
                "role": "background",
                "requires_alpha": False,
                "sha256": sha256_file(paths["bg"]),
            },
            {
                "id": "red",
                "path": "assets/red.png",
                "role": "overlay",
                "requires_alpha": False,
                "sha256": sha256_file(paths["red"]),
            },
            {
                "id": "blue",
                "path": "assets/blue.png",
                "role": "overlay",
                "requires_alpha": False,
                "sha256": sha256_file(paths["blue"]),
            },
        ]
        layer_by_id: dict[str, dict[str, Any]] = {
            "red": {
                "type": "asset",
                "asset_id": "red",
                "rect": [4, 4, 10, 10],
                "rotation_deg": 0,
                "fit": "contain",
                "anchor": [0.5, 0.5],
            },
            "blue": {
                "type": "asset",
                "asset_id": "blue",
                "rect": [8, 8, 10, 10],
                "rotation_deg": 0,
                "fit": "contain",
                "anchor": [0.5, 0.5],
            },
        }
        template = {
            "version": "collage-template/1",
            "status": "needs_review",
            "canvas": {
                "width": 20,
                "height": 20,
                "coordinate_space": "canvas_px",
                "rect_format": "xywh",
            },
            "assets": assets,
            "slots": [],
            "layers": [
                {
                    "type": "asset",
                    "asset_id": "bg",
                    "rect": [0, 0, 20, 20],
                    "rotation_deg": 0,
                    "fit": "contain",
                    "anchor": [0.5, 0.5],
                },
                *(layer_by_id[name] for name in order),
            ],
            "build": {
                "source_sha256": "0" * 64,
                "created_at": "2026-09-09T00:00:00+00:00",
                "tool_version": "test",
                "fixture_used": False,
                "providers": [],
            },
            "review": {
                "visual_approved": False,
                "reviewer": None,
                "reviewed_at": None,
                "notes": "",
                "evidence_sha256": [],
            },
        }
        atomic_write_json(package / "template.json", template)
        return package

    return factory


def make_reviewed_spec(root: Path, *, candidate: bool = True) -> Path:
    """创建只有背景节点的有效 ReviewedSpec。"""

    reference = Image.new("RGB", (24, 16), "#DDD0B0")
    reference_path = root / "reference.png"
    mask_path = root / "remove.png"
    candidate_path = root / "candidate.png"
    atomic_save_image(reference, reference_path)
    mask = Image.new("L", reference.size, 0)
    for y in range(4, 12):
        for x in range(6, 18):
            mask.putpixel((x, y), 255)
    atomic_save_image(mask, mask_path)
    atomic_save_image(Image.new("RGB", reference.size, "#88AACC"), candidate_path)
    spec = {
        "version": "collage-reviewed/1",
        "status": "reviewed",
        "reference": {"path": "reference.png", "sha256": sha256_file(reference_path)},
        "canvas": {
            "width": 24,
            "height": 16,
            "coordinate_space": "canvas_px",
            "rect_format": "xywh",
        },
        "slots": [],
        "overlays": [],
        "background": {
            "background_brief": "延续纸纹",
            "review_notes": "测试",
            "remove_mask": "remove.png",
            "allowed_mask": None,
            "candidate_path": "candidate.png" if candidate else None,
            "expand_px": 0,
            "feather_px": 0,
        },
        "layer_order": [{"type": "background"}],
        "review": {
            "reviewer": "tester",
            "reviewed_at": "2026-09-09T00:00:00+00:00",
            "notes": "",
            "questions_resolved": True,
        },
        "audit": {},
    }
    path = root / "reviewed.json"
    atomic_write_json(path, spec)
    return path
