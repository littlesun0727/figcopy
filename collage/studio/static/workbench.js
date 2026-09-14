const $ = selector => document.querySelector(selector);
const csrfToken = $('meta[name="figcopy-csrf-token"]').content;
const state = {
  projects: [],
  currentId: null,
  currentStatus: null,
  renderSignature: null,
  providerStatus: null,
  providerFormDirty: false,
  followTaskId: null,
  returnToCreateAfterProvider: false,
};

const STAGES = [
  {label: '分析参考图', values: ['analyzing']},
  {label: '确认 Draft', values: ['awaiting_review']},
  {label: '制作模板', values: ['reviewed', 'building']},
  {label: '客户素材', values: ['awaiting_bindings']},
  {label: '检查预览', values: ['rendering', 'awaiting_approval']},
  {label: '发布完成', values: ['complete']},
];

const STAGE_LABELS = {
  analyzing: '正在分析',
  awaiting_review: '等待 Draft 确认',
  reviewed: 'Draft 已确认',
  building: '正在制作模板',
  awaiting_bindings: '等待客户素材',
  rendering: '正在渲染',
  awaiting_approval: '等待最终批准',
  complete: '已发布',
  blocked: '流程被阻塞',
  failed: '执行失败',
  not_initialized: '旧版项目',
  invalid: '项目异常',
};

const ARTIFACT_LABELS = {
  reference: '参考图',
  draft: 'Draft JSON',
  draft_preview: 'Draft 框选预览',
  reviewed: '确认规格',
  remove_mask: '删除蒙版',
  template_manifest: '模板清单',
  inspection_report: '模板检查报告',
  upload_guide: '客户上传指南',
  bindings_example: 'Bindings 示例',
  bindings: '客户 Bindings',
  render: '最终预览图',
  render_audit: '渲染审计',
};

const JOB_LABELS = {
  create: '正在导入并分析参考图',
  build: '正在根据确认结果制作模板',
  render: '正在导入客户素材并生成预览',
  retry: '正在继续工作流',
  regenerate_overlay: '正在重做一件装饰并保存新版本',
  approve: '正在记录批准并发布模板',
};

function escapeHtml(value) {
  return String(value ?? '').replace(
    /[&<>"']/g,
    character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]),
  );
}

class ApiError extends Error {
  constructor(payload, status) {
    super(payload?.message || `请求失败 (${status})`);
    this.code = payload?.code || 'REQUEST_FAILED';
    this.details = payload?.details || {};
    this.status = status;
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined && !(options.body instanceof FormData)) {
    headers.set('content-type', 'application/json');
    options.body = JSON.stringify(options.body);
  }
  if ((options.method || 'GET').toUpperCase() !== 'GET') {
    headers.set('X-Figcopy-Token', csrfToken);
  }
  const response = await fetch(path, {...options, headers});
  let payload = null;
  try {
    payload = await response.json();
  } catch (_error) {
    payload = {message: `服务返回了无法读取的内容 (${response.status})`};
  }
  if (!response.ok) {
    throw new ApiError(payload, response.status);
  }
  return payload;
}

function describeError(error) {
  const details = error?.details || {};
  const issue = Array.isArray(details.issues) ? details.issues[0] : null;
  const suffix = issue ? `\n${issue.path}: ${issue.message}` : '';
  return `[${error?.code || 'ERROR'}] ${error?.message || error}${suffix}`;
}

let toastTimer = null;
function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { element.hidden = true; }, 3200);
}

function projectIdFromPath() {
  const match = window.location.pathname.match(/^\/projects\/([^/]+)\/?$/);
  return match ? decodeURIComponent(match[1]) : null;
}

function navigate(projectId, {replace = false} = {}) {
  state.currentId = projectId;
  state.renderSignature = null;
  const target = projectId ? `/projects/${encodeURIComponent(projectId)}` : '/';
  window.history[replace ? 'replaceState' : 'pushState']({}, '', target);
  renderProjectList();
  refreshCurrent();
}

function activeJob(task) {
  return task && ['queued', 'running'].includes(task.state);
}

function projectDot(project) {
  if (activeJob(project.task)) return 'active';
  if (['blocked', 'failed', 'invalid'].includes(project.stage)) return 'error';
  if (['awaiting_review', 'awaiting_bindings', 'awaiting_approval'].includes(project.stage)) return 'waiting';
  return '';
}

function renderProjectList() {
  const query = $('#projectSearch').value.trim().toLowerCase();
  const projects = state.projects.filter(project =>
    `${project.name} ${project.project_id}`.toLowerCase().includes(query),
  );
  $('#projectCount').textContent = state.projects.length;
  $('#projectList').innerHTML = projects.length
    ? projects.map(project => `
      <button class="project-item ${project.project_id === state.currentId ? 'active' : ''}"
        type="button" data-project-id="${escapeHtml(project.project_id)}">
        <strong>${escapeHtml(project.name)}</strong>
        <i class="project-dot ${projectDot(project)}"></i>
        <small>${escapeHtml(STAGE_LABELS[project.stage] || project.stage)}</small>
      </button>`).join('')
    : '<p class="preview-placeholder">还没有项目</p>';
  $('#projectList').querySelectorAll('[data-project-id]').forEach(button => {
    button.onclick = () => navigate(button.dataset.projectId);
  });
}

async function loadProjects() {
  const payload = await api('/api/projects');
  state.projects = payload.projects;
  renderProjectList();
}

