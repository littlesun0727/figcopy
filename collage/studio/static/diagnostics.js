const diagnosticState = {project: null, selected: '', job: null, after: 0, events: [], busy: false, attemptsSignature: null, openedFailure: null};

function projectError(status) {
  const taskError = status.task?.state === 'failed' ? status.task.error : null;
  const error = taskError || status.last_error;
  if (taskError && status.last_error?.code === taskError.code) {
    return {...error, details: {...status.last_error.details, ...taskError.details}};
  }
  return error;
}

function errorDetailsHtml(error) {
  const details = error?.details || {};
  const issues = Array.isArray(details.issues) ? details.issues : [];
  const metadata = ['operation', 'http_status', 'request_id', 'attempt_id', 'finish_reason']
    .filter(key => details[key] !== undefined && details[key] !== '')
    .map(key => `<div><code>${escapeHtml(key)}</code> · ${escapeHtml(details[key])}</div>`).join('');
  return `${issues.length ? `<details open><summary>共 ${issues.length} 项校验问题</summary><ol class="validation-issues">${issues.map(issue =>
    `<li><code>${escapeHtml(issue.path)}</code> · ${escapeHtml(issue.code)}<br>${escapeHtml(issue.message)}</li>`).join('')}</ol></details>` : ''}
    ${metadata}${details.response ? `<details open><summary>服务错误详情（已脱敏）</summary><pre>${escapeHtml(details.response)}</pre></details>` : ''}`;
}

function renderErrorDetails(error) {
  const container = document.getElementById('errorDetails');
  container.innerHTML = errorDetailsHtml(error);
  container.hidden = !container.innerHTML.trim();
}

async function refreshDiagnostics() {
  if (!state.currentId || diagnosticState.busy) return;
  const project = state.currentId;
  if (diagnosticState.project !== project) {
    Object.assign(diagnosticState, {project, selected: '', job: null, after: 0, events: [], attemptsSignature: null, openedFailure: null});
    document.getElementById('taskLog').textContent = '正在读取运行记录…';
    document.getElementById('analysisAttempts').replaceChildren();
    document.getElementById('diagnosticsPanel').open = false;
  }
  const selected = diagnosticState.selected;
  const query = new URLSearchParams({after: String(diagnosticState.after)});
  if (selected) query.set('job_id', selected);
  diagnosticState.busy = true;
  try {
    let payload = await api(`/api/projects/${encodeURIComponent(project)}/diagnostics?${query}`);
    if (state.currentId !== project || diagnosticState.selected !== selected) return;
    if (payload.job && payload.job.id !== diagnosticState.job) {
      diagnosticState.events = [];
      diagnosticState.after = 0;
      // A new attempt can have a lower sequence number than the previous one.
      if (query.get('after') !== '0') {
        query.set('after', '0');
        payload = await api(`/api/projects/${encodeURIComponent(project)}/diagnostics?${query}`);
        if (state.currentId !== project || diagnosticState.selected !== selected) return;
      }
    }
    const job = payload.job;
    diagnosticState.job = job?.id || null;
    const download = document.getElementById('downloadDiagnostics');
    download.href = `/api/projects/${encodeURIComponent(project)}/diagnostics?after=0${job ? '&job_id=' + encodeURIComponent(job.id) : ''}`;
    const selector = document.getElementById('diagnosticJob');
    const options = '<option value="">最新任务</option>' + payload.jobs.map(item =>
      `<option value="${escapeHtml(item.id)}">${escapeHtml(new Date(item.created_at).toLocaleString())} · ${escapeHtml(JOB_LABELS[item.kind] || item.kind)} · ${escapeHtml(item.state)}</option>`).join('');
    if (selector.innerHTML !== options) selector.innerHTML = options;
    selector.value = diagnosticState.selected;
    selector.onchange = () => {
      Object.assign(diagnosticState, {selected: selector.value, job: null, after: 0, events: []});
      refreshDiagnostics();
    };
    if (job?.truncated) diagnosticState.events = [];
    const events = job?.events || [];
    diagnosticState.events.push(...events);
    diagnosticState.events = diagnosticState.events.slice(-1000);
    diagnosticState.after = job?.last_seq || 0;
    const log = document.getElementById('taskLog');
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.textContent = diagnosticState.events.map(event =>
      `${new Date(event.at).toLocaleTimeString()} | ${event.level} | ${event.message}`).join('\n') || '暂无任务日志。旧任务的过程日志无法补录。';
    if (atBottom) log.scrollTop = log.scrollHeight;
    document.getElementById('diagnosticHint').textContent = job?.log_error?.message ||
      (job?.last_seq > 1000 ? '仅保留该任务最近 1000 条日志。' : '自动更新；任务记录在重启后仍可查看。');
    document.getElementById('diagnosticError').innerHTML = job?.error
      ? `<p>${escapeHtml(describeError(job.error))}</p>${errorDetailsHtml(job.error)}` : '';
    if (job?.state === 'failed' && diagnosticState.openedFailure !== job.id) {
      document.getElementById('diagnosticsPanel').open = true;
      diagnosticState.openedFailure = job.id;
    }
    const signature = JSON.stringify(payload.attempts);
    if (signature !== diagnosticState.attemptsSignature) {
      diagnosticState.attemptsSignature = signature;
      document.getElementById('analysisAttempts').innerHTML = payload.attempts.map(attempt => {
        const labels = {'response.txt': '模型原始回答', 'response.json': '模型响应 JSON', 'candidate.json': '校验前候选',
          'assembled_draft.json': '附加元数据后的候选', 'validation.json': '完整校验报告', 'prompt.txt': '提示词',
          'transport_response.txt': '服务原始响应', 'response_metadata.json': '响应信息'};
        const links = Object.entries(attempt.files).filter(([name]) => labels[name]).map(([name, url]) =>
          `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${labels[name]}</a>`).join(' · ');
        const responseHint = !attempt.files['response.txt'] && !attempt.files['candidate.json']
          ? (attempt.status === 'running' ? '等待模型回答' : '未取得可用草稿；请查看错误详情') : '';
        return `<details class="analysis-attempt"><summary>${escapeHtml(new Date(attempt.started_at).toLocaleString())} · ${escapeHtml(attempt.model || attempt.provider)} · ${escapeHtml(attempt.status)}${attempt.fixture ? ' · fixture' : ''}</summary>
          <p>阶段：${escapeHtml(attempt.phase)} ${responseHint}</p><p>${links}</p>${attempt.error ? `<p>${escapeHtml(describeError(attempt.error))}</p>${errorDetailsHtml(attempt.error)}` : ''}</details>`;
      }).join('') || '<p>暂无分析诊断文件。新分析会在校验前保存模型回答。</p>';
    }
  } catch (error) {
    if (state.currentId === project) document.getElementById('diagnosticHint').textContent = '运行记录读取失败：' + describeError(error);
  } finally {
    diagnosticState.busy = false;
  }
}
