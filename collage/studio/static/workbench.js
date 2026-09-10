const $ = selector => document.querySelector(selector);
const csrfToken = $('meta[name="figcopy-csrf-token"]').content;
const state = {
  projects: [],
  currentId: null,
  currentStatus: null,
  renderSignature: null,
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
    ${actionHeader('HUMAN GATE 1 / 2', '确认识别区域与清除范围', '检查蓝色内容槽、粉色装饰层和白色清除区域。保存后会自动继续制作模板。')}
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
    <div class="notice warning"><strong>${escapeHtml(error?.code || 'WORKFLOW_PAUSED')}</strong><br>${escapeHtml(error?.message || status.next_action)}</div>
    <form id="retryForm" class="retry-form">
      <label>VLM Provider<input id="retryVision" placeholder="module:object（留空沿用原设置）"></label>
      <label>图片 Provider<input id="retryImage" placeholder="module:object（留空沿用原设置）"></label>
      <label>抠图 Provider<input id="retryCutout" placeholder="module:object（留空沿用原设置）"></label>
      <label class="check"><input id="retryFixture" type="checkbox"> 改用离线 fixture 图片 Provider（仅测试）</label>
      <label class="check"><input id="retryCloud" type="checkbox"> 明确允许云端上传</label>
      <div><button id="retryButton" class="button primary" type="submit">按当前配置重试</button></div>
    </form>`;
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

function renderProject(status) {
  state.currentStatus = status;
  $('#welcomeView').hidden = true;
  $('#projectView').hidden = false;
  $('#projectId').textContent = status.project_id;
  $('#projectName').textContent = status.name;
  $('#stageBadge').textContent = STAGE_LABELS[status.stage] || status.stage;
  renderSteps(status);
  renderArtifacts(status);

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

function openCreateDialog() {
  const dialog = $('#createDialog');
  $('#createError').hidden = true;
  const reviewer = window.localStorage.getItem('figcopy-reviewer');
  if (reviewer) $('#createForm').elements.reviewer.value = reviewer;
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
  state.currentId = projectIdFromPath();
  await loadProjects();
  await refreshCurrent(true);
  window.setInterval(refreshAll, 2500);
}

$('#newProject').onclick = openCreateDialog;
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