function inferredStage(status) {
  if (!['blocked', 'failed', 'invalid'].includes(status.stage)) return status.stage;
  const previous = [...(status.history || [])].reverse().find(entry =>
    !['blocked', 'failed'].includes(entry.stage),
  );
  return previous?.stage || 'analyzing';
}

function renderSteps(status) {
  const stage = inferredStage(status);
  let currentIndex = STAGES.findIndex(item => item.values.includes(stage));
  if (currentIndex < 0) currentIndex = 0;
  $('#stageSteps').innerHTML = STAGES.map((step, index) => {
    const stateClass = index < currentIndex ? 'done' : index === currentIndex ? 'current' : '';
    return `<li class="stage-step ${stateClass}">${escapeHtml(step.label)}</li>`;
  }).join('');
}

function renderArtifacts(status) {
  const entries = Object.entries(status.artifacts || {}).filter(([_name, item]) => item.exists);
  $('#artifactList').innerHTML = entries.length
    ? entries.map(([name, item]) => `
      <a class="artifact available" href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">
        <i></i><span>${escapeHtml(ARTIFACT_LABELS[name] || name)}</span><small>打开 ↗</small>
      </a>`).join('')
    : '<p class="preview-placeholder">项目文件生成后会出现在这里</p>';
  $('#historyList').innerHTML = (status.history || []).length
    ? [...status.history].reverse().map(entry => `
      <li>${escapeHtml(STAGE_LABELS[entry.stage] || entry.stage)}${entry.code ? ` · ${escapeHtml(entry.code)}` : ''}</li>
    `).join('')
    : '<li>暂无记录</li>';
}

function actionHeader(kicker, title, lead) {
  return `<p class="action-kicker">${escapeHtml(kicker)}</p><h2>${escapeHtml(title)}</h2><p class="lead">${escapeHtml(lead)}</p>`;
}

function preview(url, alt) {
  if (!url) return '<div class="preview-frame"><p class="preview-placeholder">预览尚未生成</p></div>';
  const separator = url.includes('?') ? '&' : '?';
  return `<div class="preview-frame"><img src="${escapeHtml(url)}${separator}v=${Date.now()}" alt="${escapeHtml(alt)}"></div>`;
}

function renderRunning(status) {
  const label = JOB_LABELS[status.task.kind] || '正在处理项目';
  $('#actionPanel').innerHTML = `
    ${actionHeader('PROCESSING', label, '可以离开这个页面，所有阶段都会写入项目记录；回来后会从当前阶段继续。')}
    ${status.artifacts?.render?.exists ? preview(status.artifacts.render.url, '当前结果预览') : `
      <div class="preview-frame"><div class="preview-placeholder">后台任务运行中<br><small>页面会自动更新</small></div></div>`}
  `;
}

function bindContinueButton(projectId) {
  const button = $('#continueButton');
  if (!button) return;
  button.onclick = async () => {
    button.disabled = true;
    try {
      await api(`/api/projects/${encodeURIComponent(projectId)}/retry`, {method: 'POST', body: {}});
      toast('已继续执行');
      await refreshCurrent(true);
    } catch (error) {
      toast(describeError(error));
      button.disabled = false;
    }
  };
}

function renderReview(status) {
  const image = status.artifacts.draft_preview?.exists
    ? status.artifacts.draft_preview.url
    : status.artifacts.reference?.url;
  $('#actionPanel').innerHTML = `
    ${actionHeader('HUMAN GATE 1 / 2', '确认识别区域与清除范围', '回答识别疑问并提交纠正，检查新结果后手动确认，再继续制作模板。')}
    ${preview(image, 'Draft 框选预览')}
    <div class="action-row">
      <a class="button primary large" href="/projects/${encodeURIComponent(status.project_id)}/review">打开 Draft 审核页</a>
      ${status.artifacts.draft?.exists ? `<a class="button quiet" href="${escapeHtml(status.artifacts.draft.url)}" target="_blank" rel="noreferrer">查看 JSON</a>` : ''}
    </div>`;
}

