// Viewing a saved stage is independent of the running workflow.
const pipelineView = {key: null, signature: null, request: 0};
const PIPELINE_STATUS = {complete: '已完成', pending: '等待制作', running: '制作中',
  skipped: '失败，已跳过', missing: '缺少素材', interrupted: '执行中断', failed: '失败', blocked: '需要处理'};

function viewedStageFromUrl() {
  const value = new URLSearchParams(location.search).get('step');
  return /^[0-5]$/.test(value || '') ? Number(value) : null;
}

function stageAvailable(status, index) {
  const files = status.artifacts || {};
  return [files.reference?.exists || !status.pending, files.draft?.exists,
    files.reviewed?.exists, files.template_manifest?.exists,
    files.render?.exists || ['rendering', 'awaiting_approval'].includes(status.stage),
    status.stage === 'complete'][index];
}

function selectPipelineStage(index) {
  state.viewedStage = index;
  pipelineView.key = pipelineView.signature = null;
  pipelineView.request++;
  const url = new URL(location.href);
  if (index === null) url.searchParams.delete('step');
  else url.searchParams.set('step', index);
  history.pushState({}, '', url);
  renderProject(state.currentStatus);
}

function savedImage(url, label, className = '', emptyMessage = '尚无已保存图片') {
  return `<figure class="pipeline-image ${className}"><figcaption>${escapeHtml(label)}</figcaption>${url
    ? `<a href="${escapeHtml(url)}" data-enlarge="${escapeHtml(label)}" target="_blank" rel="noreferrer"><img src="${escapeHtml(url)}" alt="${escapeHtml(label)}" loading="lazy"></a>`
    : `<div class="image-empty">${escapeHtml(emptyMessage)}</div>`}</figure>`;
}

function resultComparison(status) {
  const stamp = encodeURIComponent(status.template_revision || status.updated_at || 'saved');
  const reference = status.artifacts?.reference;
  const result = status.artifacts?.render;
  return `<div class="result-comparison" aria-label="参考图与成图对比">${
    savedImage(reference?.exists ? reference.url : null, '最初参考图 · Reference')}${
    savedImage(result?.exists ? result.url + '?v=' + stamp : null, '当前成图 · Result')}</div>
    <p class="comparison-hint">点击任意图片放大查看；参考图始终使用项目最初上传的图片。</p>`;
}

function renderPipelineNavigation(status) {
  const toolbar = $('#pipelineToolbar');
  toolbar.hidden = state.viewedStage === null;
  document.querySelector('.content-grid').classList.toggle('pipeline-reading', state.viewedStage !== null);
  $('#projectSettings').hidden = state.viewedStage !== null;
  document.querySelector('.content-grid').classList.toggle('result-view', state.viewedStage === null && (shouldShowBuildProgress(status) || ['awaiting_approval', 'complete'].includes(status.stage) && !activeJob(status.task)));
  if (state.viewedStage !== null) {
    $('#overlayActions').hidden = true;
    $('#viewedStageLabel').textContent = '正在查看：' + STAGES[state.viewedStage].label;
  }
  $('#returnCurrentStep').onclick = () => selectPipelineStage(null);
}

function shouldShowBuildProgress(status) {
  return Boolean(activeJob(status?.task) && status?.artifacts?.reviewed?.exists && (
    ['reviewed', 'building'].includes(status.stage)
    || activeJob(status.task) && ['build', 'retry'].includes(status.task.kind) && !status.artifacts?.template_manifest?.exists));
}

function elapsedTime(startedAt) {
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(startedAt)) / 1000));
  if (!Number.isFinite(seconds)) return '';
  return seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
}

function waitingTimer(startedAt) {
  return startedAt ? `<span data-build-started="${escapeHtml(startedAt)}">已等待 ${elapsedTime(startedAt)}</span>` : '';
}

function updateBuildTimers() {
  document.querySelectorAll('[data-build-started]').forEach(node => {
    node.textContent = '已等待 ' + elapsedTime(node.dataset.buildStarted);
  });
}
window.setInterval(updateBuildTimers, 1000);

