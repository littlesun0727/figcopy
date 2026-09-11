"""Measure slot visibility with local probes without granting publication approval."""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw

from ..core.errors import CollageError
from ..core.io import (
    atomic_save_image,
    atomic_write_bytes,
    atomic_write_json,
    decode_image,
    read_json,
    safe_package_path,
    sha256_file,
)
from ..imaging.operations import rect_to_box
from ..rendering.model import PreparedBinding
from ..rendering.service import _render_layers
from .validation import validate_package

LOGGER = logging.getLogger(__name__)
CHECKER_VERSION = "collage-window-probe/1"


def _probe_image(size: tuple[int, int], index: int, label: str) -> Image.Image:
    colors = ("#21B6A8", "#EBA63F", "#8788E8", "#DF688C", "#4BA9D5")
    image = Image.new("RGBA", size, colors[index % len(colors)])
    draw = ImageDraw.Draw(image)
    step = max(8, min(size) // 8)
    for y in range(0, size[1], step):
        for x in range(0, size[0], step):
            if (x // step + y // step) % 2:
                draw.rectangle((x, y, x + step - 1, y + step - 1), fill="#F1EEE4")
    if size[0] >= 40 and size[1] >= 24:
        draw.rectangle((2, 2, min(size[0] - 1, 160), 23), fill="#172331")
        draw.text((6, 6), f"{index + 1}: {label}", fill="white")
    return image


def _expectations(
    path: Path | None,
    size: tuple[int, int],
    slot_ids: set[str],
) -> tuple[dict[str, Image.Image], int]:
    if path is None:
        return {}, 1
    record = read_json(path)
    if (
        not isinstance(record, dict)
        or set(record)
        != {"version", "purpose", "canvas_size", "alpha_tolerance", "slots"}
        or record.get("version") != "collage-probe-expectations/1"
        or record.get("purpose") != "offline_evaluation"
        or record.get("canvas_size") != list(size)
        or not isinstance(record.get("slots"), dict)
        or set(record["slots"]) != slot_ids
    ):
        raise CollageError(
            "PROBE_EXPECTATIONS_INVALID",
            "评测规格必须匹配画布及全部图片槽，且标为 offline_evaluation",
        )
    tolerance = record["alpha_tolerance"]
    # 这里只容忍 8-bit alpha 运算的舍入；不把未标定的几何误差变成通过阈值。
    if type(tolerance) is not int or not 0 <= tolerance <= 2:
        raise CollageError(
            "PROBE_EXPECTATIONS_INVALID", "alpha_tolerance 必须为 0、1 或 2"
        )
    masks = {}
    for slot_id, relative in record["slots"].items():
        if not isinstance(relative, str):
            raise CollageError(
                "PROBE_EXPECTATIONS_INVALID", "评测 mask 路径必须是字符串"
            )
        mask = decode_image(safe_package_path(path.parent, relative), mode="L")
        if mask.size != size or mask.getbbox() is None:
            raise CollageError(
                "PROBE_EXPECTATIONS_INVALID", "评测 mask 必须与画布等大且非空"
            )
        masks[slot_id] = mask
    return masks, tolerance


def _pixel_count(mask: Image.Image, tolerance: int = 0) -> int:
    return sum(mask.histogram()[tolerance + 1 :])


def probe_template(
    template_dir: Path,
    output_dir: Path,
    *,
    expectations_path: Path | None = None,
) -> Path:
    """保存窗口贡献图及可选的离线评测；不修改模板，不调用 provider。"""

    root, out = template_dir.resolve(), output_dir.resolve()
    if out == root or out.is_relative_to(root) or root.is_relative_to(out):
        raise CollageError("PROBE_OUTPUT_CONFLICT", "探针输出目录必须与模板目录分离")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise CollageError("OUTPUT_EXISTS", "请为本次探针使用新的输出目录")
    template = validate_package(root, require_ready=False)
    size = (template["canvas"]["width"], template["canvas"]["height"])
    image_slots = [slot for slot in template["slots"] if slot["type"] == "image"]
    if not image_slots:
        raise CollageError("PROBE_NO_IMAGE_SLOTS", "模板没有可检查的图片槽")
    if size[0] * size[1] > 16_777_216 or len(image_slots) > 64:
        raise CollageError("PROBE_RESOURCE_LIMIT", "探针画布或槽位数量超出本地检查上限")
    expected, tolerance = _expectations(
        expectations_path,
        size,
        {slot["id"] for slot in image_slots},
    )
    out.mkdir(parents=True, exist_ok=True)
    prepared: dict[str, PreparedBinding] = {}
    for slot in template["slots"]:
        if slot["type"] == "text":
            prepared[slot["id"]] = PreparedBinding(text=slot["default_text"] or "")
    sizes = {}
    for index, slot in enumerate(image_slots):
        box = rect_to_box(slot["rect"])
        local_size = (box[2] - box[0], box[3] - box[1])
        sizes[slot["id"]] = local_size
        # 以不透明探针测量模板窗口。它不模拟新人物 alpha 或客户构图。
        pattern = _probe_image(local_size, index, slot["id"])
        atomic_save_image(pattern, out / "inputs" / f"{slot['id']}.png")
        prepared[slot["id"]] = PreparedBinding(image=pattern)
    LOGGER.info(
        "开始窗口探针检查 | slots=%s expectations=%s", len(image_slots), bool(expected)
    )
    manifest_hash = sha256_file(root / "template.json")
    preview = _render_layers(root, template, prepared)
    atomic_save_image(preview, out / "preview.png")
    records: list[dict[str, Any]] = []
    for slot in image_slots:
        slot_id = slot["id"]
        original = prepared[slot_id]
        prepared[slot_id] = PreparedBinding(
            image=Image.new("RGBA", sizes[slot_id], "black")
        )
        dark = _render_layers(root, template, prepared).convert("RGB")
        prepared[slot_id] = PreparedBinding(
            image=Image.new("RGBA", sizes[slot_id], "white")
        )
        light = _render_layers(root, template, prepared).convert("RGB")
        prepared[slot_id] = original
        # 只改变当前槽，白黑差值测出它穿过前景与后续槽位后的实际贡献。
        # 透明/半透明前景不能用矩形覆盖面积替代，也不能从颜色相似度猜窗口。
        channels = ImageChops.difference(light, dark).split()
        contribution = ImageChops.lighter(
            ImageChops.lighter(channels[0], channels[1]), channels[2]
        )
        relative = f"visibility/{slot_id}.png"
        atomic_save_image(contribution, out / relative)
        visible = _pixel_count(contribution)
        record: dict[str, Any] = {
            "slot_id": slot_id,
            "mode": slot["mode"],
            "visible_pixels": visible,
            "visible_bbox": contribution.getbbox(),
            "visibility_mask": relative,
            "visibility_sha256": sha256_file(out / relative),
            "status": "unverified" if visible else "failed",
            "code": "PROBE_EXPECTATIONS_MISSING" if visible else "PROBE_SLOT_HIDDEN",
        }
        if slot_id in expected:
            target = expected[slot_id]
            missing = _pixel_count(ImageChops.subtract(target, contribution), tolerance)
            unexpected = _pixel_count(
                ImageChops.subtract(contribution, target), tolerance
            )
            error = ImageChops.difference(target, contribution)
            expected_relative = f"expected/{slot_id}.png"
            atomic_save_image(target, out / expected_relative)
            record.update(
                missing_pixels=missing,
                unexpected_pixels=unexpected,
                max_alpha_error=error.getextrema()[1],
                expected_mask=expected_relative,
                expected_sha256=sha256_file(out / expected_relative),
                status="passed"
                if visible and not missing and not unexpected
                else "failed",
                code="PROBE_WINDOW_MATCH"
                if visible and not missing and not unexpected
                else "PROBE_WINDOW_MISMATCH",
            )
        records.append(record)
        LOGGER.debug(
            "窗口检查 | slot=%s code=%s pixels=%s", slot_id, record["code"], visible
        )
    validate_package(root, require_ready=False)
    if sha256_file(root / "template.json") != manifest_hash:
        raise CollageError(
            "PROBE_INPUT_CHANGED", "测量过程中模板发生变化，请使用固定版本"
        )
    failed = any(item["status"] == "failed" for item in records)
    status = "failed" if failed else "passed" if expected else "unverified"
    report = {
        "version": CHECKER_VERSION,
        "scope": "template_window_visibility",
        "status": status,
        "production_acceptance": False,
        "probe_fixture_used": True,
        "template_fixture_used": template["build"]["fixture_used"],
        "template_manifest_sha256": manifest_hash,
        "expectations_sha256": sha256_file(expectations_path)
        if expectations_path
        else None,
        "evaluation_only": bool(expected),
        "alpha_tolerance": tolerance,
        "network_calls": 0,
        "preview_sha256": sha256_file(out / "preview.png"),
        "slots": records,
        "not_checked": [
            "source_photo_residue",
            "semantic_content",
            "customer_composition",
            "subject_alpha",
        ],
    }
    atomic_write_json(out / "quality.json", report)
    rows = "".join(
        f"<tr><td>{html.escape(item['slot_id'])}</td><td>{item['visible_pixels']}</td>"
        f"<td>{item['status']}</td><td>{item['code']}</td></tr>"
        for item in records
    )
    document = f"""<!doctype html><meta charset="utf-8">
<title>窗口探针检查</title>
<style>body{{font:16px/1.6 system-ui;max-width:1000px;margin:32px auto;padding:16px}}
img{{max-width:100%}}td,th{{text-align:left;padding:8px;border-bottom:1px solid #ddd}}</style>
<h1>窗口探针检查：{status}</h1>
<p>使用程序构造的编号素材，仅检查窗口可见范围；此报告不构成自动发布验收。</p>
<img src="preview.png" alt="编号窗口与固定前景">
<table><tr><th>槽位</th><th>可见像素</th><th>结果</th><th>检查代码</th></tr>{rows}</table>
<p>旧照片残留、内容语义、客户构图与人物 alpha 尚未检查。</p>
<a href="quality.json">检查证据 JSON</a>"""
    atomic_write_bytes(out / "inspection.html", document.encode("utf-8"))
    LOGGER.info("窗口探针检查完成 | status=%s failed=%s", status, failed)
    return out / "quality.json"