async function loadBindingForm(status) {
  const projectId = status.project_id;
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(projectId)}/slots`);
    if (state.currentId !== projectId || !['awaiting_bindings', 'awaiting_approval'].includes(state.currentStatus?.stage)) return;
    const cards = payload.slots.map(slot => {
      if (slot.type === 'text') {
        return `<article class="slot-card" data-slot-id="${escapeHtml(slot.id)}" data-slot-type="text">
          <div class="slot-heading"><strong>${escapeHtml(slot.label)}</strong><code>${escapeHtml(slot.id)}</code></div>
          <p>${escapeHtml(slot.upload_hint || '输入替换文字')}</p>
          <div class="slot-controls"><label class="wide">文字<input data-field="text" value="${escapeHtml(slot.text || '')}" ${slot.required && slot.default_text == null ? 'required' : ''}></label></div>
        </article>`;
      }
      const mode = {photo: '整张照片', photo_feather: '柔和融合照片', cutout: '透明主体'}[slot.mode] || slot.mode;
      return `<article class="slot-card" data-slot-id="${escapeHtml(slot.id)}" data-slot-type="image">
        <div class="slot-heading"><strong>${escapeHtml(slot.label)}</strong><code>${escapeHtml(slot.id)}</code></div>
        <p>${escapeHtml(slot.upload_hint || '选择客户图片')} · ${escapeHtml(mode)}${slot.required ? ' · 必填' : ''}</p>
        <div class="slot-controls">
          <label class="wide">客户图片<input data-field="image" type="file" accept="image/*" ${slot.required && !slot.has_image ? 'required' : ''}>${slot.has_image ? '<small>不重新选择则沿用当前图片</small>' : ''}</label>
          <label>缩放<input data-field="scale" type="number" min="0.05" max="20" step="0.01" value="${escapeHtml(slot.scale ?? 1)}"></label>
          <label>X 偏移<input data-field="offset-x" type="number" step="1" value="${escapeHtml(slot.offset_px?.[0] ?? 0)}"></label>
          <label>Y 偏移<input data-field="offset-y" type="number" step="1" value="${escapeHtml(slot.offset_px?.[1] ?? 0)}"></label>
        </div>
        ${slot.mode === 'cutout' ? `<details><summary>已有主体 Alpha 蒙版（可选）</summary><label>Alpha 图片<input data-field="alpha" type="file" accept="image/*"></label></details>` : ''}
      </article>`;
    }).join('');
    $('#actionPanel').innerHTML = `
      ${actionHeader('CUSTOMER INPUT', '放入这次要合成的素材', '每个槽位都按名称对应，不需要手改 Bindings JSON。缩放和偏移只影响当前预览。')}
      <p>布局预览：未上传照片的位置使用编号占位，上传后再生成实际结果。</p>
      ${preview(`/api/projects/${encodeURIComponent(projectId)}/layout/preview`, '候选布局预览')}
      <p><a class="button quiet" href="/projects/${encodeURIComponent(projectId)}/layers">调整照片与装饰布局</a></p>
      <form id="bindingsForm">
        <div class="slot-list">${cards}</div>
        <details class="advanced-settings"><summary>抠图高级设置</summary>
          <label>抠图 Provider<input id="bindingCutoutProvider" placeholder="module:object（留空沿用项目设置）"></label>
          <label class="check"><input id="bindingCloud" type="checkbox"> 明确允许云端抠图上传</label>
        </details>
        <div class="action-row"><button id="bindingSubmit" class="button primary large" type="submit">生成预览</button></div>
      </form>`;
    $('#bindingsForm').onsubmit = event => submitBindings(event, projectId);
  } catch (error) {
    $('#actionPanel').innerHTML = `${actionHeader('CUSTOMER INPUT', '无法读取素材槽位', describeError(error))}<button id="continueButton" class="button" type="button">重试</button>`;
    bindContinueButton(projectId);
  }
}

async function submitBindings(event, projectId) {
  event.preventDefault();
  const button = $('#bindingSubmit');
  button.disabled = true;
  const configuration = {slots: {}};
  const form = new FormData();
  document.querySelectorAll('.slot-card').forEach(card => {
    const slotId = card.dataset.slotId;
    if (card.dataset.slotType === 'text') {
      configuration.slots[slotId] = {text: card.querySelector('[data-field="text"]').value};
      return;
    }
    configuration.slots[slotId] = {
      scale: Number(card.querySelector('[data-field="scale"]').value),
      offset_px: [
        Number(card.querySelector('[data-field="offset-x"]').value),
        Number(card.querySelector('[data-field="offset-y"]').value),
      ],
    };
    const image = card.querySelector('[data-field="image"]').files[0];
    const alpha = card.querySelector('[data-field="alpha"]')?.files[0];
    if (image) form.append(`image.${slotId}`, image);
    if (alpha) form.append(`alpha.${slotId}`, alpha);
  });
  form.append('configuration', JSON.stringify(configuration));
  const provider = $('#bindingCutoutProvider').value.trim();
  if (provider) form.append('cutout_provider', provider);
  if ($('#bindingCloud').checked) form.append('allow_cloud_upload', 'true');
  try {
    await api(`/api/projects/${encodeURIComponent(projectId)}/bindings`, {method: 'POST', body: form});
    toast('素材已接收，正在生成预览');
    await refreshCurrent(true);
  } catch (error) {
    toast(describeError(error));
    button.disabled = false;
  }
}

function renderApproval(status, complete = false) {
  const result = status.artifacts.render;
  $('#actionPanel').innerHTML = `
    ${actionHeader(complete ? 'PUBLISHED' : 'HUMAN GATE 2 / 2', complete ? '模板已发布' : '检查最终合成效果', complete ? '本次模板已经通过人工验收，可以继续复用本地 Renderer。' : '重点检查旧素材残留、边缘接缝、层序、文字和主体遮挡。批准操作会写入审核记录。')}
    ${preview(result?.url, complete ? '已发布模板预览' : '待批准结果预览')}
    <p><a class="button quiet" href="/projects/${encodeURIComponent(status.project_id)}/layers">调整照片与装饰布局 · 保存新版本</a></p>
    ${complete ? `
      <div class="action-row"><a class="button primary" href="${escapeHtml(result?.url)}?download=1">下载结果 PNG</a></div>
    ` : `
      <form id="approvalForm" class="approval-form">
        <label>验收备注<textarea id="approvalNotes" rows="3" placeholder="例如：已检查接缝、层序和文字"></textarea></label>
        <label class="check"><input id="allowFixture" type="checkbox"> 我知道该模板使用测试 fixture，仍要作为演示发布</label>
        <div class="action-row">
          <button id="approveButton" class="button primary large" type="submit">确认通过并发布</button>
          <button id="editBindings" class="button quiet" type="button">更换素材或文字</button>
          <a class="button quiet" href="${escapeHtml(result?.url)}?download=1">下载原图检查</a>
        </div>
      </form>`}
  `;
  if (!complete) {
    $('#editBindings').onclick = () => loadBindingForm(status);
    $('#approvalForm').onsubmit = async event => {
      event.preventDefault();
      const button = $('#approveButton');
      button.disabled = true;
      try {
        await api(`/api/projects/${encodeURIComponent(status.project_id)}/approve`, {
          method: 'POST',
          body: {notes: $('#approvalNotes').value, allow_fixture: $('#allowFixture').checked},
        });
        toast('已提交最终批准');
        await refreshCurrent(true);
      } catch (error) {
        toast(describeError(error));
        button.disabled = false;
      }
    };
  }
}

function renderRetry(status) {
  const error = status.last_error || status.task?.error;
  $('#actionPanel').innerHTML = `
    ${actionHeader('RECOVERY', status.stage === 'blocked' ? '流程需要补充配置' : '这一步没有完成', error?.message || status.next_action)}
    <div class='action-row provider-recovery'><button id='openProviderFromRetry' class='button primary' type='button'>配置 Key 与 Provider</button></div>
    <div class="notice warning"><strong>${escapeHtml(error?.code || 'WORKFLOW_PAUSED')}</strong><br>${escapeHtml(error?.message || status.next_action)}</div>
    <form id="retryForm" class="retry-form">
      <label>识别服务<select id="retryVision">${serviceOptions('vision', status.providers?.vision_provider, true)}</select></label>
      <label>图片服务<select id="retryImage">${serviceOptions('image', status.providers?.image_provider, true)}</select></label>
      <label>抠图 Provider<input id="retryCutout" placeholder="module:object（留空沿用原设置）"></label>
      <label class="check"><input id="retryFixture" type="checkbox"> 改用离线 fixture 图片 Provider（仅测试）</label>
      <label class="check"><input id="retryCloud" type="checkbox"> 明确允许云端上传</label>
      <div><button id="retryButton" class="button primary" type="submit">按当前配置重试</button></div>
    </form>`;
  $('#openProviderFromRetry').onclick = openProviderDialog;
  $('#retryForm').onsubmit = async event => {
    event.preventDefault();
    const button = $('#retryButton');
    button.disabled = true;
    const payload = {allow_cloud_upload: $('#retryCloud').checked};
    const vision = $('#retryVision').value.trim();
    const image = $('#retryImage').value.trim();
    const cutout = $('#retryCutout').value.trim();
    if (vision) payload.vision_provider = vision;
    if (image) payload.image_provider = image;
    if (cutout) payload.cutout_provider = cutout;
    if ($('#retryFixture').checked) payload.fixture_provider = true;
    try {
      await api(`/api/projects/${encodeURIComponent(status.project_id)}/retry`, {method: 'POST', body: payload});
      toast('已开始重试');
      await refreshCurrent(true);
    } catch (caught) {
      toast(describeError(caught));
      button.disabled = false;
    }
  };
}

function renderPausedMachineStage(status) {
  const titles = {
    analyzing: '继续分析参考图',
    reviewed: '继续制作模板',
    building: '继续制作模板',
    rendering: '继续生成预览',
  };
  $('#actionPanel').innerHTML = `
    ${actionHeader('RESUMABLE', titles[status.stage] || '继续工作流', '上一次进程可能在这个阶段停止。项目状态已经保存，可以从这里安全继续。')}
    <div class="preview-frame"><div class="preview-placeholder">准备从已保存的阶段继续</div></div>
    <button id="continueButton" class="button primary" type="button">继续执行</button>`;
  bindContinueButton(status.project_id);
}

function renderLegacy(status) {
  $('#actionPanel').innerHTML = `
    ${actionHeader('LEGACY PROJECT', '这个项目没有统一工作流记录', status.next_action)}
    <div class="notice">旧项目文件仍保留，可以用命令行单步工具读取；新建项目会自动使用端到端工作流。</div>`;
}

function renderCreateFailure(status) {
  const error = status.task?.error;
  $('#actionPanel').innerHTML = `
    ${actionHeader('CREATE FAILED', '项目还没有创建成功', error?.message || '请检查上传文件后重新创建。')}
    <div class="notice warning"><strong>${escapeHtml(error?.code || 'CREATE_FAILED')}</strong><br>${escapeHtml(error?.message || '上传内容无法导入')}</div>
    <button id="retryCreate" class="button primary" type="button">重新新建项目</button>`;
  $('#retryCreate').onclick = openCreateDialog;
}

function renderOverlayActions(status) {
  const warnings = status.warnings || [];
  const overlays = status.overlays || [];
  const busy = activeJob(status.task);
  $('#overlayActions').hidden = !warnings.length && !overlays.length;
  $('#overlayActions').innerHTML = `
    ${warnings.length ? `<p><strong>候选提示</strong></p><ul>${warnings.map(item =>
      `<li>${escapeHtml(item.label)}：${escapeHtml(item.message)}</li>`).join('')}</ul>` : ''}
    ${overlays.length ? `<div class="action-row">
      <label>重做一件装饰 <select id="overlayChoice" ${busy ? 'disabled' : ''}>${overlays.map(item =>
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}</option>`).join('')}</select></label>
      <label>这次使用的图片服务<select id="regenerateImage" ${busy ? 'disabled' : ''}>${serviceOptions('image', status.providers?.image_provider, true)}</select></label>
      <button id="regenerateOverlay" class="button quiet" ${busy ? 'disabled' : ''}>生成一次并保存新版本</button>
    </div>` : ''}`;
  if (!overlays.length) return;
  $('#regenerateOverlay').onclick = async () => {
    const button = $('#regenerateOverlay');
    button.disabled = true;
    try {
      const response = await api(`/api/projects/${encodeURIComponent(status.project_id)}/overlays/regenerate`, {
        method: 'POST',
        body: {overlay_id: $('#overlayChoice').value, revision: status.template_revision, image_provider: $('#regenerateImage').value},
      });
      state.followTaskId = response.task.id;
      toast('正在重做选中的装饰，其余素材沿用');
      await refreshCurrent(true);
    } catch (error) {
      toast(describeError(error));
      button.disabled = false;
    }
  };
}

