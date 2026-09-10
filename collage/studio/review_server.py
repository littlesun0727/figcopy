"""提供仅监听本机的一页式 Draft 参数、层序和 remove mask 人工确认界面。"""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from ..core.errors import CollageError
from ..core.io import (
    atomic_save_image,
    atomic_write_json,
    read_json,
    resolve_input_path,
)
from ..imaging.operations import load_mask
from ..schemas import validate_draft
from ..template.review import (
    EDGE_FADE_MAX_PX,
    EDGE_FADE_RATIO,
    confirm_draft,
    default_overlay_review_fields,
    default_slot_review_fields,
    load_override_map,
    suggested_edge_fade_px,
    validate_override_map,
)

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 64 * 1024 * 1024
AUTO_MASK_PADDING_RATIO = 0.015
AUTO_MASK_MIN_PADDING_PX = 2
AUTO_MASK_MAX_PADDING_PX = 24


def _automatic_remove_mask(draft: dict[str, Any]) -> Image.Image:
    """用 Draft 的源图矩形自动生成白=删除的初始清版蒙版。"""

    width = int(draft["canvas"]["width"])
    height = int(draft["canvas"]["height"])
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    regions = [*draft["slots"], *draft["overlays"]]
    drawn = 0
    for item in regions:
        rect = item.get("source_rect")
        if not isinstance(rect, list) or len(rect) != 4:
            continue
        x, y, rect_width, rect_height = (float(value) for value in rect)
        padding = min(
            AUTO_MASK_MAX_PADDING_PX,
            max(
                AUTO_MASK_MIN_PADDING_PX,
                round(min(rect_width, rect_height) * AUTO_MASK_PADDING_RATIO),
            ),
        )
        left = max(0, int(x) - padding)
        top = max(0, int(y) - padding)
        right = min(width, round(x + rect_width) + padding)
        bottom = min(height, round(y + rect_height) + padding)
        if right <= left or bottom <= top:
            continue
        # Pillow 的矩形右下角为闭区间，因此减一保持计算后的尺寸不多一像素。
        draw.rectangle((left, top, right - 1, bottom - 1), fill=255)
        drawn += 1

    histogram = mask.histogram()
    covered_pixels = sum(histogram[1:])
    coverage = covered_pixels / (width * height) if width and height else 0.0
    LOGGER.info(
        "已根据 Draft 自动涂清版蒙版 | regions=%s bbox=%s coverage=%.1f%%",
        drawn,
        mask.getbbox(),
        coverage * 100,
    )
    return mask


def _build_review_options(
    draft: dict[str, Any],
    slot_overrides_path: Path | None,
    overlay_overrides_path: Path | None,
    *,
    background_expand_px: int,
    background_feather_px: int,
) -> dict[str, Any]:
    """生成确认页初值，同时保留命令行传入的高级覆盖配置。"""

    file_slot_overrides = load_override_map(slot_overrides_path)
    file_overlay_overrides = load_override_map(overlay_overrides_path)
    slot_ids = {item["id"] for item in draft["slots"]}
    overlay_ids = {item["id"] for item in draft["overlays"]}
    if unknown := set(file_slot_overrides) - slot_ids:
        raise CollageError(
            "UNKNOWN_OVERRIDE_ID",
            f"slot 覆盖包含未知 ID：{', '.join(sorted(unknown))}",
        )
    if unknown := set(file_overlay_overrides) - overlay_ids:
        raise CollageError(
            "UNKNOWN_OVERRIDE_ID",
            f"overlay 覆盖包含未知 ID：{', '.join(sorted(unknown))}",
        )

    slots: dict[str, dict[str, Any]] = {}
    suggestions: dict[str, int] = {}
    for source in draft["slots"]:
        fields = default_slot_review_fields(source)
        fields.update(file_slot_overrides.get(source["id"], {}))
        slots[source["id"]] = fields
        if (
            source["type"] == "text"
            and source.get("default_text") is None
            and fields.get("default_text")
        ):
            LOGGER.info(
                "从 Draft 已有识别结果回填默认文字 | slot=%s text=%s",
                source["id"],
                fields["default_text"],
            )
        if source["type"] == "image":
            suggestions[source["id"]] = suggested_edge_fade_px(source["target_rect"])

    overlays: dict[str, dict[str, Any]] = {}
    for source in draft["overlays"]:
        file_override = file_overlay_overrides.get(source["id"], {})
        fields = {
            **default_overlay_review_fields(),
            # 简化确认页默认允许近似制作；高级覆盖文件仍可显式要求精确素材。
            "requires_exact_content": False,
        }
        fields.update(file_override)
        overlays[source["id"]] = fields

    return {
        "slots": slots,
        "overlays": overlays,
        "edge_fade_ratio": EDGE_FADE_RATIO,
        "edge_fade_max_px": EDGE_FADE_MAX_PX,
        "edge_fade_suggestions": suggestions,
        "background": {
            "expand_px": background_expand_px,
            "feather_px": background_feather_px,
        },
    }