function buildProgressHtml(data, status) {
  const progress = data.progress;
  const current = progress.current;
  const phase = {preparing: '正在准备清版与素材', background: '正在清版', materials: '正在制作素材',
    assembling: '正在组装模板', complete: '素材制作已结束', failed: '制作中断，已完成的结果仍可查看'}[progress.phase];
  const location = current ? `正在制作第 ${current.index} / ${progress.total} 件：${current.label}`
    : progress.phase === 'background' ? '先完成背景清版，随后逐件制作下方素材。'
    : progress.phase === 'assembling' ? '素材已处理完，正在保存模板与检查预览。'
    : progress.phase === 'materials' ? '正在准备下一件素材。' : '';
  const waiting = progress.phase === 'background' ? data.background.started_at : progress.phase === 'preparing' ? status.task?.created_at : current?.started_at;
  return `<div class="build-summary" aria-live="polite"><div class="build-summary-heading"><h3>${escapeHtml(phase)}</h3>
    <span class="build-background-state">背景：${data.background.status === 'skipped' ? '使用客户照片，跳过清版' : escapeHtml(PIPELINE_STATUS[data.background.status] || data.background.status)}</span></div>
    <p class="build-counts">共 <strong>${progress.total}</strong> 件素材 · 已完成 <strong>${progress.completed}</strong> 件${progress.skipped ? ` · 失败/跳过 <strong>${progress.skipped}</strong> 件` : ''} · 剩余 ${progress.remaining} 件</p>
    ${progress.total ? `<progress aria-label="素材处理进度" max="${progress.total}" value="${progress.processed}">${progress.processed}/${progress.total}</progress>` : ''}
    <p class="build-current">${escapeHtml(location)} ${waitingTimer(waiting)}</p>
    <p class="comparison-hint">${progress.total !== progress.generated_total ? `其中 ${progress.generated_total} 件参考生成，${progress.total - progress.generated_total} 件本地制作或保留。` : ''}${['preparing', 'background', 'materials', 'assembling'].includes(progress.phase) ? '每完成一件，下方自动显示 crop 与结果对比。进度按实际完成件数计算。' : '可展开每件素材查看完整制作过程。'}</p></div>`;
}

function materialCardHtml(item, index) {
  const images = item.images;
  const variants = [['reference', '参考局部'], ['raw', '生成原图'], ['processed', '完整去底图'], ['final', '最终使用素材']];
  return `<div class="slot-heading"><strong>${index + 1}. ${escapeHtml(item.label)}</strong><span class="material-status ${escapeHtml(item.status)}">${escapeHtml(PIPELINE_STATUS[item.status] || item.status)}${item.cache_hit && item.status === 'complete' ? ' · 已复用' : ''}</span></div>
    <div class="material-pair">${savedImage(images.reference, '原素材 · Crop')}${savedImage(images.raw || images.final || images.processed,
      item.action === 'reference_generate' ? '生成结果' : '制作结果', 'checkerboard', item.status === 'running' ? '正在制作，完成后自动显示' : item.status === 'pending' ? '等待制作' : '本件没有可用结果')}</div>
    ${item.status === 'running' ? `<p class="material-wait">正在制作 ${waitingTimer(item.started_at)}</p>` : ''}
    ${item.text ? `<p>文字：${escapeHtml(item.text)}</p>` : ''}
    ${item.warnings.map(message => `<p class="material-warning">${escapeHtml(message)}</p>`).join('')}
    <details data-piece="${escapeHtml(item.id)}"><summary>查看完整制作过程</summary><div class="material-process">${variants.map(([variant, label]) => savedImage(images[variant], label, variant === 'processed' || variant === 'final' ? 'checkerboard' : '')).join('')}</div><p class="comparison-hint">仅展示已保存的图片；旧项目或本地制作的素材可能没有全部步骤。</p></details>`;
}