function renderProject(status) {
  state.currentStatus = status;
  $('#welcomeView').hidden = true;
  $('#projectView').hidden = false;
  $('#projectId').textContent = status.project_id;
  $('#projectName').textContent = status.name;
  $('#stageBadge').textContent = STAGE_LABELS[status.stage] || status.stage;
  renderSteps(status);
  renderArtifacts(status);
  renderOverlayActions(status);
  renderProjectProviders(status);
  const backgroundRevision = $('#backgroundRevision');
  backgroundRevision.hidden = !status.artifacts?.reviewed?.exists;
  $('#backgroundRevisionHint').hidden = backgroundRevision.hidden;
  backgroundRevision.disabled = activeJob(status.task);
  backgroundRevision.onclick = async () => {
    backgroundRevision.disabled = true;
    try {
      const url = `/api/projects/${encodeURIComponent(status.project_id)}/background-revision`;
      const source = await api(url);
      const result = await api(url, {method: 'POST', body: {revision: source.revision}});
      window.location.assign(result.url);
    } catch (error) {
      toast(describeError(error));
      backgroundRevision.disabled = false;
    }
  };

  const task = status.task;
  $('#taskBanner').hidden = !activeJob(task);
  $('#taskBanner').textContent = activeJob(task) ? (JOB_LABELS[task.kind] || '后台任务执行中') : '';
  const error = status.last_error || (task?.state === 'failed' ? task.error : null);
  $('#errorBanner').hidden = !error;
  $('#errorBanner').textContent = error ? describeError(error) : '';

  if (activeJob(task)) {
    renderRunning(status);
  } else if (status.pending && task?.state === 'failed') {
    renderCreateFailure(status);
  } else if (['blocked', 'failed', 'invalid'].includes(status.stage)) {
    renderRetry(status);
  } else if (status.stage === 'awaiting_review') {
    renderReview(status);
  } else if (status.stage === 'awaiting_bindings') {
    $('#actionPanel').innerHTML = `${actionHeader('CUSTOMER INPUT', '正在读取素材槽位', '模板已经准备好。')}`;
    loadBindingForm(status);
  } else if (status.stage === 'awaiting_approval') {
    renderApproval(status, false);
  } else if (status.stage === 'complete') {
    renderApproval(status, true);
  } else if (status.stage === 'not_initialized') {
    renderLegacy(status);
  } else {
    renderPausedMachineStage(status);
  }
}

