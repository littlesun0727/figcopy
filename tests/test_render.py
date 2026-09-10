"""验证 layer_order、拟合、渐隐、cutout 门禁及像素级确定性。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from collage.core.errors import CollageError
from collage.core.io import (
    atomic_save_image,
    atomic_write_json,
    read_json,
    sha256_file,
)
from collage.rendering.composer import (
    PreparedBinding,
    _fit_to_rect,
    _render_text_slot,
    _rotate_and_place,
    _safe_alpha_composite,
    render_from_files,
    render_template,
)


def test_layer_order_changes_occlusion(asset_package_factory, tmp_path: Path) -> None:
    first = asset_package_factory(tmp_path / "first", ("red", "blue"))
    second = asset_package_factory(tmp_path / "second", ("blue", "red"))
    result_a = render_template(first, {}, require_ready=False)
    result_b = render_template(second, {}, require_ready=False)
    assert result_a.getpixel((9, 9))[:3] == (0, 0, 255)
    assert result_b.getpixel((9, 9))[:3] == (255, 0, 0)


def test_cover_and_contain_keep_aspect_ratio() -> None:
    source = Image.new("RGBA", (4, 2), "red")
    cover = _fit_to_rect(source, (4, 4), fit="cover", anchor=(0.5, 0.5))
    contain = _fit_to_rect(source, (4, 4), fit="contain", anchor=(0.5, 0.5))
    assert cover.getchannel("A").getextrema() == (255, 255)
    assert contain.getpixel((2, 0))[3] == 0
    assert contain.getpixel((2, 2))[3] == 255


def _slot_package(root: Path, *, mode: str, edge_fade_px: int = 0) -> tuple[Path, Path]:
    package = root / "template"
    (package / "assets").mkdir(parents=True)
    background_path = package / "assets" / "background.png"
    atomic_save_image(Image.new("RGBA", (30, 30), "white"), background_path)
    template = {
        "version": "collage-template/1",
        "status": "needs_review",
        "canvas": {
            "width": 30,
            "height": 30,
            "coordinate_space": "canvas_px",
            "rect_format": "xywh",
        },
        "assets": [
            {
                "id": "bg",
                "path": "assets/background.png",
                "role": "background",
                "requires_alpha": False,
                "sha256": sha256_file(background_path),
            }
        ],
        "slots": [
            {
                "id": "image",
                "type": "image",
                "label": "图片",
                "required": True,
                "upload_hint": "上传",
                "rect": [5, 5, 20, 20],
                "rotation_deg": 0,
                "mode": mode,
                "fit": "cover",
                "anchor": [0.5, 0.5],
                "clip_mask": None,
                "edge_fade_px": edge_fade_px,
            }
        ],
        "layers": [
            {
                "type": "asset",
                "asset_id": "bg",
                "rect": [0, 0, 30, 30],
                "rotation_deg": 0,
                "fit": "contain",
                "anchor": [0.5, 0.5],
            },
            {"type": "slot", "slot_id": "image"},
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
    input_path = root / "客户图片.png"
    atomic_save_image(Image.new("RGB", (20, 20), "red"), input_path)
    bindings_path = root / "绑定.json"
    atomic_write_json(
        bindings_path,
        {"version": "collage-bindings/1", "slots": {"image": {"path": "客户图片.png"}}},
    )
    return package, bindings_path


def test_photo_feather_does_not_require_cutout_and_fades_edges(tmp_path: Path) -> None:
    package, bindings = _slot_package(tmp_path, mode="photo_feather", edge_fade_px=5)
    output = tmp_path / "结果.png"
    render_from_files(package, bindings, output, require_ready=False)
    image = Image.open(output).convert("RGBA")
    # 白底合成后边缘更接近白色，中心保持红色。
    assert image.getpixel((5, 15))[1] > image.getpixel((15, 15))[1]
    assert read_json(output.with_suffix(".png.render.json"))["network_calls"] == 0


def test_opaque_cutout_requires_subject_alpha(tmp_path: Path) -> None:
    package, bindings = _slot_package(tmp_path, mode="cutout")
    with pytest.raises(CollageError) as caught:
        render_from_files(
            package, bindings, tmp_path / "result.png", require_ready=False
        )
    assert caught.value.code == "CUTOUT_PROVIDER_UNAVAILABLE"


def test_repeated_render_has_identical_decoded_pixels(
    asset_package_factory, tmp_path: Path
) -> None:
    package = asset_package_factory(tmp_path)
    one = render_template(package, {}, require_ready=False)
    two = render_template(package, {}, require_ready=False)
    assert one.tobytes() == two.tobytes()


def test_clockwise_rotation_clipping_and_semitransparent_composite() -> None:
    canvas = Image.new("RGBA", (8, 8), "white")
    local = Image.new("RGBA", (3, 3), (0, 0, 0, 0))
    local.putpixel((1, 0), (255, 0, 0, 255))
    _rotate_and_place(canvas, local, [4, 4, 3, 3], 90)
    assert canvas.getpixel((6, 5))[:3] == (255, 0, 0)
    # 目标矩形可越出画布且只保留交集。
    _rotate_and_place(canvas, Image.new("RGBA", (3, 3), "blue"), [-2, -2, 3, 3], 0)
    assert canvas.getpixel((0, 0))[:3] == (0, 0, 255)
    translucent = Image.new("RGBA", (1, 1), (255, 0, 0, 128))
    _safe_alpha_composite(canvas, translucent, 2, 2)
    red, green, blue, alpha = canvas.getpixel((2, 2))
    assert (red, green, blue, alpha) == (255, 127, 127, 255)


def test_basic_text_slot_uses_explicitly_approved_fallback(tmp_path: Path) -> None:
    canvas = Image.new("RGBA", (80, 30), (0, 0, 0, 0))
    slot = {
        "id": "caption",
        "type": "text",
        "label": "标题",
        "required": True,
        "upload_hint": "输入标题",
        "rect": [0, 0, 80, 30],
        "rotation_deg": 0,
        "default_text": None,
        "font_path": None,
        "font_size": 14,
        "fallback_approved": True,
        "color": "#2244CC",
        "align": "center",
        "max_lines": 2,
        "line_spacing": 1,
    }
    _render_text_slot(tmp_path, canvas, slot, PreparedBinding(text="Hello"))
    assert canvas.getchannel("A").getbbox() is not None