function renderBuildMaterials(panel, data, status, live) {
  if (!panel.querySelector('#buildMaterials')) {
    panel.innerHTML = actionHeader(live ? 'LIVE PIPELINE' : 'SAVED MATERIALS', '清版与素材制作',
      live ? '无需一直盯着日志。这里展示当前进度，每件素材完成后自动出现对比图。' : '查看制作进度与本项目当前采用的素材；生成期间自动更新。')
      + '<div id="buildMaterials"><div id="buildProgress"></div><p id="buildRefreshState" class="comparison-hint"></p><details class="build-background-details"><summary>背景清版 · 查看参考图与清版结果</summary><div id="buildBackground"></div></details><h3 id="buildMaterialTitle"></h3><div id="buildMaterialGrid" class="material-grid"></div><div id="buildLayout"></div></div>';
  }
  function update(id, signature, html) {
    const element = panel.querySelector(id);
    if (element.dataset.signature === signature) return;
    element.dataset.signature = signature;
    element.innerHTML = html;
  }
  update('#buildProgress', JSON.stringify([data.progress, data.background.status, data.background.started_at]), buildProgressHtml(data, status));
  const background = data.background;
  update('#buildBackground', JSON.stringify(background), background.mode === 'slot'
    ? `<p class="notice">背景由客户照片槽 ${escapeHtml(background.slot_id)} 提供，无需固定清版。</p>`
    : `<div class="background-pair">${savedImage(status.artifacts?.reference?.url, '最初参考图')}${savedImage(background.images.final || background.images.candidate, '清版结果', '', background.status === 'running' ? '正在清版，完成后自动显示' : '尚无清版结果')}</div>`);
  panel.querySelector('#buildMaterialTitle').textContent = `独立素材 · ${data.overlays.length} 件`;
  const grid = panel.querySelector('#buildMaterialGrid');
  const existing = new Map([...grid.querySelectorAll(':scope > [data-material-id]')].map(node => [node.dataset.materialId, node]));
  data.overlays.forEach((item, index) => {
    let card = existing.get(item.id);
    if (!card) {
      card = document.createElement('article'); card.className = 'material-card';
      card.dataset.materialId = item.id; grid.append(card);
    }
    existing.delete(item.id);
    const signature = JSON.stringify(item);
    if (card.dataset.signature !== signature) {
      const opened = card.querySelector('details')?.open;
      card.innerHTML = materialCardHtml(item, index);
      card.querySelector('details').open = Boolean(opened);
      card.dataset.signature = signature;
    }
  });
  existing.forEach(node => node.remove());
  if (!data.overlays.length) grid.textContent = '此项目没有需要制作的独立素材。';
  update('#buildLayout', JSON.stringify([data.template_available, status.template_revision]), data.template_available
    ? `<h3>模板布局</h3>${savedImage('/api/projects/' + encodeURIComponent(status.project_id) + '/layout/preview?v=' + encodeURIComponent(status.template_revision || ''), '模板布局预览（缺照片时显示编号占位）')}` : '');
  panel.querySelector('#buildRefreshState').textContent = '最近更新 ' + new Date().toLocaleTimeString() + ' · 每约 2.5 秒自动更新';
  updateBuildTimers();
}