function renderPending(projectId, task) {
  const status = {
    project_id: projectId,
    name: projectId,
    status: 'new',
    stage: 'analyzing',
    next_action: '等待项目初始化',
    wait: null,
    last_error: task?.error || null,
    artifacts: {},
    task,
    history: [],
    pending: true,
  };
  renderProject(status);
}

async function refreshCurrent(force = false) {
  if (!state.currentId) {
    $('#welcomeView').hidden = false;
    $('#projectView').hidden = true;
    return;
  }
  try {
    const status = await api(`/api/projects/${encodeURIComponent(state.currentId)}`);
    if (status.task?.id === state.followTaskId && status.task.state === 'succeeded' && status.task.result?.url) {
      state.followTaskId = null;
      window.location.assign(status.task.result.url);
      return;
    }
    const signature = JSON.stringify([
      status.stage,
      status.updated_at,
      status.task?.state,
      status.task?.updated_at,
      status.last_error,
    ]);
    if (force || signature !== state.renderSignature) {
      state.renderSignature = signature;
      renderProject(status);
    }
  } catch (error) {
    if (error.code === 'PROJECT_NOT_FOUND') {
      const payload = await api(`/api/tasks/${encodeURIComponent(state.currentId)}`);
      if (payload.task) {
        renderPending(state.currentId, payload.task);
        return;
      }
    }
    $('#welcomeView').hidden = true;
    $('#projectView').hidden = false;
    $('#projectName').textContent = state.currentId;
    $('#actionPanel').innerHTML = `${actionHeader('ERROR', '无法打开项目', describeError(error))}`;
  }
}

async function refreshAll() {
  try {
    await loadProjects();
    await refreshCurrent();
    $('#connectionState').classList.remove('offline');
    $('#connectionState').innerHTML = '<i></i> 本机运行';
  } catch (_error) {
    $('#connectionState').classList.add('offline');
    $('#connectionState').innerHTML = '<i></i> 连接中断';
  }
}

function makeProviderCard(config, tone, label, detail) {
  const card = document.createElement('article');
  card.className = 'provider-card';
  const head = document.createElement('div');
  head.className = 'provider-card-head';
  const title = document.createElement('strong');
  title.textContent = config.name;
  const badge = document.createElement('span');
  badge.className = 'provider-state ' + tone;
  badge.textContent = label;
  head.append(title, badge);
  const model = document.createElement('p');
  model.textContent = config.model;
  const provider = document.createElement('small');
  provider.textContent = config.provider;
  const note = document.createElement('small');
  note.textContent = detail;
  card.append(head, model, provider, note);
  return card;
}