def _question_resolution_notes(questions: list[str], value: object) -> str:
    """要求逐题填写决定，并把问答保存到 ReviewedSpec 的审核说明。"""

    if not questions:
        return ""
    if not isinstance(value, list) or len(value) != len(questions):
        raise CollageError(
            "HUMAN_REVIEW_REQUIRED",
            "必须逐项回答 Draft 中的待确认问题",
            details={"question_count": len(questions)},
        )
    answers: list[str] = []
    for index, question in enumerate(questions):
        item = value[index]
        answer = item.get("answer") if isinstance(item, dict) else None
        echoed_question = item.get("question") if isinstance(item, dict) else None
        if (
            echoed_question != question
            or not isinstance(answer, str)
            or not answer.strip()
        ):
            raise CollageError(
                "HUMAN_REVIEW_REQUIRED",
                "必须逐项回答 Draft 中的待确认问题",
                details={"missing_answer_index": index},
            )
        answers.append(answer.strip())
    lines = ["确认页问题记录："]
    for question, answer in zip(questions, answers, strict=True):
        lines.extend((f"Q: {question}", f"A: {answer}"))
    return "\n".join(lines)


def _automatic_review_notes(
    draft: dict[str, Any],
    slot_overrides: dict[str, dict[str, Any]],
    overlay_overrides: dict[str, dict[str, Any]],
    *,
    questions_deferred: bool,
) -> str:
    """记录确认页代用户完成的非关键默认决策，便于后续审计和 debug。"""

    lines = ["确认页自动处理记录："]
    inferred_text = [
        source["id"]
        for source in draft["slots"]
        if source["type"] == "text"
        and source.get("default_text") is None
        and slot_overrides.get(source["id"], {}).get("default_text")
    ]
    fallback_fonts = [
        source["id"]
        for source in draft["slots"]
        if source["type"] == "text"
        and not slot_overrides.get(source["id"], {}).get("font_path")
        and slot_overrides.get(source["id"], {}).get("fallback_approved") is True
    ]
    approximate_overlays = [
        source["id"]
        for source in draft["overlays"]
        if source.get("requires_exact_content") is True
        and overlay_overrides.get(source["id"], {}).get("requires_exact_content")
        is False
    ]
    if inferred_text:
        lines.append("- 已从 Draft 槽位名称回填默认文字：" + ", ".join(inferred_text))
    if fallback_fonts:
        lines.append(
            "- 未指定字体文件，已批准使用本地通用字体：" + ", ".join(fallback_fonts)
        )
    if approximate_overlays:
        lines.append(
            "- 缺少精确透明素材，已改为近似制作：" + ", ".join(approximate_overlays)
        )
    if questions_deferred and draft.get("questions"):
        lines.append(
            f"- {len(draft['questions'])} 个 Draft 待确认问题按当前设置处理，未要求逐题填写。"
        )
    if len(lines) == 1:
        lines.append("- 无需额外自动决策。")
    return "\n".join(lines)


def _validate_review_decisions(
    draft: dict[str, Any],
    slot_overrides: dict[str, dict[str, Any]],
    overlay_overrides: dict[str, dict[str, Any]],
) -> None:
    """在写中间文件前检查页面可解决的 ReviewedSpec 门禁。"""

    blockers: list[str] = []
    slot_ids = {item["id"] for item in draft["slots"]}
    overlay_ids = {item["id"] for item in draft["overlays"]}
    if unknown := set(slot_overrides) - slot_ids:
        blockers.append(f"未知 slot：{', '.join(sorted(unknown))}")
    if unknown := set(overlay_overrides) - overlay_ids:
        blockers.append(f"未知 overlay：{', '.join(sorted(unknown))}")

    for source in draft["slots"]:
        fields = default_slot_review_fields(source)
        fields.update(slot_overrides.get(source["id"], {}))
        if source["type"] == "image" and source["mode"] == "photo_feather":
            fade = fields.get("edge_fade_px")
            if not (isinstance(fade, int) and fade > 0) and not fields.get("clip_mask"):
                blockers.append(f"{source['id']} 需要正数羽化宽度或 clip mask")
        if (
            source["type"] == "text"
            and not fields.get("font_path")
            and fields.get("fallback_approved") is not True
        ):
            blockers.append(f"{source['id']} 需要字体或明确批准 fallback")

    for source in draft["overlays"]:
        fields = {
            **default_overlay_review_fields(),
            "requires_exact_content": source["requires_exact_content"],
        }
        fields.update(overlay_overrides.get(source["id"], {}))
        if fields.get("requires_exact_content") is True and not fields.get(
            "prepared_asset"
        ):
            blockers.append(f"{source['id']} 的精确内容需要 prepared asset")

    if blockers:
        raise CollageError(
            "HUMAN_REVIEW_REQUIRED",
            "确认页面仍有未完成的制作决策",
            details={"blockers": blockers},
        )