async function renderPipelineView(status, live = false) {
  const index = live ? 2 : state.viewedStage;
  if (index === null) return;
  const projectId = status.project_id;
  const key = projectId + ':' + index + (live ? ':live' : ':saved');
  const viewMatches = () => state.currentId === projectId && (live ? state.viewedStage === null && shouldShowBuildProgress(state.currentStatus) : state.viewedStage === index);
  if (pipelineView.pending?.key === key && pipelineView.pending.request === pipelineView.request) return;
  const requestId = ++pipelineView.request;
  const pending = {key, request: requestId};
  pipelineView.pending = pending;
  const panel = $('#actionPanel');
  const first = pipelineView.key !== key;
  if (first) {
    pipelineView.key = key;
    pipelineView.signature = null;
    panel.innerHTML = actionHeader('PIPELINE', STAGES[index].label, '正在读取本项目已保存的结果…');
  }
  try {
    if (!stageAvailable(status, index)) {
      panel.innerHTML = actionHeader('PIPELINE', STAGES[index].label, '此节点尚未产生可查看的结果。');
      return;
    }
    if (index >= 4) {
      const signature = JSON.stringify([key, status.template_revision, status.updated_at]);
      if (signature === pipelineView.signature) return;
      pipelineView.signature = signature;
      panel.innerHTML = actionHeader('SAVED RESULT', index === 5 ? '发布结果对比' : '合成结果对比',
        index === 5 ? '对照最初参考图，查看已确认的成品。' : '查看当前保存的成图；确认状态以顶部项目进度为准。') + resultComparison(status);
      return;
    }
    const data = await api(`/api/projects/${encodeURIComponent(projectId)}/pipeline`);
    if (requestId !== pipelineView.request || !viewMatches()) return;
    const signature = index <= 1
      ? JSON.stringify([key, data.analysis_available, data.confirmed_available])
      : JSON.stringify([key, data, status.template_revision]);
    // In particular, do not reload the review iframe when another stage advances.
    if (signature === pipelineView.signature) {
      const refreshed = panel.querySelector('#buildRefreshState');
      if (refreshed) refreshed.textContent = '最近更新 ' + new Date().toLocaleTimeString() + ' · 每约 2.5 秒自动更新';
      return;
    }
    pipelineView.signature = signature;
    const opened = [...panel.querySelectorAll('details[open][data-piece]')].map(node => node.dataset.piece);
    if (index <= 1) {
      const confirmed = index === 1 && data.confirmed_available;
      const available = index === 0 ? data.analysis_available : confirmed || !status.artifacts.reviewed?.exists && data.analysis_available;
      const source = confirmed ? 'confirmed' : 'analysis';
      const title = index === 0 ? '识别结果与原图结构' : confirmed ? '已确认的 Draft' : 'Draft 识别结果';
      const lead = index === 0 ? '展示最近保存的识别结果，保留框选图与 photo/decor 结构对比。'
        : confirmed ? '读取确认时保存的内容，框选与结构预览沿用原确认页。'
        : '尚未确认时可查看识别结果；旧项目没有保存确认页内容时会明确提示。';
      const url = `/projects/${encodeURIComponent(projectId)}/review?view=${source}`;
      panel.innerHTML = actionHeader('SAVED REVIEW', title, lead)
        + (available ? `<div class="action-row"><a class="button quiet" href="${url}" target="_blank" rel="noreferrer">全屏查看框选与结构</a></div><iframe class="review-snapshot" title="${title}：框选图与 photo/decor 对比" src="${url}"></iframe>`
          : '<p class="notice">此项目没有留存确认页的框选结果。可在项目文件中查看已保存的确认规格。</p>')
        + (status.stage === 'awaiting_review' ? `<a class="button primary" href="/projects/${encodeURIComponent(projectId)}/review">继续确认 Draft</a>` : '');
      return;
    }
    if (index === 2) {
      renderBuildMaterials(panel, data, status, live);
    } else {
      panel.innerHTML = actionHeader('SAVED INPUTS', '客户素材与槽位', '查看本项目当前保存的上传内容和槽位对应关系。')
        + `<div class="material-grid">${data.slots.map(item => `<article class="material-card"><h3>${escapeHtml(item.label)}</h3>${item.type === 'image'
          ? savedImage(item.images.input, item.has_image ? '已上传照片' : '尚未上传照片')
          : `<p>${escapeHtml(item.text || '尚无文字')}</p>`}</article>`).join('') || '<p>此模板无需上传客户素材。</p>'}</div>`;
    }
    panel.querySelectorAll('details[data-piece]').forEach(node => { node.open = opened.includes(node.dataset.piece); });
  } catch (error) {
    if (requestId !== pipelineView.request || !viewMatches()) return;
    // Keep already visible results if a background refresh temporarily fails.
    if (first) panel.innerHTML = actionHeader('PIPELINE', '暂时无法读取节点结果', describeError(error));
    else if (panel.querySelector('#buildRefreshState')) panel.querySelector('#buildRefreshState').textContent = '进度更新暂时失败，正在重试；已显示的结果保留。';
    pipelineView.signature = null;
  } finally {
    if (pipelineView.pending === pending) pipelineView.pending = null;
  }
}

document.addEventListener('click', event => {
  const link = event.target.closest('a[data-enlarge]');
  if (!link) return;
  event.preventDefault();
  const dialog = document.querySelector('#imageDialog');
  document.querySelector('#imageDialogTitle').textContent = link.dataset.enlarge;
  const image = document.querySelector('#enlargedImage');
  image.src = link.href; image.alt = link.dataset.enlarge;
  document.querySelector('#imageOriginal').href = link.href;
  dialog.showModal();
});
