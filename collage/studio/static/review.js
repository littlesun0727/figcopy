const view = document.querySelector('#view');
const ctx = view.getContext('2d');
const mask = document.createElement('canvas');
const mctx = mask.getContext('2d');
const $ = selector => document.querySelector(selector);
const apiBase = document.querySelector('meta[name="review-api-base"]')?.content || '';
const returnUrl = document.querySelector('meta[name="review-return-url"]')?.content || '';
const csrfToken = document.querySelector('meta[name="figcopy-csrf-token"]')?.content || '';
const endpoint = path => `${apiBase}${path}`;
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
let overlayTextReviewOpened = false;
let overlayDetailsOpen = false;
let initialOverlayText = {};
let backgroundSelection = null;
let fixedBackground = null;
const photoBackgroundId = () => draft?.background?.mode === 'slot' ? draft.background.slot_id : null;

function eligibleBackgroundSlots() {
  return draft.slots.filter(item => {
    const fields = reviewOptions.slots[item.id];
    return item.type === 'image' && item.mode === 'photo'
      && JSON.stringify(item.target_rect) === JSON.stringify([0, 0, draft.canvas.width, draft.canvas.height])
      && fields.required === true && fields.fit === 'cover' && fields.rotation_deg === 0
      && !fields.clip_mask && fields.edge_fade_px === 0;
  });
}