def _background_parameter(payload: dict[str, Any], name: str, default: int) -> int:
    """读取确认页中的非负背景像素参数。"""

    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4096:
        raise CollageError("INVALID_REQUEST", f"{name} 必须是 0 到 4096 之间的整数")
    return value


HTML = r"""<!doctype html>
<!-- 本页用最少输入确认 Draft 的槽位和白=删除 mask；高级参数默认自动处理。 -->
<meta charset="utf-8">
<title>Collage Draft Review</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:14px/1.4 system-ui;background:#1d1f24;color:#eee}
main{display:grid;grid-template-columns:minmax(480px,1fr) 430px;height:100vh}
.stage{display:grid;place-items:center;overflow:auto;padding:18px}
.canvas-wrap{position:relative;line-height:0;box-shadow:0 8px 30px #0008}
canvas{max-width:calc(100vw - 480px);max-height:calc(100vh - 36px);touch-action:none}
aside{overflow:auto;padding:18px;background:#292c33}
h1{font-size:20px;margin-top:0}
h2{font-size:15px;margin:20px 0 8px}
.card{border:1px solid #555;border-radius:8px;padding:10px;margin:8px 0}
.row{display:flex;gap:8px;align-items:center;margin:7px 0;flex-wrap:wrap}
.row>*{min-width:0}
.stack{display:block;margin:8px 0}
.stack>input[type=text],.stack>textarea{display:block;width:100%;margin-top:4px}
input,select,button,textarea{font:inherit}
input[type=number]{width:76px}
input[type=text],textarea,select{background:#181a1f;color:#fff;border:1px solid #666;padding:5px}
textarea{min-height:58px;resize:vertical}
button{padding:6px 9px;border:0;border-radius:5px;cursor:pointer}
.primary{background:#5b7cfa;color:white}
.selected{outline:2px solid #70ddff}
.mode button.active{background:#f0b429}
.muted{color:#abb2bf}
.warning{color:#ffd479}
.auto{color:#9ee6b8}
.badge{display:inline-block;padding:1px 6px;border-radius:10px;background:#414650;color:#dce3ee;font-size:12px}
.advanced{margin-top:8px;color:#cbd2dc}
summary{cursor:pointer;color:#dce3ee;margin:6px 0}
.status{white-space:pre-wrap;padding:8px;background:#17191e;border-radius:6px}
code{overflow-wrap:anywhere}
</style>
<main>
  <div class="stage"><div class="canvas-wrap"><canvas id="view"></canvas></div></div>
  <aside>
    <h1>确认模板</h1>
    <p class="muted">通常只需检查蓝框/粉框位置并画出要从底图清除的区域。羽化、字体和装饰素材已自动处理。</p>
    <div class="row mode">
      <button id="boxMode" class="active">拖动框</button>
      <button id="drawMode">画删除区</button>
      <button id="eraseMode">擦除</button>
      <button id="resetMask">恢复自动涂层</button>
      <label>笔刷 <input id="brush" type="range" min="2" max="160" value="36"></label>
    </div>
    <div id="items"></div>

    <h2>清除旧内容</h2>
    <p id="maskHint" class="auto">正在自动标出需要清除的区域…</p>
    <label id="emptyMaskOption" class="warning" hidden>
      <input id="emptyMaskApproved" type="checkbox">
      这张图不需要清除旧内容
    </label>

    <details class="advanced">
      <summary>高级：清版参数</summary>
      <label class="stack">背景补全要求
        <textarea id="backgroundBrief"></textarea>
      </label>
      <div class="row">
        <label>删除区扩张 <input id="backgroundExpand" type="number" min="0" max="4096"></label>
        <label>融合羽化 <input id="backgroundFeather" type="number" min="0" max="4096"></label>
      </div>
    </details>
    <details class="advanced">
      <summary>高级：调整图层顺序</summary>
      <div id="layers"></div>
    </details>
    <p id="autoSummary" class="muted"></p>
    <h2>保存</h2>
    <div class="row"><button id="save" class="primary">确认并保存</button></div>
    <div id="status" class="status">正在载入…</div>
  </aside>
</main>
<script>
const view = document.querySelector('#view');
const ctx = view.getContext('2d');
const mask = document.createElement('canvas');
const mctx = mask.getContext('2d');
const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value).replace(
  /[&<>"']/g,
  character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character])
);

let draft;
let reviewOptions;
let image;
let mode = 'box';
let selected = null;
let drag = null;
let initialMaskData = null;

function setMode(next) {
  mode = next;
  ['box', 'draw', 'erase'].forEach(name => {
    $('#' + name + 'Mode').classList.toggle('active', name === next);
  });
}

$('#boxMode').onclick = () => setMode('box');
$('#drawMode').onclick = () => setMode('draw');
$('#eraseMode').onclick = () => setMode('erase');
$('#resetMask').onclick = () => {
  if (!initialMaskData) {
    return;
  }
  mctx.putImageData(initialMaskData, 0, 0);
  updateMaskState();
  render();
  $('#status').textContent = '已恢复自动生成的清除区域。';
};

function allItems() {
  return [
    ...draft.slots.map(item => ({kind: 'slot', item})),
    ...draft.overlays.map(item => ({kind: 'overlay', item})),
  ];
}

function suggestedFade(rect) {
  const shortSide = Math.min(Number(rect[2]), Number(rect[3]));
  const proportional = Math.max(1, Math.round(shortSide * reviewOptions.edge_fade_ratio));
  const halfSide = Math.max(1, Math.floor(shortSide / 2));
  return Math.min(proportional, halfSide, reviewOptions.edge_fade_max_px);
}

function render() {
  ctx.clearRect(0, 0, view.width, view.height);
  ctx.drawImage(image, 0, 0);
  ctx.save();
  ctx.globalAlpha = 0.34;
  ctx.drawImage(mask, 0, 0);
  ctx.restore();
  ctx.font = `${Math.max(12, view.width / 80)}px sans-serif`;
  for (const entry of allItems()) {
    const rect = entry.item.target_rect;
    ctx.strokeStyle = entry.kind === 'slot' ? '#00d4ff' : '#ff42c8';
    ctx.lineWidth = entry.item.id === selected ? 5 : 2;
    ctx.strokeRect(...rect);
    if (entry.item.id === selected) {
      ctx.fillStyle = ctx.strokeStyle;
      ctx.fillText(entry.item.label, rect[0] + 4, rect[1] + 16);
    }
    if (
      entry.kind === 'slot'
      && entry.item.type === 'image'
      && entry.item.mode === 'photo_feather'
    ) {
      const fade = Number(reviewOptions.slots[entry.item.id].edge_fade_px);
      if (fade > 0 && rect[2] > fade * 2 && rect[3] > fade * 2) {
        ctx.save();
        ctx.setLineDash([10, 7]);
        ctx.lineWidth = 2;
        ctx.strokeStyle = '#9af2ff';
        ctx.strokeRect(
          rect[0] + fade,
          rect[1] + fade,
          rect[2] - fade * 2,
          rect[3] - fade * 2,
        );
        ctx.restore();
      }
    }
  }
}

function updateRect(kind, id, index, rawValue) {
  const list = kind === 'slot' ? draft.slots : draft.overlays;
  const item = list.find(candidate => candidate.id === id);
  const oldSuggestion = kind === 'slot' && item.type === 'image'
    ? reviewOptions.edge_fade_suggestions[id]
    : null;
  item.target_rect[index] = Number(rawValue);
  if (kind === 'slot' && item.type === 'image') {
    const nextSuggestion = suggestedFade(item.target_rect);
    const settings = reviewOptions.slots[id];
    if (settings.edge_fade_px === oldSuggestion) {
      settings.edge_fade_px = nextSuggestion;
    }
    reviewOptions.edge_fade_suggestions[id] = nextSuggestion;
  }
  render();
}

function imageDecisionControls(item) {
  const settings = reviewOptions.slots[item.id];
  if (item.mode !== 'photo_feather') {
    return '<p class="muted">无需额外边缘设置。</p>';
  }
  return `
    <p class="auto">已按图片框大小自动设置 ${settings.edge_fade_px} px 柔和边缘。</p>
    <details class="advanced">
      <summary>高级：调整柔和边缘</summary>
      <label>宽度
        <input type="number" min="1" max="4096" value="${settings.edge_fade_px}"
          data-option-kind="slot" data-item-id="${escapeHtml(item.id)}"
          data-field="edge_fade_px"> px
      </label>
    </details>`;
}

function textDecisionControls(item) {
  const settings = reviewOptions.slots[item.id];
  return `
    <label class="stack">默认文字
      <input type="text" value="${escapeHtml(settings.default_text || '')}"
        maxlength="120"
        data-option-kind="slot" data-item-id="${escapeHtml(item.id)}"
        data-field="default_text" data-nullable="true">
    </label>
    <p class="auto">字体样式已自动处理。</p>
    <details class="advanced">
      <summary>高级：调整文字样式</summary>
      <div class="row">
        <label>字号
          <input type="number" min="1" max="2048" value="${settings.font_size}"
            data-option-kind="slot" data-item-id="${escapeHtml(item.id)}"
            data-field="font_size">
        </label>
        <label>颜色
          <input type="text" value="${escapeHtml(settings.color)}"
            data-option-kind="slot" data-item-id="${escapeHtml(item.id)}"
            data-field="color">
        </label>
        <label>对齐
          <select data-option-kind="slot" data-item-id="${escapeHtml(item.id)}"
            data-field="align">
            ${['left', 'center', 'right'].map(value =>
              `<option ${settings.align === value ? 'selected' : ''}>${value}</option>`
            ).join('')}
          </select>
        </label>
      </div>
    </details>`;
}

function overlayDecisionControls(item) {
  return item.action === 'reference_generate'
    ? '<p class="auto">装饰会按参考图近似制作，无需上传透明素材。</p>'
    : '<p class="auto">装饰会由程序自动绘制。</p>';
}

function decisionControls(kind, item) {
  if (kind === 'slot' && item.type === 'image') {
    return imageDecisionControls(item);
  }
  if (kind === 'slot' && item.type === 'text') {
    return textDecisionControls(item);
  }
  return overlayDecisionControls(item);
}

function renderItems() {
  const root = $('#items');
  const modeLabels = {
    photo: '普通照片',
    photo_feather: '柔和融合照片',
    cutout: '透明抠图',
    unknown: '请选择图片类型',
  };

  function itemCard(kind, item) {
    const rect = item.target_rect;
    const modeSelect = kind === 'slot' && item.type === 'image'
      ? `<select data-mode="${escapeHtml(item.id)}">
          ${['photo', 'photo_feather', 'cutout', 'unknown'].map(value =>
            `<option value="${value}" ${value === item.mode ? 'selected' : ''}>${modeLabels[value]}</option>`
          ).join('')}
        </select>`
      : '';
    const rectInputs = rect.map((value, index) =>
      `<input type="number" value="${value}" data-rect-kind="${kind}"
        data-item-id="${escapeHtml(item.id)}" data-rect-index="${index}">`
    );
    return `
      <div class="card ${item.id === selected ? 'selected' : ''}"
        data-select="${escapeHtml(item.id)}">
        <div class="row"><b>${escapeHtml(item.label)}</b>
          <span class="badge">${kind === 'slot' ? '可替换内容' : '固定装饰'}</span>
          ${modeSelect}
        </div>
        ${decisionControls(kind, item)}
        <details class="advanced">
          <summary>高级：调整位置和大小</summary>
          <div class="row">x ${rectInputs[0]} y ${rectInputs[1]}</div>
          <div class="row">宽 ${rectInputs[2]} 高 ${rectInputs[3]}</div>
        </details>
      </div>`;
  }

  const slotCards = draft.slots.map(item => itemCard('slot', item)).join('');
  const overlayCards = draft.overlays.map(item => itemCard('overlay', item)).join('');
  const overlays = draft.overlays.length
    ? `<details class="advanced">
        <summary>固定装饰 ${draft.overlays.length} 项（已自动处理）</summary>
        ${overlayCards}
      </details>`
    : '';
  root.innerHTML = '<h2>可替换内容</h2>' + slotCards + overlays;

  root.querySelectorAll('[data-select]').forEach(element => {
    element.onclick = event => {
      if (!event.target.closest('input,select,textarea,button,label,details')) {
        selected = element.dataset.select;
        renderItems();
        render();
      }
    };
  });
  root.querySelectorAll('[data-rect-kind]').forEach(element => {
    element.onchange = () => {
      updateRect(
        element.dataset.rectKind,
        element.dataset.itemId,
        Number(element.dataset.rectIndex),
        element.value,
      );
    };
  });
  root.querySelectorAll('[data-mode]').forEach(element => {
    element.onchange = () => {
      const item = draft.slots.find(candidate => candidate.id === element.dataset.mode);
      const settings = reviewOptions.slots[item.id];
      item.mode = element.value;
      settings.fit = item.mode === 'cutout' ? 'contain' : 'cover';
      settings.edge_fade_px = item.mode === 'photo_feather'
        ? reviewOptions.edge_fade_suggestions[item.id]
        : 0;
      renderItems();
      render();
    };
  });
  root.querySelectorAll('[data-option-kind]').forEach(element => {
    element.onchange = () => {
      const collection = element.dataset.optionKind === 'slot'
        ? reviewOptions.slots
        : reviewOptions.overlays;
      let value;
      if (element.type === 'checkbox') {
        value = element.checked;
      } else if (element.type === 'number') {
        value = Number(element.value);
      } else {
        value = element.value.trim();
        if (element.dataset.nullable === 'true' && value === '') {
          value = null;
        }
      }
      collection[element.dataset.itemId][element.dataset.field] = value;
      render();
    };
  });
}

function renderLayers() {
  const root = $('#layers');
  root.innerHTML = draft.layer_order.map((entry, index) => `
    <div class="row card">
      <span style="flex:1">${index}. ${escapeHtml(entry.type)}${entry.id ? ': ' + escapeHtml(entry.id) : ''}</span>
      <button data-up="${index}">↑</button><button data-down="${index}">↓</button>
    </div>`).join('');
  root.querySelectorAll('[data-up]').forEach(button => {
    button.onclick = () => move(Number(button.dataset.up), -1);
  });
  root.querySelectorAll('[data-down]').forEach(button => {
    button.onclick = () => move(Number(button.dataset.down), 1);
  });
}

function move(index, direction) {
  const target = index + direction;
  if (index === 0 || target <= 0 || target >= draft.layer_order.length) {
    return;
  }
  [draft.layer_order[index], draft.layer_order[target]] = [
    draft.layer_order[target],
    draft.layer_order[index],
  ];
  renderLayers();
}

function point(event) {
  const bounds = view.getBoundingClientRect();
  return [
    (event.clientX - bounds.left) * view.width / bounds.width,
    (event.clientY - bounds.top) * view.height / bounds.height,
  ];
}

view.onpointerdown = event => {
  const [x, y] = point(event);
  view.setPointerCapture(event.pointerId);
  if (mode === 'box') {
    const hit = allItems().reverse().find(entry => {
      const rect = entry.item.target_rect;
      return x >= rect[0] && x <= rect[0] + rect[2]
        && y >= rect[1] && y <= rect[1] + rect[3];
    });
    if (hit) {
      selected = hit.item.id;
      drag = {
        item: hit.item,
        x,
        y,
        originalX: hit.item.target_rect[0],
        originalY: hit.item.target_rect[1],
      };
      renderItems();
    }
  } else {
    drag = {};
    paint(x, y);
  }
};

view.onpointermove = event => {
  if (!drag) {
    return;
  }
  const [x, y] = point(event);
  if (mode === 'box' && drag.item) {
    drag.item.target_rect[0] = Math.round(drag.originalX + x - drag.x);
    drag.item.target_rect[1] = Math.round(drag.originalY + y - drag.y);
    render();
  } else if (mode !== 'box') {
    paint(x, y);
  }
};

view.onpointerup = () => {
  const editedMask = mode !== 'box';
  drag = null;
  if (editedMask) {
    updateMaskState();
  }
  renderItems();
};

function paint(x, y) {
  const radius = Number($('#brush').value) / 2;
  mctx.save();
  mctx.globalCompositeOperation = mode === 'erase' ? 'destination-out' : 'source-over';
  mctx.fillStyle = 'white';
  mctx.beginPath();
  mctx.arc(x, y, radius, 0, Math.PI * 2);
  mctx.fill();
  mctx.restore();
  render();
}

function maskHasPixels() {
  const pixels = mctx.getImageData(0, 0, mask.width, mask.height).data;
  for (let index = 3; index < pixels.length; index += 4) {
    if (pixels[index] > 0) {
      return true;
    }
  }
  return false;
}

function updateMaskState() {
  const hasPixels = maskHasPixels();
  $('#emptyMaskOption').hidden = hasPixels;
  if (hasPixels) {
    $('#emptyMaskApproved').checked = false;
  }
}

function preflightErrors() {
  const errors = [];
  for (const item of draft.slots) {
    const settings = reviewOptions.slots[item.id];
    if (item.mode === 'unknown') {
      errors.push(`${item.id} 的图片模式仍为 unknown`);
    }
    if (
      item.type === 'image'
      && item.mode === 'photo_feather'
      && Number(settings.edge_fade_px) <= 0
      && !settings.clip_mask
    ) {
      errors.push(`${item.label} 的自动柔和边缘参数无效`);
    }
  }
  for (const item of draft.overlays) {
    const settings = reviewOptions.overlays[item.id];
    if (settings.requires_exact_content === true && !settings.prepared_asset) {
      errors.push(`${item.label} 的高级配置要求精确素材，但没有提供素材`);
    }
  }
  if (!maskHasPixels() && !$('#emptyMaskApproved').checked) {
    errors.push('删除蒙版为空；请画出旧内容，或明确确认无需删除');
  }
  return errors;
}

async function load() {
  const [draftResponse, optionsResponse] = await Promise.all([
    fetch('/draft'),
    fetch('/review-options'),
  ]);
  draft = await draftResponse.json();
  reviewOptions = await optionsResponse.json();

  image = new Image();
  image.src = '/reference';
  await image.decode();
  view.width = mask.width = draft.canvas.width;
  view.height = mask.height = draft.canvas.height;

  const initial = new Image();
  initial.src = '/mask';
  await initial.decode();
  const temporary = document.createElement('canvas');
  temporary.width = mask.width;
  temporary.height = mask.height;
  const temporaryContext = temporary.getContext('2d');
  temporaryContext.drawImage(initial, 0, 0);
  const pixels = temporaryContext.getImageData(0, 0, temporary.width, temporary.height);
  for (let index = 0; index < pixels.data.length; index += 4) {
    const value = pixels.data[index];
    pixels.data[index] = pixels.data[index + 1] = pixels.data[index + 2] = 255;
    pixels.data[index + 3] = value;
  }
  mctx.putImageData(pixels, 0, 0);
  initialMaskData = mctx.getImageData(0, 0, mask.width, mask.height);
  updateMaskState();
  $('#maskHint').textContent = reviewOptions.initial_mask_source === 'draft_rects'
    ? '已根据照片、文字和装饰位置自动涂好；明显不对时再补画或擦除。'
    : '已载入指定的清除区域；明显不对时再补画或擦除。';

  $('#backgroundBrief').value = draft.background.background_brief;
  $('#backgroundBrief').oninput = event => {
    draft.background.background_brief = event.target.value;
  };
  $('#backgroundExpand').value = reviewOptions.background.expand_px;
  $('#backgroundFeather').value = reviewOptions.background.feather_px;
  selected = (draft.slots[0] || draft.overlays[0] || {}).id;
  renderItems();
  renderLayers();
  $('#autoSummary').textContent = draft.questions.length
    ? `${draft.questions.length} 个待确认问题本次按当前默认设置处理，无需填写。`
    : '没有额外问题需要填写。';
  render();
  $('#status').textContent = '就绪；清除区域已自动生成，检查后可直接保存。';
}

$('#save').onclick = async () => {
  const errors = preflightErrors();
  if (errors.length) {
    $('#status').textContent = '尚不能保存：\n- ' + errors.join('\n- ');
    return;
  }
  const copy = structuredClone(draft);
  copy.questions = [];
  $('#status').textContent = '正在校验并保存…';
  const response = await fetch('/save', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({
      draft: copy,
      mask_png: mask.toDataURL('image/png'),
      slot_overrides: reviewOptions.slots,
      overlay_overrides: reviewOptions.overlays,
      defer_questions: true,
      empty_mask_approved: $('#emptyMaskApproved').checked,
      background_expand_px: Number($('#backgroundExpand').value),
      background_feather_px: Number($('#backgroundFeather').value),
    }),
  });
  const result = await response.json();
  $('#status').textContent = response.ok
    ? `已保存：${result.path}`
    : `失败 [${result.code}]\n${result.message}\n${JSON.stringify(result.details || {}, null, 2)}`;
};

load().catch(error => {
  $('#status').textContent = '载入失败：' + error;
});
</script>"""


