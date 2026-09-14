"""Write the local HTML inspection report for a template build."""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any

from ...core.io import atomic_write_bytes
from ...schemas.background import background_slot_id


def _write_inspection_report(
    work_dir: Path,
    output_dir: Path,
    spec: dict[str, Any],
    audits: list[dict[str, Any]],
    warnings: list[dict] | None = None,
) -> None:
    overlay_rows = "".join(
        f"<li>{html.escape(overlay['id'])}: <a href='previews/overlay_{html.escape(overlay['id'])}_edges.png'>浅/深底边缘预览</a></li>"
        for overlay in spec["overlays"]
        if (work_dir / "previews" / f"overlay_{overlay['id']}_edges.png").is_file()
    )
    warning_rows = "".join(
        f"<li>{html.escape(item['label'])}：{html.escape(item['message'])}"
        f"（{html.escape(item['code'])}）</li>"
        for item in warnings or []
    )
    provider_rows = "".join(
        f"<li>{html.escape(item['node'])}: {html.escape(item['name'])}, fixture={item['fixture']}, cache_hit={item['cache_hit']}</li>"
        for item in audits
    )
    package_rel = Path(os.path.relpath(output_dir, work_dir)).as_posix()
    composition = (
        "整张重建：采用完整清版候选"
        if spec["background"].get("composition_mode") == "full_candidate"
        else "局部保护：仅在删除区与融合边带替换像素"
    )
    photo_background = background_slot_id(spec)
    if photo_background:
        background_section = (
            "<h2>背景</h2><p>由客户上传的全屏照片提供，固定背景制作已跳过。</p>"
        )
    else:
        background_section = (
            f"<h2>背景制作</h2><p>{composition}</p>"
            '<img src="remove_mask.png" alt="remove mask"><img src="blend_mask.png" alt="blend mask">'
            '<img src="background_candidate.png" alt="background candidate"><img src="background.png" alt="protected background">'
        )
    automated = spec.get("provenance", {}).get("kind") in {
        "automatic",
        "fixture",
        "diagnostic",
    }
    status_text = (
        "needs_validation。制作来源已记录；探针和文件检查不能代替完整自动验收。"
        if automated
        else "needs_review。必须用新客户素材生成预览后，再执行 approve。"
    )
    document = f"""<!doctype html>
<!-- 本文件汇总模板制作产物，供人工视觉验收。 -->
<meta charset="utf-8"><title>Collage build inspection</title>
<style>body{{font:16px/1.5 system-ui;max-width:900px;margin:32px auto}}img{{max-width:44%;border:1px solid #aaa;margin:8px}}</style>
<h1>模板制作检查</h1>
<p>状态：{status_text}</p>
{background_section}
<h2>候选提示</h2><p>装饰未进行 VLM 复核，请结合整图确认效果。</p><ul>{warning_rows or "<li>没有本地处理提示</li>"}</ul>
<h2>Overlay 边缘</h2><ul>{overlay_rows or "<li>无 overlay</li>"}</ul>
<h2>调用审计</h2><ul>{provider_rows}</ul>
<p>模板清单：<a href="{html.escape(package_rel)}/template.json">template.json</a></p>
"""
    atomic_write_bytes(work_dir / "inspection.html", document.encode("utf-8"))