function renderBackgroundSource() {
  const id = photoBackgroundId();
  const candidates = eligibleBackgroundSlots();
  $('#backgroundSource').value = id ? 'slot' : 'fixed';
  $('#backgroundSlotControl').hidden = !id;
  $('#backgroundSlot').innerHTML = candidates.map(item =>
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)} · ${escapeHtml(item.id)}</option>`).join('');
  if (id) $('#backgroundSlot').value = id;
  $('#backgroundSourceHint').textContent = id
    ? '客户背景照片必须不透明并铺满画布；跳过固定背景制作，装饰仍独立处理。'
    : candidates.length ? '当前仍使用固定底板。发现可用的满版照片槽；仅当客户会替换整个背景时才切换。'
      : '保留固定底板；清版与保护区域按下方设置处理。暂无符合满版约束的普通照片槽。';
  $('#backgroundControls').hidden = Boolean(id);
  $('#photoBackgroundHint').hidden = !id;
  for (const name of ['drawMode', 'eraseMode', 'resetMask', 'brushControl']) $('#' + name).hidden = Boolean(id);
  if (id) setMode('box');
  updateMaskState();
}

function selectBackground(mode, slotId = null) {
  const candidate = eligibleBackgroundSlots().find(item => item.id === slotId) || eligibleBackgroundSlots()[0];
  if (mode === 'slot' && !candidate) {
    renderBackgroundSource();
    $('#status').textContent = '不能使用照片背景：需先有必填、满版、无旋转、无开洞或羽化的普通照片槽。';
    return;
  }
  $('#finalConfirmed').checked = false;
  if (!photoBackgroundId()) fixedBackground = structuredClone(draft.background);
  if (mode === 'slot') {
    draft.background = {mode: 'slot', slot_id: candidate.id, review_notes: '用户选择客户满版照片作为背景'};
    draft.layer_order = [{type: 'slot', id: candidate.id}, ...draft.layer_order.filter(layer =>
      layer.type !== 'background' && !(layer.type === 'slot' && layer.id === candidate.id))];
    backgroundSelection = {mode: 'slot', slot_id: candidate.id};
  } else {
    draft.background = structuredClone(fixedBackground || {background_brief: '', review_notes: '请复核固定底板制作说明与删除区域'});
    draft.layer_order = [{type: 'background'}, ...draft.layer_order.filter(layer => layer.type !== 'background')];
    $('#backgroundBrief').value = draft.background.background_brief;
    backgroundSelection = {mode: 'fixed'};
  }
  // The server derives the schema version and validates this order independently.
  draft.version = mode === 'slot' ? 'collage-draft/2' : 'collage-draft/1';
  renderItems(); renderLayers(); renderBackgroundSource(); render();
  $('#status').textContent = '背景来源已修改，请检查并重新勾选整体确认；尚未调用模型。';
}

$('#backgroundSource').onchange = event => selectBackground(event.target.value);
$('#backgroundSlot').onchange = event => selectBackground('slot', event.target.value);

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
  $('#finalConfirmed').checked = false;
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
  if (!photoBackgroundId()) ctx.drawImage(mask, 0, 0);
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
  if (id === photoBackgroundId()) return;
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
  const settings = reviewOptions.overlays[item.id];
  return (item.action === 'basic_shape'
    ? '<p class="auto">简单图形由程序绘制，保持独立图层。</p>'
    : '<p class="auto">装饰将根据参考图生成完整的独立素材，并检查边缘与内容。</p>')
    + `<label class="stack">素材中的完整文字（无文字则留空）
      <input type="text" maxlength="500" value="${escapeHtml(settings.text_content || '')}"
        data-option-kind="overlay" data-item-id="${escapeHtml(item.id)}"
        data-field="text_content" data-nullable="true">
      </label>
      <label><input type="checkbox" ${settings.text_confirmed ? 'checked' : ''}
        data-option-kind="overlay" data-item-id="${escapeHtml(item.id)}"
        data-field="text_confirmed">我已逐字核对上述文字</label>`;
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
  const previousDetails = $('#overlayDetails');
  if (previousDetails) {
    // toggle is asynchronous; read the live state before rebuilding the cards.
    overlayDetailsOpen = previousDetails.open;
    if (previousDetails.open && !overlayTextReviewOpened) {
      overlayTextReviewOpened = true;
      $('#finalConfirmed').checked = false;
    }
  }
  const modeLabels = {
    photo: '普通照片',
    photo_feather: '柔和融合照片',
    cutout: '透明抠图',
    unknown: '请选择图片类型',
  };

  function itemCard(kind, item) {
    const rect = item.target_rect;
    const modeSelect = kind === 'slot' && item.type === 'image'
      ? `<select data-mode="${escapeHtml(item.id)}" ${item.id === photoBackgroundId() ? 'disabled' : ''}>
          ${['photo', 'photo_feather', 'cutout', 'unknown'].map(value =>
            `<option value="${value}" ${value === item.mode ? 'selected' : ''}>${modeLabels[value]}</option>`
          ).join('')}
        </select>`
      : '';
    const rectInputs = rect.map((value, index) =>
      `<input type="number" value="${value}" data-rect-kind="${kind}"
        data-item-id="${escapeHtml(item.id)}" data-rect-index="${index}" ${item.id === photoBackgroundId() ? 'disabled' : ''}>`
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
    ? `<details class="advanced" id="overlayDetails" ${overlayDetailsOpen ? 'open' : ''}>
        <summary>固定装饰 ${draft.overlays.length} 项（分别制作）</summary>
        <p class="muted">未展开时沿用识别文字；展开后请核对非空文字并勾选确认。</p>
        ${overlayCards}
      </details>`
    : '';
  root.innerHTML = '<h2>可替换内容</h2>' + slotCards + overlays;
  const overlayDetails = $('#overlayDetails');
  if (overlayDetails) overlayDetails.ontoggle = () => {
    if (!overlayDetails.isConnected) return;
    overlayDetailsOpen = overlayDetails.open;
    if (overlayDetails.open && !overlayTextReviewOpened) {
      overlayTextReviewOpened = true;
      $('#finalConfirmed').checked = false;
    }
  };

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
      if (element.dataset.optionKind === 'overlay' && element.dataset.field === 'text_content') {
        overlayTextReviewOpened = true;
        collection[element.dataset.itemId].text_confirmed = false;
        const checkbox = root.querySelector(`[data-item-id="${element.dataset.itemId}"][data-field="text_confirmed"]`);
        if (checkbox) checkbox.checked = false;
      }
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
  $('#finalConfirmed').checked = false;
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
    if (hit && hit.item.id !== photoBackgroundId()) {
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
  if (photoBackgroundId()) { $('#emptyMaskOption').hidden = true; return; }
  const hasPixels = maskHasPixels();
  $('#emptyMaskOption').hidden = hasPixels;
  if (hasPixels) {
    $('#emptyMaskApproved').checked = false;
  }
}

function acceptsDefaultText(item, settings) {
  return !overlayTextReviewOpened && settings.text_content
    && settings.text_content === initialOverlayText[item.id];
}

function preflightErrors() {
  const errors = [];
  if (backgroundSelection?.mode === 'fixed' && !$('#backgroundBrief').value.trim()) {
    errors.push('切回固定底板时请填写清版说明并复核删除区域');
  }
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
    if (settings.requires_exact_content === true && !settings.prepared_asset
      && !(settings.text_content && (settings.text_confirmed || acceptsDefaultText(item, settings)))) {
      errors.push(`${item.label} 的高级配置要求精确素材，但没有提供素材`);
    }
  }
  if ($('#otherFeedback').value.trim() || questionAnswers().some(item => item.answer)) {
    errors.push('请先提交问答或其他反馈，让 VLM 纠正当前结果');
  }
  if (!$('#finalConfirmed').checked) errors.push('请手动确认当前识别结果没有问题');
  for (const item of draft.overlays) {
    const settings = reviewOptions.overlays[item.id];
    if ((item.text_content || settings.text_content)
      && !(settings.text_content && (settings.text_confirmed || acceptsDefaultText(item, settings)))) {
      errors.push(item.label + ' 的完整文字尚未确认');
    }
  }
  if (!photoBackgroundId() && !maskHasPixels() && !$('#emptyMaskApproved').checked) {
    errors.push('删除蒙版为空；请画出旧内容，或明确确认无需删除');
  }
  return errors;
}

async function load() {
  if (returnUrl) {
    $('#returnLink').hidden = false;
    $('#returnLink').href = returnUrl;
  }
  const response = await fetch(endpoint('/session'));
  if (!response.ok) throw new Error('无法读取当前审核版本');
  const snapshot = await response.json();
  $('#items').replaceChildren();
  overlayTextReviewOpened = false;
  overlayDetailsOpen = false;
  initialOverlayText = Object.fromEntries(Object.entries(snapshot.review_options.overlays)
    .map(([id, fields]) => [id, fields.text_content]));
  draft = snapshot.draft;
  reviewOptions = snapshot.review_options;
  backgroundSelection = null;
  fixedBackground = photoBackgroundId() ? null : structuredClone(draft.background);

  image = new Image();
  image.src = endpoint('/reference');
  await image.decode();
  view.width = mask.width = draft.canvas.width;
  view.height = mask.height = draft.canvas.height;

  const initial = new Image();
  initial.src = snapshot.fixed_mask_data_url || snapshot.mask_data_url;
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

  $('#backgroundBrief').value = draft.background.background_brief || '';
  $('#backgroundBrief').oninput = event => {
    if (!photoBackgroundId()) draft.background.background_brief = event.target.value;
  };
  $('#backgroundComposition').value = reviewOptions.background.composition_mode || 'protected';
  const updateBackgroundControls = () => {
    const fullCandidate = $('#backgroundComposition').value === 'full_candidate';
    $('#backgroundExpand').disabled = fullCandidate;
    $('#backgroundFeather').disabled = fullCandidate;
  };
  $('#backgroundComposition').onchange = updateBackgroundControls;
  updateBackgroundControls();
  $('#backgroundExpand').value = reviewOptions.background.expand_px;
  $('#backgroundFeather').value = reviewOptions.background.feather_px;
  selected = (draft.slots[0] || draft.overlays[0] || {}).id;
  renderItems();
  renderLayers();
  renderQuestions();
  renderBackgroundSource();
  await loadRecoveries();
  $('#autoSummary').textContent = '没有反馈时可直接勾选整体确认；未展开的装饰文字沿用识别结果。';
  render();
  $('#status').textContent = '请检查识别结果；接受当前结果可直接确认，有错误再填写反馈。';
}

$('#save').onclick = async () => {
  const errors = preflightErrors();
  if (errors.length) {
    $('#status').textContent = '尚不能保存：\n- ' + errors.join('\n- ');
    return;
  }
  const copy = structuredClone(draft);
  $('#status').textContent = '正在校验并保存…';
  const response = await fetch(endpoint('/save'), {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      ...(csrfToken ? {'X-Figcopy-Token': csrfToken} : {}),
    },
    body: JSON.stringify({
      draft: copy,
      mask_png: mask.toDataURL('image/png'),
      slot_overrides: reviewOptions.slots,
      overlay_overrides: reviewOptions.overlays,
      revision: reviewOptions.revision,
      final_confirmed: $('#finalConfirmed').checked,
      question_resolutions: questionAnswers(),
      other_feedback: $('#otherFeedback').value.trim(),
      overlay_text_review_opened: overlayTextReviewOpened,
      empty_mask_approved: $('#emptyMaskApproved').checked,
      background_composition_mode: $('#backgroundComposition').value,
      background_expand_px: Number($('#backgroundExpand').value),
      background_feather_px: Number($('#backgroundFeather').value),
      background_selection: backgroundSelection,
    }),
  });
  const result = await response.json();
  $('#status').textContent = response.ok
    ? `已保存：${result.path}`
    : `失败 [${result.code}]\n${result.message}\n${JSON.stringify(result.details || {}, null, 2)}`;
  if (response.ok && returnUrl) {
    window.setTimeout(() => window.location.assign(returnUrl), 450);
  }
};

function feedbackKey() { return 'figcopy-feedback:' + apiBase + ':' + reviewOptions.revision; }

function questionAnswers() {
  return draft.questions.map((question, index) => ({
    question, answer: $('#questions').querySelectorAll('textarea')[index].value.trim(),
  }));
}

function renderQuestions() {
  $('#questions').innerHTML = draft.questions.length
    ? draft.questions.map((question, index) => `<label class="stack">${index + 1}. ${escapeHtml(question)}
      <textarea maxlength="12000" aria-label="${escapeHtml(question)}" placeholder="选填；留空表示接受当前识别"></textarea></label>`).join('')
    : '<p class="auto">模型没有留下疑问，请继续核对画面；发现遗漏可填写其他说明。</p>';
  $('#otherFeedback').value = '';
  $('#finalConfirmed').checked = false;
  try {
    const saved = JSON.parse(sessionStorage.getItem(feedbackKey()) || 'null');
    if (saved) {
      $('#questions').querySelectorAll('textarea').forEach((node, index) => {
        node.value = saved.answers?.[index]?.answer || '';
      });
      $('#otherFeedback').value = saved.other || '';
    }
  } catch (_) { /* Storage may be disabled; server retains submitted feedback. */ }
  $('#revisionHint').textContent = '当前识别版本：' + reviewOptions.revision.slice(0, 12);
}

async function loadRecoveries() {
  const container = $('#savedCorrections');
  container.hidden = true;
  container.replaceChildren();
  if (!apiBase) return;
  try {
    const response = await fetch(endpoint('/recoveries'));
    if (!response.ok) return;
    const options = (await response.json()).recoveries || [];
    container.hidden = !options.length;
    for (const option of options.slice(0, 3)) {
      const button = document.createElement('button');
      button.textContent = option.background_slot_id
        ? '使用全屏照片作为背景，恢复已保存的改稿'
        : '恢复已保存的改稿（不调用模型）';
      button.onclick = async () => {
        busy(true);
        try {
          const response = await fetch(endpoint('/recover'), {
            method: 'POST',
            headers: {'content-type': 'application/json', ...(csrfToken ? {'X-Figcopy-Token': csrfToken} : {})},
            body: JSON.stringify({...option, revision: reviewOptions.revision}),
          });
          const result = await response.json();
          if (!response.ok) throw new Error('[' + result.code + '] ' + result.message);
          await awaitCorrection(result.task);
        } catch (error) {
          $('#status').textContent = '恢复失败：' + error.message;
        } finally { busy(false); }
      };
      container.append(button);
    }
  } catch (_) { /* Optional recovery list must not prevent reviewing the current Draft. */ }
}

function busy(value) {
  document.querySelectorAll('aside button, aside input, aside textarea, aside select').forEach(node => {
    node.disabled = value;
  });
  view.style.pointerEvents = value ? 'none' : '';
  if (!value && photoBackgroundId()) {
    document.querySelectorAll('[data-mode], [data-rect-kind]').forEach(node => {
      if (node.dataset.mode === photoBackgroundId() || node.dataset.itemId === photoBackgroundId()) node.disabled = true;
    });
  }
}

async function awaitCorrection(task) {
  const previousSelection = backgroundSelection;
  while (task && ['queued', 'running'].includes(task.state)) {
    $('#status').textContent = task.kind === 'recover_review' ? '正在恢复已保存的结果，无需调用模型…' : 'VLM 正在根据回答纠正识别，请稍候…';
    await new Promise(resolve => setTimeout(resolve, 1500));
    const response = await fetch(endpoint('/correction-status'));
    if (!response.ok) throw new Error('无法读取纠正进度，请刷新查看；不要重复提交');
    task = (await response.json()).task;
  }
  if (!task || task.state !== 'succeeded') {
    const issue = task?.error?.details?.issues?.[0];
    throw new Error((task?.error?.message || '纠正未完成；当前回答和原识别稿已保留') + (issue ? '：' + issue.path + ' ' + issue.message : ''));
  }
  try { sessionStorage.removeItem(feedbackKey()); } catch (_) {}
  await load();
  $('#status').textContent = '已生成纠正后的结果。请再次检查，确认无误后手动勾选确认。';
  if (previousSelection && (previousSelection.mode !== (photoBackgroundId() ? 'slot' : 'fixed')
    || (previousSelection.mode === 'slot' && previousSelection.slot_id !== photoBackgroundId()))) {
    $('#status').textContent += '\n注意：模型返回的背景来源与刚才的显式选择不同，请检查“背景来源”后再决定。';
  }
}

$('#revise').onclick = async () => {
  const answers = questionAnswers();
  const other = $('#otherFeedback').value.trim();
  if (!answers.some(item => item.answer) && !other) {
    $('#status').textContent = '没有填写反馈；接受当前结果可直接勾选整体确认并保存。'; return;
  }
  $('#finalConfirmed').checked = false;
  busy(true);
  try {
    const edited = structuredClone(draft);
    for (const item of edited.overlays) {
      item.text_content = reviewOptions.overlays[item.id].text_content;
    }
    const response = await fetch(endpoint('/revise'), {
      method: 'POST',
      headers: {'content-type': 'application/json', ...(csrfToken ? {'X-Figcopy-Token': csrfToken} : {})},
      body: JSON.stringify({revision: reviewOptions.revision, draft: edited,
        background_selection: backgroundSelection,
        question_resolutions: answers, other_feedback: other}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error('[' + result.code + '] ' + result.message);
    await awaitCorrection(result.task);
  } catch (error) {
    $('#status').textContent = '纠正失败：' + error.message;
    await loadRecoveries();
  } finally { busy(false); }
};

document.querySelector('aside').addEventListener('input', event => {
  if (event.target.id !== 'finalConfirmed') $('#finalConfirmed').checked = false;
  if (draft && (event.target.id === 'otherFeedback' || event.target.closest('#questions'))) {
    try { sessionStorage.setItem(feedbackKey(), JSON.stringify({answers: questionAnswers(), other: $('#otherFeedback').value})); } catch (_) {}
  }
});
view.addEventListener('pointerdown', () => { $('#finalConfirmed').checked = false; });

load().then(async () => {
  const response = await fetch(endpoint('/correction-status'));
  if (!response.ok) return;
  const task = (await response.json()).task;
  if (['revise_review', 'recover_review'].includes(task?.kind) && ['queued', 'running'].includes(task.state)) {
    busy(true);
    try { await awaitCorrection(task); } finally { busy(false); }
  }
}).catch(error => {
  $('#status').textContent = '载入失败：' + error;
});