function serviceOptions(kind, selected, inherit = false) {
  const choices = {...(state.providerStatus?.choices?.[kind] || {})};
  if (selected && !choices[selected]) choices[selected] = selected;
  const items = Object.entries(choices).map(([value, label]) =>
    '<option value="' + escapeHtml(value) + '"' + (value === selected ? ' selected' : '') + '>' + escapeHtml(label) + '</option>');
  if (inherit) items.unshift('<option value="">沿用项目设置</option>');
  return items.join('');
}

function serviceDetails(config) {
  const connection = config.connection || {};
  if (!config.configured) return ['error', '未配置', connection.message || '请填写配置'];
  if (['offline', 'invalid'].includes(connection.state)) return ['error', '连接异常', connection.message];
  return [connection.state === 'online' ? 'ready' : 'warning',
    connection.state === 'online' ? '在线' : '已配置', connection.message || '待实际请求验证'];
}

function renderProviderStatus(status) {
  state.providerStatus = status;
  const cutout = status.cutout;
  const cutoutReady = cutout.configured && cutout.model_cached;
  $('#providerCards').replaceChildren(
    makeProviderCard(status.vision, ...serviceDetails(status.vision)),
    makeProviderCard(status.image, ...serviceDetails(status.image)),
    makeProviderCard(cutout, cutoutReady ? 'ready' : 'warning',
      cutoutReady ? '本地就绪' : '按需配置',
      cutoutReady ? '客户图片不会上传' : '抠图需要本地依赖和模型权重'),
  );
  const configured = status.vision.configured && status.image.configured;
  $('#providerDot').className = 'provider-dot ' + (configured ? 'ready' : 'warning');
  $('#providerLabel').textContent = configured ? 'Provider 已配置' : 'Provider 设置';
  $('#credentialHint').textContent = '当前：' + status.credential.source + '。新 Key 留空会沿用。';
  const usesYibu = [status.vision.provider, status.image.provider].some(value => value?.includes('.yibu:'));
  const audit = $('#auditStatus');
  audit.hidden = !usesYibu;
  audit.className = 'audit-status ' + (status.audit.state === 'online' ? 'ready' : 'warning');
  audit.textContent = 'Yibu 审计代理 · ' + status.audit.url + ' · ' + status.audit.message;
}

function populateProviderForm(status) {
  const services = status.services;
  const vision = services['collage.providers.yibu:YibuVisionProvider'];
  const image = services['collage.providers.yibu:YibuImageProvider'];
  const intranet = services['collage.providers.intranet:IntranetVisionProvider'];
  const qwen = services['collage.providers.qwen:QwenImageProvider'];
  $('#defaultVision').innerHTML = serviceOptions('vision', status.selection.vision);
  $('#defaultImage').innerHTML = serviceOptions('image', status.selection.image);
  $('#auditBaseUrl').value = status.audit.url || 'http://127.0.0.1:17860';
  $('#vlmModel').value = vision.model || '';
  $('#vlmMaxTokens').value = vision.max_tokens || '';
  $('#vlmReasoning').value = vision.reasoning_effort || '';
  $('#timeoutSeconds').value = vision.timeout_seconds || '';
  $('#imageModel').value = image.model || '';
  $('#imageSize').value = image.image_size || '2K';
  $('#intranetBaseUrl').value = intranet.base_url || '';
  $('#intranetModel').value = intranet.model || 'Qwen/Qwen3.8-Flash-Next';
  $('#intranetMaxTokens').value = intranet.max_tokens || '16384';
  $('#intranetTimeout').value = intranet.timeout_seconds || '900';
  $('#intranetCredentialHint').textContent = '当前：' + (intranet.credential?.source || '未配置');
  $('#qwenBaseUrl').value = qwen.base_url || '';
  $('#qwenTimeout').value = qwen.timeout_seconds || '900';
  $('#clearIntranetCredentials').checked = false;
  const device = status.cutout.device || 'auto';
  const deviceSelect = $('#birefnetDevice');
  if (![...deviceSelect.options].some(option => option.value === device)) {
    deviceSelect.add(new Option(device, device));
  }
  deviceSelect.value = device;
  $('#sharedPath').value = '';
  $('#sharedPath').placeholder = status.credential.shared_path_configured
    ? '当前已配置 shared.py；留空沿用' : '已有 shared.py 的本机路径';
  $('#birefnetModelPath').value = '';
  $('#clearCredentials').checked = false;
}

async function loadProviderStatus() {
  try {
    const status = await api('/api/provider-settings');
    renderProviderStatus(status);
    return status;
  } catch (error) {
    $('#providerDot').className = 'provider-dot error';
    $('#providerLabel').textContent = 'Provider 状态异常';
    throw error;
  }
}

function showProviderError(error) {
  const node = $('#providerError');
  node.textContent = describeError(error);
  node.hidden = false;
}

function openProviderDialog() {
  const dialog = $('#providerDialog');
  const canRetry = Boolean(
    state.currentId && ['blocked', 'failed', 'invalid'].includes(state.currentStatus?.stage),
  );
  $('#providerError').hidden = true;
  state.providerFormDirty = false;
  $('#yibuApiKey').value = '';
  $('#intranetApiKey').value = '';
  $('#retryAfterSettingsLabel').hidden = !canRetry;
  $('#retryAfterSettings').checked = canRetry;
  if (state.providerStatus) populateProviderForm(state.providerStatus);
  dialog.showModal();
  loadProviderStatus()
    .then(status => {
      if (!state.providerFormDirty) populateProviderForm(status);
    })
    .catch(showProviderError);
}

