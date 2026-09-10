"""根据已发布模板的 slots 生成命名上传指南与 Bindings 起始文件。"""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_bytes, atomic_write_json
from .validate import validate_package

LOGGER = logging.getLogger(__name__)


def create_upload_guide(
    template_dir: Path,
    html_output: Path,
    bindings_output: Path,
    *,
    require_ready: bool = True,
) -> tuple[Path, Path]:
    """生成不上传任何数据的本地指南；客户按 slot_id 明确绑定素材。"""

    template = validate_package(template_dir, require_ready=require_ready)
    cards: list[str] = []
    starter_slots: dict[str, dict[str, Any]] = {}
    for slot in template["slots"]:
        if slot["type"] == "image":
            mode_note = {
                "photo": "整张照片按 cover/contain 裁切，不做人像抠图。",
                "photo_feather": "整张照片会应用模板边缘渐隐，不做人像抠图。",
                "cutout": "需要透明 PNG，或先显式运行 cutout 准备步骤。",
            }[slot["mode"]]
            starter_slots[slot["id"]] = {
                "path": f"REPLACE_{slot['id']}.png",
                "scale": 1.0,
                "offset_px": [0, 0],
            }
        else:
            mode_note = "输入简单文字；字体和换行由模板控制。"
            starter_slots[slot["id"]] = {"text": slot["default_text"] or ""}
        cards.append(
            "<section>"
            f"<h2>{html.escape(slot['label'])} <code>{html.escape(slot['id'])}</code></h2>"
            f"<p>{html.escape(slot['upload_hint'])}</p><p>{html.escape(mode_note)}</p>"
            "</section>"
        )
    document = f"""<!doctype html>
<!-- 本文件根据 TemplateSpec 自动生成，仅展示命名上传要求，不会联网。 -->
<meta charset="utf-8"><title>模板上传指南</title>
<style>body{{font:16px/1.5 system-ui;max-width:760px;margin:32px auto}}section{{padding:12px 18px;margin:12px 0;border:1px solid #bbb;border-radius:10px}}code{{font-size:.85em}}</style>
<h1>模板上传指南</h1>
<p>参考图和模板制作模型不会在客户渲染阶段再次调用。请编辑配套 Bindings JSON 中每个命名槽位。</p>
{"".join(cards)}
"""
    atomic_write_bytes(html_output, document.encode("utf-8"))
    atomic_write_json(
        bindings_output, {"version": "collage-bindings/1", "slots": starter_slots}
    )
    LOGGER.info(
        "上传指南已生成 | html=%s bindings=%s",
        html_output.resolve(),
        bindings_output.resolve(),
    )
    return html_output, bindings_output
