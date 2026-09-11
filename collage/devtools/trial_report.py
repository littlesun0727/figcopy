"""Write a private, offline comparison page for real automatic-template experiments."""

from __future__ import annotations

import html
import json
from pathlib import Path

from ..core.io import atomic_write_bytes, read_json


def write_trial_report(root: Path, quality: dict, renders: list[dict]) -> Path:
    """Present source, diagnostic evidence, real replacements and unresolved checks without a release claim."""
    cards = []
    for label, relative in [
        ("原始 S02 样板", "reference.png"),
        ("首轮模型候选（可能有错误）", "structure_preview.png"),
        ("模板窗口探针", "probes/preview.png"),
        ("分离的固定前景", "foreground_preview.png"),
        *((r["group"] + " 真实换图", r["output"]) for r in renders),
    ]:
        if (root / relative).is_file():
            cards.append(
                f'<figure><a href="{html.escape(relative)}"><img src="{html.escape(relative)}"></a><figcaption>{html.escape(label)}</figcaption></figure>'
            )
    rows = []
    for render in renders:
        for item in render["composition"]:
            rows.append(
                "<tr>"
                + "".join(
                    f"<td>{html.escape(str(v))}</td>"
                    for v in (
                        render["group"],
                        item["slot"],
                        item["material"],
                        f"{item['head_visible_fraction']:.1%}",
                        item["cover_complete"],
                        item["zoom"],
                    )
                )
                + "</tr>"
            )
    request_rows = []
    transport_by_node = (
        {
            item["node"]: item
            for item in read_json(root / "usage.json").get("requests", [])
        }
        if (root / "usage.json").is_file()
        else {}
    )
    for path in sorted((root / "requests").glob("*.json")):
        request = read_json(path)
        transport = transport_by_node.get(request["node"], {})
        requested_model = (
            request.get("request_parameters", {}).get("model")
            or request.get("audit", {}).get("requested_model")
            or transport.get("requested_model")
            or "未记录"
        )
        request_rows.append(
            "<tr>"
            + "".join(
                f"<td>{html.escape(str(v))}</td>"
                for v in (
                    {
                        "structure": "首次结构分析",
                        "geometry_review": "几何与层序复核",
                        "geometry_review_service_retry": "单图复核重试",
                        "structure_resolution": "结构纠错（最多一次）",
                        "template_inspection": "模板辅助视觉检查",
                    }.get(request["node"], request["node"]),
                    requested_model,
                    {
                        "complete": "完成",
                        "running": "处理中",
                        "failed_or_uncertain": "失败，计费状态未知",
                    }.get(request["status"], request["status"]),
                    request.get("audit", {}).get(
                        "elapsed_ms", request.get("elapsed_ms")
                    ),
                    request.get("usage", {}).get("total_tokens"),
                    "未返回金额",
                )
            )
            + "</tr>"
        )
    if not rows:
        rows.append(
            '<tr><td colspan="6">本轮尚未生成客户换图；停止原因见上方检查记录。</td></tr>'
        )
    navigation = " · ".join(
        f'<a href="{relative}">{label}</a>'
        for label, relative in [
            ("检查 JSON", "quality.json"),
            ("运行记录", "run.json"),
            ("真实用量", "usage.json"),
            ("离线判定", "evaluation.md"),
            ("窗口报告", "probes/inspection.html"),
            ("模板配置", "template/template.json"),
            ("本地人像定位", "material_focus_preview.png"),
        ]
        if (root / relative).is_file()
    )
    document = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>S02 真实自动重建试验</title>
<style>
body{{font:16px/1.7 system-ui,sans-serif;background:#f3f4f6;color:#202630;margin:0;padding:30px}}
main{{max-width:1500px;margin:auto}}h1{{margin:0}}.note{{padding:16px;background:#fff4d4;border-radius:12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}}
figure{{margin:0;background:white;padding:12px;border-radius:12px}}img{{display:block;width:100%;max-height:680px;object-fit:contain}}
figcaption{{margin-top:8px;font-weight:600}}table{{border-collapse:collapse;background:white;width:100%;margin:16px 0}}
td,th{{border:1px solid #ddd;padding:8px;text-align:left}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:16px}}
</style><main>
<h1>S02 真实自动重建试验</h1>
<p class="note">这是开发阶段的真实模型试验。模板尚未完成正式验收。状态：{html.escape(quality.get("status", "running"))}。
客户照片仅用于本地处理，没有重新生成客户人物。原始模型错误和服务失败记录均保留。</p>
<p>{html.escape(quality.get("message", ""))}</p>
<div class="grid">{"".join(cards)}</div>
<h2>每个照片槽的构图检查</h2>
<p>可见比例根据本地人脸检测扩出的头部矩形与实际窗口遮挡计算。它用于发现裁切风险，并不等于头发分割或完整视觉验收。</p>
<table><tr><th>组合</th><th>槽位</th><th>素材</th><th>头部区域可见</th><th>填满窗口</th><th>缩放</th></tr>{"".join(rows)}</table>
<h2>真实调用记录</h2>
<table><tr><th>节点</th><th>请求模型</th><th>状态</th><th>耗时 ms</th><th>上游总 token</th><th>费用</th></tr>{"".join(request_rows)}</table>
<p>费用上限按用户要求设为不限；上游没有报告人民币金额，不能将缺失金额当成零费用。</p>
<details><summary>完整检查记录</summary><pre>{html.escape(json.dumps(quality, ensure_ascii=False, indent=2))}</pre></details>
<p>{navigation}</p>
</main></html>"""
    target = root / "comparison.html"
    atomic_write_bytes(target, document.encode("utf-8"))
    return target


def write_failure_report(root: Path, error) -> None:
    """Retain completed artifacts when an upstream or construction stage fails."""
    from ..core.io import atomic_write_json

    run = read_json(root / "run.json") if (root / "run.json").exists() else {}
    requests = [read_json(p) for p in sorted((root / "requests").glob("*.json"))]
    renders = [read_json(p) for p in sorted((root / "renders").glob("*/render.json"))]
    run.update(
        status="failed",
        error_code=error.code,
        production_acceptance=False,
        render_count=len(renders),
        real_model_calls=len(requests),
        image_generation_calls=0,
        customer_network_calls=0,
        cost_cny=None,
        cost_source="not_reported",
    )
    atomic_write_json(root / "run.json", run)
    quality = {
        "version": "auto-trial-quality/1",
        "status": "failed",
        "error_code": error.code,
        "message": error.message,
        "diagnostic_only": run.get("diagnostic_only", False),
        "known_structure_issues": run.get("known_structure_issues", []),
        "production_acceptance": False,
        "render_count": len(renders),
        "real_model_calls": len(requests),
        "customer_network_calls": 0,
        "fixture_template_used": False,
        "cost_cny": None,
        "cost_source": "not_reported",
        "remaining": [
            "real automatic geometry not accepted",
            "full A2 template acceptance incomplete",
        ],
    }
    atomic_write_json(root / "quality.json", quality)
    write_trial_report(root, quality, renders)