function providerFormPayload() {
  const payload = {
    default_vision_provider: $('#defaultVision').value,
    default_image_provider: $('#defaultImage').value,
    intranet_base_url: $('#intranetBaseUrl').value.trim(),
    intranet_model: $('#intranetModel').value.trim(),
    intranet_max_tokens: $('#intranetMaxTokens').value.trim(),
    intranet_timeout: $('#intranetTimeout').value.trim(),
    qwen_base_url: $('#qwenBaseUrl').value.trim(),
    qwen_timeout: $('#qwenTimeout').value.trim(),
  };
  if ($('#clearIntranetCredentials').checked) payload.clear_intranet_credentials = true;
  else if ($('#intranetApiKey').value.trim()) payload.intranet_api_key = $('#intranetApiKey').value.trim();
  const clearCredentials = $('#clearCredentials').checked;
  const apiKey = $('#yibuApiKey').value.trim();
  const sharedPath = $('#sharedPath').value.trim();
  const credentialWillExist = !clearCredentials && Boolean(
    apiKey || sharedPath || state.providerStatus?.credential.configured,
  );

  if (clearCredentials) {
    payload.clear_credentials = true;
  } else if (credentialWillExist) {
    if (apiKey) payload.yibu_api_key = apiKey;
    if (sharedPath) payload.shared_path = sharedPath;
    payload.audit_base_url = $('#auditBaseUrl').value.trim();
    payload.vlm_model = $('#vlmModel').value.trim();
    payload.vlm_max_tokens = $('#vlmMaxTokens').value.trim();
    payload.vlm_reasoning_effort = $('#vlmReasoning').value;
    payload.timeout_seconds = $('#timeoutSeconds').value.trim();
    payload.image_model = $('#imageModel').value.trim();
    payload.image_size = $('#imageSize').value;
  }

  payload.birefnet_device = $('#birefnetDevice').value;
  const modelPath = $('#birefnetModelPath').value.trim();
  if (modelPath) payload.birefnet_model_path = modelPath;
  return {payload, credentialWillExist};
}

async function submitProviderSettings(event) {
  event.preventDefault();
  if (event.submitter?.value === 'cancel') {
    $('#providerDialog').close();
    return;
  }
  const submit = $('#providerSubmit');
  const retry = !$('#retryAfterSettingsLabel').hidden && $('#retryAfterSettings').checked;
  const projectId = state.currentId;
  const {payload} = providerFormPayload();

  submit.disabled = true;
  $('#providerError').hidden = true;
  try {
    const request = api('/api/provider-settings', {method: 'POST', body: payload});
    $('#yibuApiKey').value = '';
    if (payload.yibu_api_key) payload.yibu_api_key = '';
    $('#intranetApiKey').value = '';
    if (payload.intranet_api_key) payload.intranet_api_key = '';
    const status = await request;
    renderProviderStatus(status);
    state.providerFormDirty = false;
    populateProviderForm(status);
    if (retry && projectId) {
      try {
        await api('/api/projects/' + encodeURIComponent(projectId) + '/retry', {
          method: 'POST',
          body: {vision_provider: status.selection.vision, image_provider: status.selection.image},
        });
      } catch (error) {
        showProviderError(new ApiError({
          code: error.code,
          message: '设置已保存，但项目重试失败：' + error.message,
          details: error.details,
        }, error.status));
        return;
      }
    }
    $('#providerDialog').close();
    toast(retry ? 'Provider 已配置，项目正在重试' : 'Provider 设置已应用到当前工作台');
    await loadProjects();
    await refreshCurrent(true);
  } catch (error) {
    showProviderError(error);
  } finally {
    submit.disabled = false;
  }
}