def _decode_mask(data_url: str, expected_size: tuple[int, int]) -> Image.Image:
    prefix = "data:image/png;base64,"
    if not data_url.startswith(prefix):
        raise CollageError("INVALID_MASK_PAYLOAD", "mask 必须是 PNG data URL")
    try:
        raw = base64.b64decode(data_url[len(prefix) :], validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            rgba = source.convert("RGBA")
            # 浏览器编辑画布以 alpha 表示删除强度，RGB 仅用于白色预览。
            mask = rgba.getchannel("A")
    except Exception as exc:
        raise CollageError("INVALID_MASK_PAYLOAD", "无法解码 mask PNG") from exc
    if mask.size != expected_size:
        raise CollageError("MASK_SIZE_MISMATCH", "界面保存的 mask 与工作画布尺寸不一致")
    return mask


def serve_review_ui(
    draft_path: Path,
    output_path: Path,
    *,
    reviewer: str,
    initial_mask_path: Path | None = None,
    allowed_mask_path: Path | None = None,
    background_candidate_path: Path | None = None,
    slot_overrides_path: Path | None = None,
    overlay_overrides_path: Path | None = None,
    background_expand_px: int = 0,
    background_feather_px: int = 0,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """启动本地确认页；成功保存后自动停止服务。"""

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise CollageError("UNSAFE_REVIEW_HOST", "确认界面只允许监听本机回环地址")
    draft_path = draft_path.resolve()
    output_path = output_path.resolve()
    draft = validate_draft(read_json(draft_path))
    reference_path = resolve_input_path(draft_path, draft["source"]["path"])
    canvas_size = (draft["canvas"]["width"], draft["canvas"]["height"])
    if initial_mask_path is not None:
        initial_mask = load_mask(
            initial_mask_path, canvas_size, name="初始 remove_mask"
        )
        initial_mask_source = "provided_file"
        LOGGER.info("使用用户提供的初始清版蒙版 | path=%s", initial_mask_path)
    else:
        initial_mask = _automatic_remove_mask(draft)
        initial_mask_source = "draft_rects"
    mask_bytes = io.BytesIO()
    initial_mask.save(mask_bytes, format="PNG")
    review_options = _build_review_options(
        draft,
        slot_overrides_path,
        overlay_overrides_path,
        background_expand_px=background_expand_px,
        background_feather_px=background_feather_px,
    )
    review_options["initial_mask_source"] = initial_mask_source
    feather_count = sum(
        1
        for slot in draft["slots"]
        if slot["type"] == "image" and slot["mode"] == "photo_feather"
    )
    LOGGER.info(
        "确认页制作参数已准备 | slots=%s overlays=%s photo_feather=%s questions=%s",
        len(draft["slots"]),
        len(draft["overlays"]),
        feather_count,
        len(draft["questions"]),
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "CollageReview/1"

        def log_message(self, format_string: str, *args: Any) -> None:
            LOGGER.debug("review-ui | " + format_string, *args)

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(
                    HTTPStatus.OK, "text/html; charset=utf-8", HTML.encode("utf-8")
                )
            elif self.path == "/draft":
                self._send(
                    HTTPStatus.OK,
                    "application/json",
                    json.dumps(draft, ensure_ascii=False).encode("utf-8"),
                )
            elif self.path == "/review-options":
                self._send(
                    HTTPStatus.OK,
                    "application/json",
                    json.dumps(review_options, ensure_ascii=False).encode("utf-8"),
                )
            elif self.path == "/reference":
                self._send(HTTPStatus.OK, "image/png", reference_path.read_bytes())
            elif self.path == "/mask":
                self._send(HTTPStatus.OK, "image/png", mask_bytes.getvalue())
            else:
                self._send(
                    HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found"
                )

        def do_POST(self) -> None:
            if self.path != "/save":
                self._send(
                    HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found"
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise CollageError("REQUEST_TOO_LARGE", "保存请求为空或超过 64 MiB")
                payload = json.loads(self.rfile.read(length))
                edited = payload["draft"]
                # 重新绑定程序元数据，页面只能修改 Draft 的语义字段。
                for field in (
                    "version",
                    "status",
                    "source",
                    "canvas",
                    "prompt_version",
                    "provider",
                    "created_at",
                ):
                    edited[field] = draft[field]
                validate_draft(edited)
                slot_overrides = validate_override_map(
                    payload.get("slot_overrides", {}), source="确认页面 slot 覆盖"
                )
                overlay_overrides = validate_override_map(
                    payload.get("overlay_overrides", {}),
                    source="确认页面 overlay 覆盖",
                )
                _validate_review_decisions(edited, slot_overrides, overlay_overrides)
                if payload.get("defer_questions") is True:
                    question_notes = ""
                else:
                    question_notes = _question_resolution_notes(
                        draft["questions"], payload.get("question_resolutions", [])
                    )
                automatic_notes = _automatic_review_notes(
                    draft,
                    slot_overrides,
                    overlay_overrides,
                    questions_deferred=payload.get("defer_questions") is True,
                )
                notes = "\n\n".join(
                    part for part in (question_notes, automatic_notes) if part
                )
                LOGGER.info(
                    "确认页自动策略已应用 | inferred_text=%s fallback_fonts=%s "
                    "approximate_overlays=%s deferred_questions=%s",
                    sum(
                        1
                        for source in draft["slots"]
                        if source["type"] == "text"
                        and source.get("default_text") is None
                        and slot_overrides.get(source["id"], {}).get("default_text")
                    ),
                    sum(
                        1
                        for source in draft["slots"]
                        if source["type"] == "text"
                        and slot_overrides.get(source["id"], {}).get(
                            "fallback_approved"
                        )
                        is True
                    ),
                    sum(
                        1
                        for source in draft["overlays"]
                        if source.get("requires_exact_content") is True
                        and overlay_overrides.get(source["id"], {}).get(
                            "requires_exact_content"
                        )
                        is False
                    ),
                    len(draft["questions"])
                    if payload.get("defer_questions") is True
                    else 0,
                )
                mask = _decode_mask(payload["mask_png"], canvas_size)
                if mask.getbbox() is None:
                    if payload.get("empty_mask_approved") is not True:
                        raise CollageError(
                            "EMPTY_REMOVE_MASK",
                            "删除蒙版为空；请画出需要清版的旧内容，或明确确认无需删除",
                        )
                    LOGGER.warning("模板作者明确接受空删除蒙版")
                else:
                    LOGGER.info("删除蒙版已确认 | bbox=%s", mask.getbbox())
                selected_expand_px = _background_parameter(
                    payload,
                    "background_expand_px",
                    background_expand_px,
                )
                selected_feather_px = _background_parameter(
                    payload,
                    "background_feather_px",
                    background_feather_px,
                )
                ui_draft_path = output_path.parent / "ui_confirmed_draft.json"
                ui_mask_path = output_path.parent / "remove_mask.png"
                atomic_write_json(ui_draft_path, edited)
                atomic_save_image(mask, ui_mask_path)
                confirm_draft(
                    ui_draft_path,
                    output_path,
                    remove_mask_path=ui_mask_path,
                    reviewer=reviewer,
                    allowed_mask_path=allowed_mask_path,
                    background_candidate_path=background_candidate_path,
                    slot_overrides_path=slot_overrides_path,
                    overlay_overrides_path=overlay_overrides_path,
                    slot_overrides_data=slot_overrides,
                    overlay_overrides_data=overlay_overrides,
                    background_expand_px=selected_expand_px,
                    background_feather_px=selected_feather_px,
                    notes=notes,
                )
                body = json.dumps(
                    {"ok": True, "path": str(output_path)}, ensure_ascii=False
                ).encode("utf-8")
                self._send(HTTPStatus.OK, "application/json", body)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            except (CollageError, KeyError, json.JSONDecodeError) as exc:
                error = (
                    exc
                    if isinstance(exc, CollageError)
                    else CollageError("INVALID_REQUEST", "保存请求格式错误")
                )
                body = json.dumps(error.as_dict(), ensure_ascii=False).encode("utf-8")
                self._send(HTTPStatus.BAD_REQUEST, "application/json", body)

    server = ThreadingHTTPServer((host, port), Handler)
    LOGGER.info("人工确认页已启动 | url=http://%s:%s", host, port)
    LOGGER.info("浏览器成功保存 reviewed.json 后服务会自动停止；也可按 Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("人工确认页已停止")
    finally:
        server.server_close()
