const $ = selector => document.querySelector(selector);
const projectId = decodeURIComponent(location.pathname.split('/')[2]);
const base = '/api/projects/' + encodeURIComponent(projectId) + '/layout';
const token = document.querySelector('meta[name="figcopy-csrf-token"]').content;
const canvas = $('#view');
const context = canvas.getContext('2d');
let documentState, selected, dragging, dirty = false, busy = false;
const images = new Map();
const escapeHtml = value => String(value).replace(/[&<>"']/g,
  char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const chosen = () => documentState.items.find(item => item.id === selected);
$('#returnLink').href = '/projects/' + encodeURIComponent(projectId);

async function request(path, payload) {
  const response = await fetch(base + path, payload ? {
    method: 'POST', headers: {'content-type':'application/json', 'X-Figcopy-Token':token},
    body: JSON.stringify(payload),
  } : {});
  if (!response.ok) {
    const error = await response.json();
    throw new Error('[' + error.code + '] ' + error.message);
  }
  return response;
}

function payload() {
  return {revision: documentState.revision, items: documentState.items.map(item => ({
    id:item.id, rect:item.rect, rotation_deg:item.rotation_deg,
  }))};
}

function render() {
  $('#exactPreview').hidden = true;
  canvas.hidden = false;
  context.clearRect(0, 0, canvas.width, canvas.height);
  for (const item of documentState.items) {
    const image = images.get(item.id);
    const [x, y, w, h] = item.rect;
    context.save();
    context.translate(x + w / 2, y + h / 2);
    context.rotate(item.rotation_deg * Math.PI / 180);
    context.beginPath();
    context.rect(-w/2, -h/2, w, h);
    context.clip();
    const scale = item.fit === 'cover' ? Math.max(w/image.width, h/image.height) : Math.min(w/image.width, h/image.height);
    const iw = image.width * scale, ih = image.height * scale;
    context.drawImage(image, -w/2 + (w-iw)*item.anchor[0], -h/2 + (h-ih)*item.anchor[1], iw, ih);
    context.restore();
    if (item.id === selected) {
      context.save();
      context.translate(x+w/2, y+h/2);
      context.rotate(item.rotation_deg * Math.PI / 180);
      context.strokeStyle = '#ff42c8';
      context.lineWidth = Math.max(2, canvas.width / 500);
      context.strokeRect(-w/2, -h/2, w, h);
      context.restore();
    }
  }
}

function controls() {
  const item = chosen();
  $('#controls').disabled = busy || !item?.editable;
  $('#selection').textContent = item ? item.label + (item.editable ? '' : '（位置已锁定）') : '请选择装饰';
  if (item) {
    ['x','y','width','height'].forEach((key, i) => { $('#' + key).value = item.rect[i]; });
    $('#rotation').value = item.rotation_deg;
  }
  $('#layers').innerHTML = [...documentState.items].reverse().map(item =>
    '<button class="card ' + (item.id === selected ? 'selected' : '') + '" data-id="' + escapeHtml(item.id) + '">'
      + escapeHtml(item.label) + (item.background ? ' · 背景' : item.editable ? ' · 独立装饰' : ' · 照片/文字槽') + '</button>'
  ).join('');
  $('#layers').querySelectorAll('button').forEach(button => {
    button.disabled = busy;
    button.onclick = () => { selected = button.dataset.id; controls(); render(); };
  });
}

function change() {
  dirty = true;
  controls();
  render();
}

['x','y','width','height'].forEach((key, index) => {
  $('#' + key).onchange = event => {
    const item = chosen(), value = Number(event.target.value);
    if (!item?.editable || !Number.isFinite(value) || (index > 1 && value < 1)) return;
    if (index > 1 && $('#keepAspect').checked) {
      const other = index === 2 ? 3 : 2;
      item.rect[other] = Math.max(1, Math.round(item.rect[other] * value / item.rect[index]));
    }
    item.rect[index] = value;
    change();
  };
});
$('#rotation').onchange = event => {
  const value = Number(event.target.value);
  if (chosen()?.editable && Number.isFinite(value)) { chosen().rotation_deg = value; change(); }
};
function reorder(delta) {
  const index = documentState.items.findIndex(item => item.id === selected);
  const next = index + delta;
  if (index <= 0 || next <= 0 || next >= documentState.items.length) return;
  [documentState.items[index], documentState.items[next]] = [documentState.items[next], documentState.items[index]];
  change();
}
$('#up').onclick = () => reorder(1);
$('#down').onclick = () => reorder(-1);

function point(event) {
  const bounds = canvas.getBoundingClientRect();
  return [(event.clientX-bounds.left)*canvas.width/bounds.width, (event.clientY-bounds.top)*canvas.height/bounds.height];
}
canvas.onpointerdown = event => {
  if (busy) return;
  const [x,y] = point(event);
  const hit = [...documentState.items].reverse().find(item => {
    if (!item.editable) return false;
    const [lx,ly,w,h] = item.rect;
    const angle = -item.rotation_deg * Math.PI/180;
    const dx = x-lx-w/2, dy = y-ly-h/2;
    return Math.abs(dx*Math.cos(angle)-dy*Math.sin(angle)) <= w/2
      && Math.abs(dx*Math.sin(angle)+dy*Math.cos(angle)) <= h/2;
  });
  if (!hit) return;
  selected = hit.id;
  dragging = {x,y,rect:[...hit.rect]};
  canvas.setPointerCapture(event.pointerId);
  controls(); render();
};
canvas.onpointermove = event => {
  if (!dragging) return;
  const [x,y] = point(event), item = chosen();
  item.rect[0] = Math.round(dragging.rect[0]+x-dragging.x);
  item.rect[1] = Math.round(dragging.rect[1]+y-dragging.y);
  dirty = true; render();
};
canvas.onpointerup = canvas.onpointercancel = () => { dragging = null; if (documentState) controls(); };

function setBusy(value) {
  busy = value;
  $('#save').disabled = $('#preview').disabled = value;
  controls();
}
$('#preview').onclick = async () => {
  setBusy(true);
  try {
    const response = await request('/preview', payload());
    if ($('#exactPreview').src.startsWith('blob:')) URL.revokeObjectURL($('#exactPreview').src);
    $('#exactPreview').src = URL.createObjectURL(await response.blob());
    $('#exactPreview').hidden = false;
    canvas.hidden = true;
    $('#status').textContent = '已用正式本地 renderer 合成。点击图层可继续调整。';
  } catch (error) { $('#status').textContent = error.message; }
  finally { setBusy(false); }
};
$('#save').onclick = async () => {
  setBusy(true);
  try {
    const result = await (await request('/save', payload())).json();
    dirty = false;
    location.assign(result.url);
  } catch (error) { $('#status').textContent = error.message; setBusy(false); }
};
window.addEventListener('beforeunload', event => {
  if (dirty) { event.preventDefault(); event.returnValue = ''; }
});

async function load() {
  documentState = await (await request('')).json();
  canvas.width = documentState.canvas.width; canvas.height = documentState.canvas.height;
  await Promise.all(documentState.items.map(async item => {
    const image = new Image();
    image.src = base + '/layers/' + encodeURIComponent(item.id);
    await image.decode(); images.set(item.id, image);
  }));
  selected = documentState.items.find(item => item.editable)?.id;
  controls(); render();
  $('#status').textContent = '每件装饰都可单独调整。修改位置不会调用生成模型。';
}
load().catch(error => { $('#status').textContent = '载入失败：' + error.message; });