function fallbackProjectId() {
  const now = new Date();
  const pad = value => String(value).padStart(2, '0');
  return `project-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}

function slugify(value) {
  const slug = value.normalize('NFKD').toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^[._-]+|[._-]+$/g, '')
    .slice(0, 64);
  return slug || fallbackProjectId();
}

function renderCreateProviderNotice() {
  const form = $('#createForm');
  let notice = $('#createProviderNotice');
  const status = state.providerStatus;
  const issues = [];
  for (const [kind, id] of [['识别', '#createVision'], ['图片', '#createImage']]) {
    const config = status?.services?.[$(id).value];
    if (config && !config.configured) issues.push(kind + '服务尚未配置。');
    else if (config && ['offline', 'invalid'].includes(config.connection?.state)) issues.push(kind + '服务：' + config.connection.message);
  }
  if (!issues.length) {
    notice?.remove();
    return;
  }
  if (!notice) {
    notice = document.createElement('div');
    notice.id = 'createProviderNotice';
    notice.className = 'notice warning create-provider-notice';
    form.querySelector('.file-drop').before(notice);
  }
  const message = document.createElement('span');
  message.textContent = issues.join(' ');
  const button = document.createElement('button');
  button.className = 'button quiet';
  button.type = 'button';
  button.textContent = '先配置 Provider';
  button.onclick = () => {
    state.returnToCreateAfterProvider = true;
    $('#createDialog').close();
    openProviderDialog();
  };
  notice.replaceChildren(message, button);
}

function renderProjectProviders(status) {
  const panel = $('#projectProviders');
  const providers = status.providers || {};
  const hasTemplate = status.artifacts?.template_manifest?.exists;
  const disabled = activeJob(status.task) || hasTemplate;
  panel.innerHTML = '<strong>本项目服务</strong><div class="form-grid two">' +
    '<label>识别服务<select id="projectVision"' + (disabled ? ' disabled' : '') + '>' +
    serviceOptions('vision', providers.vision_provider, !providers.vision_provider) + '</select></label>' +
    '<label>图片服务<select id="projectImage"' + (disabled ? ' disabled' : '') + '>' +
    serviceOptions('image', providers.image_provider, !providers.image_provider) + '</select></label></div>' +
    (hasTemplate ? '<p>已有素材继续保留。重做单件时可选择服务；修改背景请另存版本。</p>'
      : '<button id="saveProjectProviders" class="button"' + (disabled ? ' disabled' : '') + '>保存本项目选择</button>');
  if (!hasTemplate) $('#saveProjectProviders').onclick = async () => {
    try {
      await api('/api/projects/' + encodeURIComponent(status.project_id) + '/providers', {
        method: 'POST', body: {vision_provider: $('#projectVision').value, image_provider: $('#projectImage').value},
      });
      toast('已保存；后续模型任务使用本项目选择');
      await refreshCurrent(true);
    } catch (error) { toast(describeError(error)); }
  };
}

function openCreateDialog() {
  const dialog = $('#createDialog');
  $('#createError').hidden = true;
  const reviewer = window.localStorage.getItem('figcopy-reviewer');
  if (reviewer) $('#createForm').elements.reviewer.value = reviewer;
  $('#createVision').innerHTML = serviceOptions('vision', state.providerStatus?.selection?.vision);
  $('#createImage').innerHTML = serviceOptions('image', state.providerStatus?.selection?.image);
  renderCreateProviderNotice();
  dialog.showModal();
}

async function submitCreate(event) {
  event.preventDefault();
  if (event.submitter?.value === 'cancel') {
    $('#createDialog').close();
    return;
  }
  const formElement = $('#createForm');
  if (!formElement.reportValidity()) return;
  const submit = $('#createSubmit');
  const errorNode = $('#createError');
  submit.disabled = true;
  errorNode.hidden = true;
  const form = new FormData(formElement);
  if (!String(form.get('vision_provider') || '').trim()) form.set('vision_provider', $('#createVision').value);
  if (!String(form.get('image_provider') || '').trim()) form.set('image_provider', $('#createImage').value);
  const projectId = String(form.get('project_id'));
  try {
    await api('/api/projects', {method: 'POST', body: form});
    window.localStorage.setItem('figcopy-reviewer', String(form.get('reviewer')));
    $('#createDialog').close();
    formElement.reset();
    $('#referenceFileName').textContent = 'JPG / PNG / WEBP，项目的画布与布局从这里开始';
    toast('项目已创建，正在分析参考图');
    navigate(projectId);
    await loadProjects();
  } catch (error) {
    errorNode.textContent = describeError(error);
    errorNode.hidden = false;
  } finally {
    submit.disabled = false;
  }
}

async function boot() {
  const config = await api('/api/config');
  $('#dataDirectory').textContent = config.data_dir;
  await loadProviderStatus().catch(() => null);
  state.currentId = projectIdFromPath();
  await loadProjects();
  await refreshCurrent(true);
  window.setInterval(refreshAll, 2500);
}

$('#newProject').onclick = openCreateDialog;
$('#providerSettings').onclick = openProviderDialog;
$('#createVision').onchange = renderCreateProviderNotice;
$('#createImage').onchange = renderCreateProviderNotice;
$('#providerForm').onsubmit = submitProviderSettings;
$('#providerForm').oninput = () => { state.providerFormDirty = true; };
$('#providerDialog').addEventListener('close', () => {
  const returnToCreate = state.returnToCreateAfterProvider;
  state.returnToCreateAfterProvider = false;
  $('#yibuApiKey').value = '';
  $('#intranetApiKey').value = '';
  $('#providerError').hidden = true;
  if (returnToCreate) window.setTimeout(openCreateDialog, 0);
});
document.querySelectorAll('[data-open-create]').forEach(button => { button.onclick = openCreateDialog; });
document.querySelectorAll('[data-home]').forEach(link => {
  link.onclick = event => { event.preventDefault(); navigate(null); };
});
$('#projectSearch').oninput = renderProjectList;
$('#createForm').onsubmit = submitCreate;
$('#createDialog').querySelectorAll('[value="cancel"]').forEach(button => {
  button.onclick = event => { event.preventDefault(); $('#createDialog').close(); };
});
let projectIdTouched = false;
$('#createForm').elements.project_id.oninput = () => { projectIdTouched = true; };
$('#createForm').elements.name.oninput = event => {
  if (!projectIdTouched) $('#createForm').elements.project_id.value = slugify(event.target.value);
};
$('#createDialog').addEventListener('close', () => { projectIdTouched = false; });
$('#referenceUpload').onchange = event => {
  $('#referenceFileName').textContent = event.target.files[0]?.name || 'JPG / PNG / WEBP，项目的画布与布局从这里开始';
};
window.onpopstate = () => {
  state.currentId = projectIdFromPath();
  state.renderSignature = null;
  renderProjectList();
  refreshCurrent(true);
};

boot().catch(error => {
  $('#connectionState').innerHTML = '<i></i> 启动失败';
  $('#welcomeView').innerHTML = `${actionHeader('STARTUP ERROR', '工作台无法载入', describeError(error))}`;
});
