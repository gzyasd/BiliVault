const $ = (id) => document.querySelector(`[data-dom-id="${id}"]`);

let currentMode = 'quick';
let currentSid = null;
const eventSources = {
  pipeline: null,
  execution: null,
  refine: null,
  cleanup: null,
};
let qrPollToken = 0;
let currentView = 'home';
let utilityReturnContext = null;
let lastResultStats = null;
let activeReviewFilter = 'ALL';
let isExecuting = false;
let categoryLimit = 14;
let categoryGranularity = 'balanced';
const CATEGORY_LIMIT_PRESETS = { coarse: 8, balanced: 14, detailed: 24 };
let folderResourceState = null;
const collapsedReviewGroups = new Set();
const selectedSourceFids = new Set();
const selectedEmptyFolderFids = new Set();
let emptyFolderSelectionMode = false;
let emptyFolderCandidates = new Map();
let deletingEmptyFolders = false;
let folderSortMode = false;
let folderSortOriginalIds = [];
let savingFolderOrder = false;
let folderSortNotice = '';
let nativeDraggedFolderRow = null;
let skippedPanelCollapsed = false;
const collapsedSkippedReasons = new Set();
let cachedSkippedItems = null;
let activeRefineJob = null;
let activeRefineKind = null;
let lastRefineProgress = null;
let lastRefineInstruction = '';
let refineNotice = '';
const cleanupState = {
  scan: null,
  items: [],
  filter: 'all',
  selected: new Set(),
  collapsedFolders: new Set(),
};

function escapeHtml(value) {
  const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return String(value ?? '').replace(/[&<>"']/g, ch => map[ch]);
}

function isDeletableEmptyFolder(f) {
  return Number(f.media_count) === 0
    && Number(f.fav_state) !== 1
    && !Boolean(f.is_default);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(data.message || `请求失败 ${r.status}`);
    err.code = data.code;
    if (err.code === 'NOT_LOGGED_IN') {
      showView('login');
      renderLogin();
    } else if (err.code === 'AI_AUTH_FAILED' || err.code === 'AI_NOT_CONFIGURED') {
      showView('config');
      loadConfig();
    }
    throw err;
  }
  return data;
}

function closeEventSource(slot, expected = null) {
  const source = eventSources[slot];
  if (!source || (expected && source !== expected)) return;
  source.close();
  eventSources[slot] = null;
}

function replaceEventSource(slot, source) {
  closeEventSource(slot);
  eventSources[slot] = source;
  return source;
}

function cleanupPollingAndSSE() {
  Object.keys(eventSources).forEach(slot => closeEventSource(slot));
  qrPollToken++;
}

function showView(name) {
  if (currentView !== name) cleanupPollingAndSSE();
  document.querySelectorAll('[data-view]').forEach(s => s.classList.remove('active'));
  document.querySelector(`[data-view="${name}"]`).classList.add('active');
  currentView = name;
  if (window.lucide) lucide.createIcons();
}

function openUtilityView(name) {
  if (currentView === 'accounts' && name !== 'accounts') {
    void cancelAddAccountLogin();
  }
  if (!['config', 'accounts'].includes(currentView)) {
    utilityReturnContext = { view: currentView, sid: currentSid };
  }
  showView(name);
  if (name === 'config') loadConfig();
  if (name === 'accounts') renderAccounts();
}

async function returnFromUtilityView() {
  const context = utilityReturnContext;
  utilityReturnContext = null;
  if (!context) {
    await start();
    return;
  }

  switch (context.view) {
    case 'progress':
      if (!context.sid) break;
      currentSid = context.sid;
      showView('progress');
      runPipeline(context.sid, { reset: false });
      return;
    case 'review':
      if (!context.sid) break;
      await openSession(context.sid);
      return;
    case 'result':
      if (!context.sid || !lastResultStats) break;
      await renderResult(context.sid, lastResultStats);
      return;
    case 'cleanup':
      await openCleanup();
      return;
    case 'folder-resources':
      if (!folderResourceState) break;
      await openFolderResources(
        folderResourceState.fid,
        folderResourceState.title,
        folderResourceState.declaredCount,
      );
      return;
    case 'home':
      showView('home');
      await renderHome();
      return;
    default:
      break;
  }

  showView('home');
  await renderHome();
}

function resetUtilityReturnAfterAccountChange() {
  utilityReturnContext = { view: 'home', sid: null };
  currentSid = null;
  activeRefineJob = null;
  activeRefineKind = null;
  lastRefineProgress = null;
  lastResultStats = null;
}

async function start() {
  try {
    const state = await api('/api/state');
    if (!state.configured) { showView('config'); loadConfig(); return; }
    if (!state.logged_in) { showView('login'); renderLogin(); return; }
    showView('home'); renderHome();
  } catch (e) {
    alert(e.message);
  }
}

function loadConfig() {
  api('/api/config').then(cfg => {
    if (cfg.configured) {
      $('config-base-url').value = cfg.ai_base_url || '';
      $('config-model').value = cfg.ai_model || '';
      const batchInput = $('config-ai-batch-size');
      if (batchInput) batchInput.value = cfg.ai_batch_size || 100;
      $('config-cancel').style.display = 'inline-flex';
    } else {
      $('config-cancel').style.display = 'none';
    }
  });
  $('config-save').onclick = async () => {
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({
          ai_base_url: $('config-base-url').value,
          ai_api_key: $('config-api-key').value,
          ai_model: $('config-model').value,
          ai_batch_size: Number($('config-ai-batch-size').value || 100),
        }),
      });
      await returnFromUtilityView();
    } catch (e) { alert(e.message); }
  };
  $('config-cancel').onclick = returnFromUtilityView;
}

async function renderLogin() {
  api('/api/state').then(state => {
    $('login-back').style.display = state.configured ? 'inline-flex' : 'none';
  }).catch(() => { $('login-back').style.display = 'none'; });
  const data = await api('/api/qrcode/generate', { method: 'POST' });
  $('qr-image').innerHTML = `<img src="${data.image}" style="width:100%;height:100%;border-radius:8px;">`;
  $('qr-status').textContent = '等待扫码...';
  const myToken = ++qrPollToken;
  pollQrcode(data.qrcode_key, myToken);
  $('login-refresh-qr').onclick = () => renderLogin();
  $('login-back').onclick = () => { start(); };
}

async function pollQrcode(key, myToken) {
  const startTime = Date.now();
  const tick = async () => {
    if (myToken !== qrPollToken) return;
    if (Date.now() - startTime > 180000) {
      $('qr-status').textContent = '二维码已过期，请刷新';
      return;
    }
    try {
      const r = await api(`/api/qrcode/poll?qrcode_key=${key}`);
      if (myToken !== qrPollToken) return;
      if (r.status === 'success') { start(); return; }
      if (r.status === 'scanned') $('qr-status').textContent = '已扫码，请在手机确认';
      if (r.status === 'expired') { $('qr-status').textContent = '二维码已过期，请刷新'; return; }
      setTimeout(tick, 2000);
    } catch (e) { if (myToken === qrPollToken) $('qr-status').textContent = e.message; }
  };
  tick();
}

function renderFolderLoadingState() {
  const sortBar = $('folder-sort-bar');
  if (sortBar) {
    sortBar.style.display = 'none';
    sortBar.innerHTML = '';
  }
  const batchBar = $('empty-folder-batch-bar');
  if (batchBar) {
    batchBar.style.display = 'none';
    batchBar.innerHTML = '';
  }
  const list = $('folder-list');
  if (list) {
    const skeletonRows = Array.from({ length: 4 }, (_, index) => `
      <div class="flex items-center gap-4 p-4 rounded-xl" style="height:80px;background:var(--card);border:1px solid var(--border);box-shadow:var(--shadow-xs);">
        <div class="skeleton-pulse shrink-0 w-12 h-12 rounded-xl" style="background:var(--background-300);animation-delay:${index * 90}ms;"></div>
        <div class="flex-1 min-w-0 flex flex-col gap-2.5">
          <div class="skeleton-pulse h-4 rounded-md" style="width:${46 + index * 7}%;background:var(--background-300);animation-delay:${index * 90}ms;"></div>
          <div class="skeleton-pulse h-3 rounded-md" style="width:28%;background:var(--background-200);animation-delay:${index * 90}ms;"></div>
        </div>
        <div class="skeleton-pulse shrink-0 w-5 h-5 rounded-md" style="background:var(--background-200);animation-delay:${index * 90}ms;"></div>
      </div>`).join('');
    list.innerHTML = `
      <div data-dom-id="folder-loading-status" class="flex items-center justify-center gap-2 py-2 text-sm" style="color:var(--muted-foreground);" role="status" aria-live="polite">
        <i data-lucide="loader-circle" class="w-4 h-4 spin-slow"></i><span>正在从 B 站加载收藏夹...</span>
      </div>
      ${skeletonRows}`;
  }
  const startButton = $('start-organize');
  if (startButton) {
    startButton.disabled = true;
    startButton.querySelector('span').textContent = '正在加载收藏夹';
  }
  if (window.lucide) lucide.createIcons();
}

function renderFolderLoadError(error) {
  const list = $('folder-list');
  if (list) {
    list.innerHTML = `
      <div class="flex flex-col sm:flex-row items-center justify-between gap-3 p-4 rounded-lg" style="background:var(--state-error-surface);border:1px solid var(--border);" role="alert">
        <div class="flex items-center gap-2 min-w-0">
          <i data-lucide="circle-alert" class="w-5 h-5 shrink-0" style="color:var(--state-error);"></i>
          <span class="text-sm" style="color:var(--state-error);">${escapeHtml(error.message || '收藏夹加载失败')}</span>
        </div>
        <button data-dom-id="folder-load-retry" type="button" class="btn btn-secondary shrink-0">
          <i data-lucide="refresh-cw" class="w-4 h-4"></i><span>重新加载</span>
        </button>
      </div>`;
    $('folder-load-retry').onclick = renderHome;
  }
  const startButton = $('start-organize');
  if (startButton) {
    startButton.disabled = true;
    startButton.querySelector('span').textContent = '开始智能整理';
  }
  if (window.lucide) lucide.createIcons();
}

async function renderHome() {
  try {
    renderFolderLoadingState();
    selectedSourceFids.clear();
    selectedEmptyFolderFids.clear();
    emptyFolderSelectionMode = false;
    emptyFolderCandidates = new Map();
    folderSortMode = false;
    folderSortOriginalIds = [];
    savingFolderOrder = false;
    folderSortNotice = '';
    updateFolderSelectionUi();
    const resumable = await api('/api/sessions/resumable');
    const resumeEl = $('resume-session');
    if (resumable.sessions.length) {
      const s = resumable.sessions[0];
      resumeEl.style.display = 'block';
      resumeEl.innerHTML = `
        <div class="flex items-center gap-4 p-4 rounded-xl" style="background: linear-gradient(135deg, var(--brand-50), var(--brand-100));">
          <div class="shrink-0 flex items-center justify-center w-10 h-10 rounded-xl" style="background: var(--brand-500);">
            <i data-lucide="play" style="width:18px;height:18px;color:var(--primary-foreground);"></i>
          </div>
          <div class="flex-1 min-w-0">
            <p class="text-sm font-semibold truncate" style="color: var(--brand-800);">收藏夹 #${escapeHtml(s.source_fid)}</p>
            <p class="text-xs truncate mt-0.5" style="color: var(--brand-600);">状态: ${escapeHtml(s.status)}</p>
          </div>
          <button class="btn btn-primary shrink-0" data-dom-id="resume-continue" style="height:32px;padding:0 14px;font-size:12px;">继续</button>
        </div>`;
      $('resume-continue').onclick = () => resumeSession(s);
    } else {
      resumeEl.style.display = 'none';
    }

    const data = await api('/api/folders');
    emptyFolderCandidates = new Map(data.folders
      .filter(isDeletableEmptyFolder)
      .map(f => [Number(f.fid), f]));
    const colors = ['var(--brand-100)', 'var(--state-success-surface)', 'var(--state-error-surface)', 'var(--brand-50)'];
    const iconColors = ['var(--brand-600)', 'var(--state-success)', 'var(--state-error)', 'var(--brand-500)'];
    $('folder-list').innerHTML = data.folders.map((f, i) => `
      <div data-dom-id="select-folder-${f.fid}" data-folder-id="${f.fid}" class="flex items-center gap-4 p-4 rounded-xl cursor-pointer transition-all hover:shadow-md hover:-translate-y-0.5" style="background: var(--card); border: 1px solid var(--border); box-shadow: var(--shadow-xs);">
        <button data-role="folder-drag-handle" type="button" class="btn btn-text shrink-0" style="width:32px;height:40px;padding:0;display:none;cursor:grab;touch-action:none;" aria-label="拖动调整收藏夹顺序：${escapeHtml(f.title)}" title="拖动排序">
          <i data-lucide="grip-vertical" class="w-5 h-5"></i>
        </button>
        <div class="shrink-0 flex items-center justify-center w-12 h-12 rounded-xl" style="background: ${colors[i % 4]};">
          <i data-lucide="folder" style="width:24px;height:24px;color:${iconColors[i % 4]};"></i>
        </div>
        <div class="flex-1 min-w-0">
          <p class="text-sm font-medium truncate" style="color: var(--foreground);">${escapeHtml(f.title)}</p>
          <p class="text-xs truncate mt-0.5" style="color: var(--muted-foreground);">${escapeHtml(f.media_count)} 个收藏条目</p>
        </div>
        <div data-role="folder-check" class="shrink-0 items-center justify-center w-6 h-6 rounded-full" style="background:var(--brand-500);color:var(--primary-foreground);display:none;">
          <i data-lucide="check" class="w-4 h-4"></i>
        </div>
        ${isDeletableEmptyFolder(f) ? `
          <label data-role="empty-folder-batch-select" class="shrink-0 items-center justify-center w-9 h-9" style="display:none;" aria-label="选择空收藏夹：${escapeHtml(f.title)}">
            <input data-empty-folder-select="${f.fid}" type="checkbox" class="w-4 h-4" style="accent-color:var(--state-error);">
          </label>
          <button data-role="empty-folder-single-delete" data-folder-row-action data-dom-id="delete-empty-folder-${f.fid}" type="button" class="btn btn-text shrink-0" style="width:36px;height:36px;padding:0;color:var(--state-error);" aria-label="删除空收藏夹：${escapeHtml(f.title)}" title="删除空收藏夹">
            <i data-lucide="trash-2" class="w-4 h-4"></i>
          </button>` : ''}
        <button data-folder-row-action data-dom-id="view-folder-resources-${f.fid}" type="button" class="btn btn-text shrink-0" style="width:36px;height:36px;padding:0;" aria-label="查看收藏夹资源：${escapeHtml(f.title)}" title="查看资源">
          <i data-lucide="chevron-right" class="w-[18px] h-[18px]" style="color:var(--icon-300);"></i>
        </button>
      </div>`).join('') || '<p style="color:var(--muted-foreground);">暂无收藏夹</p>';

    data.folders.forEach(f => {
      const fid = Number(f.fid);
      $(`select-folder-${f.fid}`).onclick = () => {
        if (folderSortMode) return;
        if (emptyFolderSelectionMode && emptyFolderCandidates.has(fid)) {
          toggleEmptyFolderSelection(fid);
          return;
        }
        toggleFolderSelection(fid);
      };
      const viewButton = $(`view-folder-resources-${f.fid}`);
      if (viewButton) {
        viewButton.onclick = event => {
          event.stopPropagation();
          openFolderResources(f.fid, f.title, f.media_count);
        };
      }
      if (isDeletableEmptyFolder(f)) {
        const checkbox = document.querySelector(`[data-empty-folder-select="${f.fid}"]`);
        if (checkbox) {
          checkbox.onclick = event => event.stopPropagation();
          checkbox.onchange = () => toggleEmptyFolderSelection(fid, checkbox.checked);
        }
        const deleteButton = $(`delete-empty-folder-${f.fid}`);
        if (deleteButton) {
          deleteButton.onclick = event => deleteEmptyFolder(event, f.fid, f.title);
        }
      }
      const row = $(`select-folder-${f.fid}`);
      const dragHandle = row && row.querySelector('[data-role="folder-drag-handle"]');
      if (row && dragHandle) {
        bindFolderDragHandle(dragHandle, row);
        bindFolderDragSurface(row, row);
        bindNativeFolderDrag(row);
      }
    });
    renderEmptyFolderBatchControls();
    folderSortOriginalIds = data.folders.map(f => Number(f.fid));
    renderFolderSortControls();
    if (window.lucide) lucide.createIcons();

    Object.keys(CATEGORY_LIMIT_PRESETS).forEach(name => {
      $(`granularity-${name}`).onclick = () => setCategoryGranularity(name);
    });
    $('granularity-custom').onclick = () => setCategoryGranularity('custom');
    $('granularity-custom-limit').oninput = () => {
      if (categoryGranularity === 'custom') updateCustomCategoryLimit();
    };
    $('granularity-custom-limit').onblur = () => updateCustomCategoryLimit(true);
    setCategoryGranularity(categoryGranularity);

    $('start-organize').onclick = () => {
      if (!selectedSourceFids.size) return;
      newSession([...selectedSourceFids]);
    };

    $('mode-quick').onclick = () => setMode('quick');
    $('mode-full').onclick = () => setMode('full');
    $('open-cleanup').onclick = openCleanup;
  } catch (e) {
    if (e.code === 'NOT_LOGGED_IN') {
      showView('login');
      renderLogin();
      return;
    }
    renderFolderLoadError(e);
  }
}

function getCurrentFolderOrder() {
  const list = $('folder-list');
  if (!list) return [];
  return [...list.querySelectorAll('[data-folder-id]')]
    .map(row => Number(row.getAttribute('data-folder-id')));
}

function folderOrderChanged() {
  const current = getCurrentFolderOrder();
  return current.length === folderSortOriginalIds.length
    && current.some((fid, index) => fid !== folderSortOriginalIds[index]);
}

function updateFolderSortSaveState() {
  const saveButton = $('folder-sort-save');
  if (saveButton) saveButton.disabled = savingFolderOrder || !folderOrderChanged();
}

function updateFolderSortUi() {
  const list = $('folder-list');
  if (list) list.classList.toggle('folder-sort-mode', folderSortMode);
  document.querySelectorAll('[data-folder-id]').forEach(row => {
    row.setAttribute('aria-grabbed', folderSortMode ? 'false' : '');
    row.style.touchAction = folderSortMode ? 'none' : 'pan-y';
    row.draggable = folderSortMode;
  });
  document.querySelectorAll('[data-role="folder-drag-handle"]').forEach(handle => {
    handle.style.display = folderSortMode ? 'inline-flex' : 'none';
  });
  document.querySelectorAll('[data-folder-row-action]').forEach(action => {
    if (folderSortMode) {
      action.style.display = 'none';
    } else if (!action.matches('[data-role="empty-folder-single-delete"]')) {
      action.style.display = 'inline-flex';
    }
  });
  updateFolderSelectionUi();
  updateFolderSortSaveState();
}

function renderFolderSortControls() {
  const bar = $('folder-sort-bar');
  if (!bar) return;
  bar.style.display = folderSortOriginalIds.length ? 'block' : 'none';
  if (!folderSortOriginalIds.length) {
    bar.innerHTML = '';
    return;
  }

  if (!folderSortMode) {
    bar.innerHTML = `
      <div class="flex flex-wrap items-center justify-between gap-3 py-3" style="border-bottom:1px solid var(--border);">
        <span class="text-sm" style="color:${folderSortNotice ? 'var(--state-success)' : 'var(--muted-foreground)'};">${escapeHtml(folderSortNotice || '可调整收藏夹在 B 站中的显示顺序')}</span>
        <button data-dom-id="folder-sort-start" type="button" class="btn btn-secondary" ${folderSortOriginalIds.length < 2 ? 'disabled' : ''}>
          <i data-lucide="arrow-up-down" class="w-4 h-4"></i><span>调整收藏夹顺序</span>
        </button>
      </div>`;
    $('folder-sort-start').onclick = startFolderSort;
  } else {
    bar.innerHTML = `
      <div class="flex flex-wrap items-center justify-between gap-3 p-3 rounded-lg" style="background:var(--background-100);border:1px solid var(--border);">
        <span class="text-sm font-medium" style="color:var(--foreground);">拖动左侧手柄调整顺序</span>
        <div class="flex items-center gap-2">
          <button data-dom-id="folder-sort-cancel" type="button" class="btn btn-text" ${savingFolderOrder ? 'disabled' : ''}>取消</button>
          <button data-dom-id="folder-sort-save" type="button" class="btn btn-primary" ${savingFolderOrder || !folderOrderChanged() ? 'disabled' : ''}>
            ${savingFolderOrder
              ? '<i data-lucide="loader-circle" class="w-4 h-4 spin-slow"></i><span>正在保存</span>'
              : '<i data-lucide="save" class="w-4 h-4"></i><span>保存排序</span>'}
          </button>
        </div>
      </div>`;
    $('folder-sort-cancel').onclick = cancelFolderSort;
    $('folder-sort-save').onclick = saveFolderSort;
  }
  updateFolderSortUi();
  if (window.lucide) lucide.createIcons();
}

function startFolderSort() {
  if (savingFolderOrder || folderSortOriginalIds.length < 2) return;
  folderSortOriginalIds = getCurrentFolderOrder();
  folderSortNotice = '';
  folderSortMode = true;
  emptyFolderSelectionMode = false;
  selectedEmptyFolderFids.clear();
  selectedSourceFids.clear();
  renderEmptyFolderBatchControls();
  renderFolderSortControls();
}

function cancelFolderSort() {
  if (savingFolderOrder) return;
  const list = $('folder-list');
  folderSortOriginalIds.forEach(fid => {
    const row = $(`select-folder-${fid}`);
    if (list && row) list.appendChild(row);
  });
  folderSortMode = false;
  renderEmptyFolderBatchControls();
  renderFolderSortControls();
}

async function saveFolderSort() {
  if (!folderSortMode || savingFolderOrder || !folderOrderChanged()) return;
  const fids = getCurrentFolderOrder();
  savingFolderOrder = true;
  renderFolderSortControls();
  try {
    const result = await api('/api/folders/sort', {
      method: 'POST',
      body: JSON.stringify({ fids }),
    });
    folderSortOriginalIds = (result.fids || fids).map(Number);
    folderSortMode = false;
    folderSortNotice = '排序已保存到 B 站';
  } catch (error) {
    alert(error.message);
  } finally {
    savingFolderOrder = false;
    renderEmptyFolderBatchControls();
    renderFolderSortControls();
  }
}

function bindFolderDragSurface(surface, row, keyboardEnabled = false) {
  let activePointerId = null;

  const finishDrag = event => {
    if (activePointerId == null || (event.pointerId != null && event.pointerId !== activePointerId)) return;
    if (surface.hasPointerCapture && surface.hasPointerCapture(activePointerId)) {
      surface.releasePointerCapture(activePointerId);
    }
    activePointerId = null;
    row.classList.remove('folder-sort-dragging');
    row.setAttribute('aria-grabbed', 'false');
    updateFolderSortSaveState();
  };

  surface.addEventListener('pointerdown', event => {
    if (!folderSortMode || savingFolderOrder) return;
    if (event.pointerType === 'mouse') return;
    activePointerId = event.pointerId;
    if (surface.setPointerCapture) surface.setPointerCapture(activePointerId);
    row.classList.add('folder-sort-dragging');
    row.setAttribute('aria-grabbed', 'true');
    event.preventDefault();
    event.stopPropagation();
  });
  surface.addEventListener('pointermove', event => {
    if (activePointerId == null || event.pointerId !== activePointerId) return;
    const scrollEdge = 72;
    if (event.clientY < scrollEdge) window.scrollBy(0, -18);
    else if (event.clientY > window.innerHeight - scrollEdge) window.scrollBy(0, 18);
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-folder-id]');
    if (!target || target === row || target.parentElement !== row.parentElement) return;
    const rect = target.getBoundingClientRect();
    const before = event.clientY < rect.top + rect.height / 2;
    row.parentElement.insertBefore(row, before ? target : target.nextSibling);
    updateFolderSortSaveState();
    event.preventDefault();
  });
  surface.addEventListener('pointerup', finishDrag);
  surface.addEventListener('pointercancel', finishDrag);
  if (keyboardEnabled) {
    surface.addEventListener('keydown', event => {
      if (!folderSortMode || savingFolderOrder || !['ArrowUp', 'ArrowDown'].includes(event.key)) return;
      const sibling = event.key === 'ArrowUp' ? row.previousElementSibling : row.nextElementSibling;
      if (!sibling || !sibling.matches('[data-folder-id]')) return;
      if (event.key === 'ArrowUp') row.parentElement.insertBefore(row, sibling);
      else row.parentElement.insertBefore(sibling, row);
      updateFolderSortSaveState();
      event.preventDefault();
    });
  }
}

function bindFolderDragHandle(handle, row) {
  handle.style.touchAction = 'none';
  bindFolderDragSurface(handle, row, true);
  handle.addEventListener('click', event => event.stopPropagation());
}

function moveFolderRowAtPointer(draggedRow, targetRow, clientY) {
  if (!draggedRow || !targetRow || draggedRow === targetRow || targetRow.parentElement !== draggedRow.parentElement) {
    return;
  }
  const rect = targetRow.getBoundingClientRect();
  const before = clientY < rect.top + rect.height / 2;
  draggedRow.parentElement.insertBefore(draggedRow, before ? targetRow : targetRow.nextSibling);
  updateFolderSortSaveState();
}

function bindNativeFolderDrag(row) {
  row.addEventListener('dragstart', event => {
    if (!folderSortMode || savingFolderOrder) {
      event.preventDefault();
      return;
    }
    nativeDraggedFolderRow = row;
    row.classList.add('folder-sort-dragging');
    row.setAttribute('aria-grabbed', 'true');
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', row.getAttribute('data-folder-id') || '');
    }
  });
  row.addEventListener('dragover', event => {
    if (!folderSortMode || !nativeDraggedFolderRow) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    moveFolderRowAtPointer(nativeDraggedFolderRow, row, event.clientY);
  });
  row.addEventListener('drop', event => {
    if (!nativeDraggedFolderRow) return;
    event.preventDefault();
    moveFolderRowAtPointer(nativeDraggedFolderRow, row, event.clientY);
  });
  row.addEventListener('dragend', () => {
    if (nativeDraggedFolderRow) {
      nativeDraggedFolderRow.classList.remove('folder-sort-dragging');
      nativeDraggedFolderRow.setAttribute('aria-grabbed', 'false');
    }
    nativeDraggedFolderRow = null;
    updateFolderSortSaveState();
  });
}

async function deleteEmptyFolder(event, fid, title) {
  event.stopPropagation();
  if (!confirm(`确认删除空收藏夹“${title}”？此操作不可逆。`)) return;

  const button = $(`delete-empty-folder-${fid}`);
  const originalHtml = button ? button.innerHTML : '';
  if (button) {
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle" class="w-4 h-4 spin-slow"></i>';
    if (window.lucide) lucide.createIcons();
  }
  try {
    await api(`/api/folders/${fid}`, { method: 'DELETE' });
    selectedSourceFids.delete(Number(fid));
    selectedEmptyFolderFids.delete(Number(fid));
    emptyFolderCandidates.delete(Number(fid));
    const row = $(`select-folder-${fid}`);
    if (row) row.remove();
    renderEmptyFolderBatchControls();
    updateFolderSelectionUi();
  } catch (error) {
    if (button) {
      button.disabled = false;
      button.innerHTML = originalHtml;
      if (window.lucide) lucide.createIcons();
    }
    alert(error.message);
  }
}

function updateEmptyFolderSelectionUi() {
  emptyFolderCandidates.forEach((_, fid) => {
    const row = $(`select-folder-${fid}`);
    const checkbox = document.querySelector(`[data-empty-folder-select="${fid}"]`);
    const selected = selectedEmptyFolderFids.has(fid);
    if (checkbox) checkbox.checked = selected;
    if (row && emptyFolderSelectionMode) {
      row.style.borderColor = selected ? 'var(--state-error)' : 'var(--border)';
      row.style.background = selected ? 'var(--state-error-surface)' : 'var(--card)';
    }
  });
}

function toggleEmptyFolderSelection(fid, forceSelected = null) {
  const normalizedFid = Number(fid);
  if (!emptyFolderCandidates.has(normalizedFid) || deletingEmptyFolders) return;
  const selected = forceSelected == null
    ? !selectedEmptyFolderFids.has(normalizedFid)
    : Boolean(forceSelected);
  if (selected) selectedEmptyFolderFids.add(normalizedFid);
  else selectedEmptyFolderFids.delete(normalizedFid);
  renderEmptyFolderBatchControls();
}

function setEmptyFolderSelectionMode(enabled) {
  if (deletingEmptyFolders || folderSortMode) return;
  emptyFolderSelectionMode = Boolean(enabled);
  selectedEmptyFolderFids.clear();
  if (emptyFolderSelectionMode) selectedSourceFids.clear();
  updateFolderSelectionUi();
  renderEmptyFolderBatchControls();
}

function renderEmptyFolderBatchControls() {
  const bar = $('empty-folder-batch-bar');
  if (!bar) return;
  if (folderSortMode) {
    bar.style.display = 'none';
    return;
  }
  const candidateCount = emptyFolderCandidates.size;
  if (!candidateCount) {
    bar.style.display = 'none';
    bar.innerHTML = '';
    emptyFolderSelectionMode = false;
    selectedEmptyFolderFids.clear();
    return;
  }

  bar.style.display = 'block';
  if (!emptyFolderSelectionMode) {
    bar.innerHTML = `
      <div class="flex flex-wrap items-center justify-between gap-3 py-3" style="border-bottom:1px solid var(--border);">
        <span class="text-sm" style="color:var(--muted-foreground);">检测到 ${candidateCount} 个空收藏夹</span>
        <button data-dom-id="empty-folder-select-start" type="button" class="btn btn-secondary">
          <i data-lucide="list-checks" class="w-4 h-4"></i><span>批量选择空收藏夹并删除</span>
        </button>
      </div>`;
    $('empty-folder-select-start').onclick = () => setEmptyFolderSelectionMode(true);
  } else {
    const selectedCount = selectedEmptyFolderFids.size;
    bar.innerHTML = `
      <div class="flex flex-wrap items-center justify-between gap-3 p-3 rounded-lg" style="background:var(--background-100);border:1px solid var(--border);">
        <span class="text-sm font-medium tabular-nums" style="color:var(--foreground);">已选择 ${selectedCount}/${candidateCount} 个空收藏夹</span>
        <div class="flex flex-wrap items-center gap-2">
          <button data-dom-id="empty-folder-select-all" type="button" class="btn btn-text" ${deletingEmptyFolders ? 'disabled' : ''}>全选</button>
          <button data-dom-id="empty-folder-select-none" type="button" class="btn btn-text" ${deletingEmptyFolders ? 'disabled' : ''}>取消全选</button>
          <button data-dom-id="empty-folder-select-finish" type="button" class="btn btn-text" ${deletingEmptyFolders ? 'disabled' : ''}>完成</button>
          <button data-dom-id="empty-folder-delete-selected" type="button" class="btn btn-secondary" style="color:var(--state-error);" ${selectedCount && !deletingEmptyFolders ? '' : 'disabled'}>
            ${deletingEmptyFolders
              ? '<i data-lucide="loader-circle" class="w-4 h-4 spin-slow"></i><span>正在删除并确认</span>'
              : `<i data-lucide="trash-2" class="w-4 h-4"></i><span>删除选中（${selectedCount}）</span>`}
          </button>
        </div>
      </div>`;
    $('empty-folder-select-all').onclick = () => {
      selectedEmptyFolderFids.clear();
      emptyFolderCandidates.forEach((_, fid) => selectedEmptyFolderFids.add(fid));
      renderEmptyFolderBatchControls();
    };
    $('empty-folder-select-none').onclick = () => {
      selectedEmptyFolderFids.clear();
      renderEmptyFolderBatchControls();
    };
    $('empty-folder-select-finish').onclick = () => setEmptyFolderSelectionMode(false);
    $('empty-folder-delete-selected').onclick = deleteSelectedEmptyFolders;
  }

  document.querySelectorAll('[data-role="empty-folder-batch-select"]').forEach(el => {
    el.style.display = emptyFolderSelectionMode ? 'flex' : 'none';
  });
  document.querySelectorAll('[data-role="empty-folder-single-delete"]').forEach(el => {
    el.style.display = emptyFolderSelectionMode ? 'none' : 'inline-flex';
  });
  updateFolderSelectionUi();
  updateEmptyFolderSelectionUi();
  if (window.lucide) lucide.createIcons();
}

async function deleteSelectedEmptyFolders() {
  const fids = [...selectedEmptyFolderFids];
  if (!fids.length || deletingEmptyFolders) return;
  if (!confirm(`确认批量删除 ${fids.length} 个空收藏夹？此操作不可逆。`)) return;

  deletingEmptyFolders = true;
  renderEmptyFolderBatchControls();
  try {
    const result = await api('/api/folders/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ fids }),
    });
    (result.deleted_fids || []).forEach(fid => {
      const normalizedFid = Number(fid);
      selectedSourceFids.delete(normalizedFid);
      selectedEmptyFolderFids.delete(normalizedFid);
      emptyFolderCandidates.delete(normalizedFid);
      const row = $(`select-folder-${normalizedFid}`);
      if (row) row.remove();
    });
    const failedCount = Number(result.stats && result.stats.failed || 0);
    if (failedCount) {
      alert(`已删除 ${result.stats.success} 个，${failedCount} 个尚未得到 B 站确认，已保留供重试。`);
    } else {
      emptyFolderSelectionMode = false;
      selectedEmptyFolderFids.clear();
    }
  } catch (error) {
    alert(error.message);
  } finally {
    deletingEmptyFolders = false;
    renderEmptyFolderBatchControls();
    updateFolderSelectionUi();
  }
}

function cleanupIsRunning() {
  return cleanupState.scan && ['queued', 'scanning', 'removing'].includes(cleanupState.scan.status);
}

function cleanupVisibleItems() {
  return cleanupState.items.filter(item => (
    cleanupState.filter === 'all' || item.problem_type === cleanupState.filter
  ));
}

function setCleanupData(data, defaultSelect = false) {
  const previousScanId = cleanupState.scan && cleanupState.scan.scan_id;
  cleanupState.scan = data.scan || null;
  cleanupState.items = data.items || [];
  const scanChanged = cleanupState.scan && cleanupState.scan.scan_id !== previousScanId;
  if (defaultSelect || scanChanged) {
    cleanupState.selected = new Set(
      cleanupState.items.filter(item => !item.removed).map(item => Number(item.id)),
    );
    cleanupState.collapsedFolders.clear();
    cleanupState.filter = 'all';
  } else {
    const selectableIds = new Set(cleanupState.items.filter(item => !item.removed).map(item => Number(item.id)));
    cleanupState.selected = new Set([...cleanupState.selected].filter(id => selectableIds.has(id)));
  }
}

function updateCleanupProgress(event = {}) {
  const scan = cleanupState.scan || {};
  const stage = event.stage || scan.status || 'queued';
  const isRemoval = stage === 'removing' || scan.status === 'removing';
  const rawProgress = event.progress != null
    ? Number(event.progress)
    : (scan.folders_total ? Number(scan.folders_scanned || 0) / Number(scan.folders_total) : 0);
  const percent = Math.max(0, Math.min(100, Math.round(rawProgress <= 1 ? rawProgress * 100 : rawProgress)));
  const statusLabels = {
    queued: '\u6b63\u5728\u51c6\u5907\u626b\u63cf',
    scanning: event.current_folder_title
      ? `\u6b63\u5728\u68c0\u67e5\uff1a${event.current_folder_title}`
      : '\u6b63\u5728\u68c0\u67e5\u6536\u85cf\u5939',
    ready: '\u626b\u63cf\u5b8c\u6210',
    removing: event.current_folder_title
      ? `\u6b63\u5728\u5220\u9664\uff1a${event.current_folder_title}`
      : '\u6b63\u5728\u5220\u9664\u9009\u4e2d\u8d44\u6e90',
    completed: '\u5220\u9664\u5904\u7406\u5b8c\u6210',
    failed: '\u4efb\u52a1\u5931\u8d25',
    cancelled: '\u4efb\u52a1\u5df2\u53d6\u6d88',
  };
  $('cleanup-progress-status').textContent = statusLabels[stage] || '\u6b63\u5728\u5904\u7406';
  $('cleanup-progress-percent').textContent = `${percent}%`;
  $('cleanup-progress-bar').style.width = `${percent}%`;
  $('cleanup-folders').textContent = `${event.folders_scanned ?? scan.folders_scanned ?? 0}/${event.folders_total ?? scan.folders_total ?? 0}`;
  $('cleanup-resources').textContent = event.resources_scanned ?? scan.resources_scanned ?? 0;
  $('cleanup-problems').textContent = event.problem_total ?? scan.problem_total ?? cleanupState.items.length;
  $('cleanup-cancel').style.display = ['queued', 'scanning'].includes(stage) ? 'inline-flex' : 'none';
  $('cleanup-rescan').disabled = cleanupIsRunning();
  if (isRemoval) {
    const processed = Number(event.processed || 0);
    const total = Number(event.total || 0);
    $('cleanup-remove-status').textContent = total
      ? `\u5df2\u5904\u7406 ${processed}/${total}\uff0c\u6210\u529f ${Number(event.success || 0)}\uff0c\u5931\u8d25 ${Number(event.failed || 0)}`
      : '\u6b63\u5728\u51c6\u5907\u5220\u9664';
  }
}

function renderCleanupResults() {
  const counts = {
    all: cleanupState.items.length,
    invalid: cleanupState.items.filter(item => item.problem_type === 'invalid').length,
    inaccessible: cleanupState.items.filter(item => item.problem_type === 'inaccessible').length,
  };
  const chips = [
    ['all', '\u5168\u90e8'],
    ['invalid', '\u5df2\u5931\u6548'],
    ['inaccessible', '\u65e0\u6cd5\u8bbf\u95ee'],
  ];
  $('cleanup-filter-bar').innerHTML = chips.map(([key, label]) => `
    <button data-dom-id="cleanup-filter-${key}" type="button" class="btn ${cleanupState.filter === key ? 'btn-primary' : 'btn-secondary'}" style="height:32px;padding:0 12px;font-size:12px;">
      <span>${label}</span><span class="tabular-nums">${counts[key]}</span>
    </button>`).join('');
  chips.forEach(([key]) => {
    $(`cleanup-filter-${key}`).onclick = () => {
      cleanupState.filter = key;
      renderCleanupResults();
    };
  });

  const groups = new Map();
  cleanupVisibleItems().forEach(item => {
    const fid = Number(item.source_fid);
    if (!groups.has(fid)) groups.set(fid, { title: item.source_title || String(fid), items: [] });
    groups.get(fid).items.push(item);
  });
  $('cleanup-list').innerHTML = [...groups.entries()].map(([fid, group]) => {
    const collapsed = cleanupState.collapsedFolders.has(fid);
    const rows = collapsed ? '' : group.items.map(item => {
      const removed = Boolean(item.removed);
      const checked = cleanupState.selected.has(Number(item.id));
      const title = item.title || item.bvid || `\u8d44\u6e90 ID ${item.resource_id}`;
      const label = item.problem_type === 'invalid' ? '\u5df2\u5931\u6548' : '\u65e0\u6cd5\u8bbf\u95ee';
      const badgeStyle = item.problem_type === 'invalid'
        ? 'background:var(--state-error-surface);color:var(--state-error);'
        : 'background:var(--background-200);color:var(--muted-foreground);';
      return `<label class="flex items-start gap-3 p-3 rounded-lg" style="background:var(--card);border:1px solid var(--border);${removed ? 'opacity:.64;' : ''}">
        <input data-cleanup-item-id="${item.id}" type="checkbox" class="mt-1 shrink-0" ${checked ? 'checked' : ''} ${removed ? 'disabled' : ''}>
        <span class="flex-1 min-w-0">
          <span class="block text-sm font-medium" style="color:var(--foreground);overflow-wrap:anywhere;">${escapeHtml(title)}</span>
          <span class="mt-1 flex flex-wrap items-center gap-2 text-xs" style="color:var(--muted-foreground);">
            <span class="inline-flex items-center h-5 px-2 rounded-full" style="${badgeStyle}">${label}</span>
            <span>ID ${escapeHtml(item.resource_id)} \u00b7 ${escapeHtml(item.resource_type || 2)}</span>
            ${removed ? '<span style="color:var(--state-success);">\u5df2\u5220\u9664</span>' : ''}
          </span>
          ${item.remove_error ? `<span class="block mt-1 text-xs" style="color:var(--state-error);">${escapeHtml(item.remove_error)}</span>` : ''}
        </span>
      </label>`;
    }).join('');
    return `<section class="rounded-lg overflow-hidden" style="background:var(--background-100);border:1px solid var(--border);">
      <button data-dom-id="cleanup-folder-${fid}" type="button" class="w-full flex items-center gap-2 p-4 text-left" style="background:transparent;border:0;cursor:pointer;color:var(--foreground);">
        <i data-lucide="${collapsed ? 'chevron-right' : 'chevron-down'}" class="w-4 h-4"></i>
        <span class="flex-1 min-w-0 truncate text-sm font-semibold">${escapeHtml(group.title)}</span>
        <span class="text-xs tabular-nums" style="color:var(--muted-foreground);">${group.items.length}</span>
      </button>
      <div class="px-3 pb-3 flex flex-col gap-2">${rows}</div>
    </section>`;
  }).join('') || `<div class="py-12 text-center text-sm" style="color:var(--muted-foreground);">${cleanupState.items.length ? '\u5f53\u524d\u7b5b\u9009\u4e0b\u6ca1\u6709\u8d44\u6e90' : '\u672a\u53d1\u73b0\u5931\u6548\u6216\u65e0\u6cd5\u8bbf\u95ee\u7684\u8d44\u6e90'}</div>`;

  groups.forEach((_, fid) => {
    $(`cleanup-folder-${fid}`).onclick = () => {
      if (cleanupState.collapsedFolders.has(fid)) cleanupState.collapsedFolders.delete(fid);
      else cleanupState.collapsedFolders.add(fid);
      renderCleanupResults();
    };
  });
  document.querySelectorAll('[data-cleanup-item-id]').forEach(input => {
    input.onchange = () => {
      const id = Number(input.getAttribute('data-cleanup-item-id'));
      if (input.checked) cleanupState.selected.add(id);
      else cleanupState.selected.delete(id);
      updateCleanupSelectionUi();
    };
  });
  updateCleanupSelectionUi();
  if (window.lucide) lucide.createIcons();
}

function updateCleanupSelectionUi() {
  const selectedItems = cleanupState.items.filter(item => cleanupState.selected.has(Number(item.id)) && !item.removed);
  $('cleanup-selection-summary').textContent = `\u5df2\u9009\u62e9 ${selectedItems.length} \u9879`;
  $('cleanup-remove').disabled = !selectedItems.length || cleanupIsRunning();
  $('cleanup-remove').querySelector('span').textContent = `\u5220\u9664\u9009\u4e2d\u9879${selectedItems.length ? ` (${selectedItems.length})` : ''}`;
}

function renderCleanupPage() {
  const scan = cleanupState.scan;
  const statusLabel = scan ? ({
    queued: '\u7b49\u5f85\u626b\u63cf', scanning: '\u626b\u63cf\u4e2d', ready: '\u626b\u63cf\u5b8c\u6210',
    removing: '\u5220\u9664\u4e2d', completed: '\u5904\u7406\u5b8c\u6210', failed: '\u5931\u8d25', cancelled: '\u5df2\u53d6\u6d88',
  }[scan.status] || scan.status) : '';
  $('cleanup-summary').textContent = scan
    ? `\u5f53\u524d\u8d26\u53f7\u7684\u5168\u90e8\u6536\u85cf\u5939 \u00b7 ${statusLabel}`
    : '\u68c0\u67e5\u5f53\u524d\u8d26\u53f7\u7684\u5168\u90e8\u6536\u85cf\u5939';
  $('cleanup-error').style.display = scan && scan.error ? 'block' : 'none';
  $('cleanup-error').innerHTML = scan && scan.error
    ? `<div class="p-3 rounded-lg text-sm" style="background:var(--state-error-surface);color:var(--state-error);">${escapeHtml(scan.error)}</div>`
    : '';
  updateCleanupProgress(scan || {});
  renderCleanupResults();
}

async function loadCleanupScan(scanId, defaultSelect = false) {
  const data = await api(`/api/cleanup/scans/${scanId}`);
  setCleanupData(data, defaultSelect);
  renderCleanupPage();
  return data;
}

function startCleanupStream(scanId) {
  const es = replaceEventSource('cleanup', new EventSource(`/api/cleanup/scans/${scanId}/stream`));
  es.addEventListener('stage', event => {
    const data = JSON.parse(event.data);
    if (cleanupState.scan) cleanupState.scan.status = data.stage;
    updateCleanupProgress(data);
    updateCleanupSelectionUi();
  });
  es.addEventListener('done', async () => {
    closeEventSource('cleanup', es);
    try {
      await loadCleanupScan(scanId);
    } catch (error) {
      showCleanupError(error.message);
    }
  });
  const handleEnd = event => {
    const data = JSON.parse(event.data);
    closeEventSource('cleanup', es);
    if (cleanupState.scan) cleanupState.scan.status = 'failed';
    updateCleanupProgress({ stage: 'failed', progress: 0 });
    updateCleanupSelectionUi();
    showCleanupError(data.message || '\u6e05\u7406\u4efb\u52a1\u5931\u8d25');
  };
  es.addEventListener('failed', handleEnd);
  es.addEventListener('cancelled', event => {
    closeEventSource('cleanup', es);
    if (cleanupState.scan) cleanupState.scan.status = 'cancelled';
    updateCleanupProgress({ stage: 'cancelled', progress: 0 });
    showCleanupError(JSON.parse(event.data).message || '\u626b\u63cf\u5df2\u53d6\u6d88');
  });
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED && eventSources.cleanup === es) {
      showCleanupError('\u8fdb\u5ea6\u8fde\u63a5\u5df2\u5173\u95ed\uff0c\u8bf7\u8fd4\u56de\u9996\u9875\u540e\u91cd\u65b0\u8fdb\u5165');
    }
  };
}

function showCleanupError(message) {
  $('cleanup-error').style.display = 'block';
  $('cleanup-error').innerHTML = `<div class="p-3 rounded-lg text-sm" style="background:var(--state-error-surface);color:var(--state-error);">${escapeHtml(message)}</div>`;
}

async function startCleanupScan() {
  $('cleanup-error').style.display = 'none';
  $('cleanup-rescan').disabled = true;
  cleanupState.scan = { status: 'queued', folders_total: 0, folders_scanned: 0, resources_scanned: 0, problem_total: 0 };
  cleanupState.items = [];
  cleanupState.selected.clear();
  renderCleanupPage();
  try {
    const job = await api('/api/cleanup/scans', { method: 'POST' });
    cleanupState.scan.scan_id = job.scan_id;
    startCleanupStream(job.scan_id);
  } catch (error) {
    cleanupState.scan.status = 'failed';
    renderCleanupPage();
    showCleanupError(error.message);
  }
}

async function removeCleanupSelected() {
  const scan = cleanupState.scan;
  if (!scan) return;
  const selectedItems = cleanupState.items.filter(item => cleanupState.selected.has(Number(item.id)) && !item.removed);
  if (!selectedItems.length) return;
  const folderCount = new Set(selectedItems.map(item => Number(item.source_fid))).size;
  if (!confirm(`\u5c06\u4ece ${folderCount} \u4e2a\u6536\u85cf\u5939\u4e2d\u5220\u9664 ${selectedItems.length} \u4e2a\u8d44\u6e90\u4f4d\u7f6e\uff0c\u6b64\u64cd\u4f5c\u4e0d\u53ef\u9006\u3002\u662f\u5426\u7ee7\u7eed\uff1f`)) return;
  try {
    const result = await api(`/api/cleanup/scans/${scan.scan_id}/remove`, {
      method: 'POST',
      body: JSON.stringify({ item_ids: selectedItems.map(item => Number(item.id)) }),
    });
    scan.status = 'removing';
    updateCleanupProgress({ stage: 'removing', processed: 0, total: selectedItems.length, progress: 0 });
    updateCleanupSelectionUi();
    startCleanupStream(result.scan_id);
  } catch (error) {
    showCleanupError(error.message);
  }
}

async function openCleanup() {
  showView('cleanup');
  $('cleanup-back').onclick = () => { showView('home'); renderHome(); };
  $('cleanup-rescan').onclick = startCleanupScan;
  $('cleanup-select-all').onclick = () => {
    cleanupState.selected = new Set(cleanupState.items.filter(item => !item.removed).map(item => Number(item.id)));
    renderCleanupResults();
  };
  $('cleanup-select-none').onclick = () => {
    cleanupState.selected.clear();
    renderCleanupResults();
  };
  $('cleanup-remove').onclick = removeCleanupSelected;
  $('cleanup-cancel').onclick = async () => {
    if (!cleanupState.scan) return;
    $('cleanup-cancel').disabled = true;
    try {
      await api(`/api/cleanup/scans/${cleanupState.scan.scan_id}/cancel`, { method: 'POST' });
    } catch (error) {
      showCleanupError(error.message);
    }
  };
  try {
    const latest = await api('/api/cleanup/scans/latest');
    if (!latest.scan) {
      await startCleanupScan();
      return;
    }
    setCleanupData(latest, true);
    renderCleanupPage();
    if (cleanupIsRunning()) startCleanupStream(latest.scan.scan_id);
  } catch (error) {
    showCleanupError(error.message);
  }
}

const RESOURCE_TYPE_LABELS = {
  2: '视频',
  7: '直播',
  11: '合集',
  12: '音频',
  17: '剧集',
  19: '番剧',
  21: '图文专栏',
};

function folderResourceKey(item) {
  return `${Number(item.resource_id || 0)}:${Number(item.resource_type || 2)}`;
}

async function openFolderResources(fid, title, declaredCount) {
  folderResourceState = {
    fid: Number(fid),
    title: String(title || '收藏夹资源'),
    declaredCount: Number(declaredCount || 0),
    total: Number(declaredCount || 0),
    page: 0,
    hasMore: true,
    loading: false,
    items: [],
    seenKeys: new Set(),
    allResourceIds: [],
  };
  showView('folder-resources');
  $('folder-resources-title').textContent = folderResourceState.title;
  $('folder-resources-summary').textContent = `共 ${folderResourceState.declaredCount} 个收藏条目`;
  $('folder-resources-list').innerHTML = '';
  $('folder-resources-error').style.display = 'none';
  $('folder-resources-back').onclick = () => {
    showView('home');
    updateFolderSelectionUi();
  };
  $('folder-resources-load-more').onclick = () => loadFolderResourcePage();
  await loadFolderResourcePage();
}

function appendInaccessibleResources() {
  if (!folderResourceState) return;
  folderResourceState.allResourceIds.forEach(item => {
    const key = folderResourceKey(item);
    if (folderResourceState.seenKeys.has(key)) return;
    folderResourceState.seenKeys.add(key);
    folderResourceState.items.push({
      resource_id: item.resource_id,
      resource_type: item.resource_type || 2,
      bvid: item.bvid || '',
      title: '',
      up_name: '',
      cover_url: '',
      tname: '',
      status: 'inaccessible',
      status_label: '无法访问',
    });
  });
}

function renderFolderResourceList() {
  if (!folderResourceState) return;
  const state = folderResourceState;
  $('folder-resources-summary').textContent = `已展示 ${state.items.length} 个，B站记录 ${state.total || state.declaredCount} 个`;
  $('folder-resources-list').innerHTML = state.items.map(item => {
    const typeLabel = RESOURCE_TYPE_LABELS[Number(item.resource_type)] || '其他';
    const fallbackTitle = item.bvid
      ? `无法访问的资源（${item.bvid}）`
      : `无法访问的资源（ID：${item.resource_id || '未知'}）`;
    const title = item.title || fallbackTitle;
    const isProblem = item.status === 'invalid' || item.status === 'inaccessible';
    const statusColor = item.status === 'invalid' ? 'var(--state-error)' : (item.status === 'inaccessible' ? 'var(--muted-foreground)' : 'var(--state-success)');
    const statusBg = item.status === 'invalid' ? 'var(--state-error-surface)' : (item.status === 'inaccessible' ? 'var(--background-200)' : 'var(--state-success-surface)');
    const media = item.cover_url
      ? `<img src="${escapeHtml(item.cover_url)}" alt="" referrerpolicy="no-referrer" class="w-full h-full object-cover" onerror="this.style.display='none';if(this.nextElementSibling)this.nextElementSibling.style.display='block';"><i data-lucide="file-video" class="w-5 h-5" style="display:none;color:var(--icon-muted);"></i>`
      : `<i data-lucide="${isProblem ? 'file-question' : 'play'}" class="w-5 h-5" style="color:var(--icon-muted);"></i>`;
    return `<article class="flex items-center gap-3 p-3 rounded-xl" style="background:var(--card);border:1px solid var(--border);box-shadow:var(--shadow-xs);">
      <div class="shrink-0 w-20 h-12 rounded-lg overflow-hidden flex items-center justify-center" style="background:var(--background-200);">${media}</div>
      <div class="flex-1 min-w-0">
        <p class="text-sm font-medium line-clamp-2" style="color:var(--foreground);">${escapeHtml(title)}</p>
        <p class="mt-1 text-xs truncate" style="color:var(--muted-foreground);">${escapeHtml(item.up_name || item.bvid || `资源 ID ${item.resource_id || ''}`)}</p>
      </div>
      <div class="shrink-0 flex flex-col items-end gap-1.5">
        <span class="text-xs" style="color:var(--muted-foreground);">${typeLabel}</span>
        <span class="inline-flex items-center h-5 px-2 rounded-full text-xs" style="background:${statusBg};color:${statusColor};">${escapeHtml(item.status_label || '可访问')}</span>
      </div>
    </article>`;
  }).join('') || '<p class="py-12 text-center text-sm" style="color:var(--muted-foreground);">这个收藏夹暂无可展示资源</p>';

  const loadMore = $('folder-resources-load-more');
  loadMore.style.display = state.hasMore ? 'inline-flex' : 'none';
  loadMore.disabled = state.loading;
  loadMore.innerHTML = state.loading
    ? '<i data-lucide="loader-circle" class="w-4 h-4 spin-slow"></i><span>加载中</span>'
    : '<i data-lucide="chevrons-down" class="w-4 h-4"></i><span>加载更多</span>';
  if (window.lucide) lucide.createIcons();
}

async function loadFolderResourcePage() {
  if (!folderResourceState || folderResourceState.loading || !folderResourceState.hasMore) return;
  const state = folderResourceState;
  const nextPage = state.page + 1;
  state.loading = true;
  $('folder-resources-error').style.display = 'none';
  renderFolderResourceList();
  try {
    const data = await api(`/api/folders/${folderResourceState.fid}/resources?page=${nextPage}&page_size=20`);
    if (nextPage === 1) {
      state.allResourceIds = data.resource_ids || [];
      state.title = data.folder && data.folder.title ? data.folder.title : state.title;
      state.declaredCount = Number(data.folder && data.folder.media_count || state.declaredCount);
      $('folder-resources-title').textContent = state.title;
    }
    (data.items || []).forEach(item => {
      const key = folderResourceKey(item);
      if (item.resource_id && state.seenKeys.has(key)) return;
      if (item.resource_id) state.seenKeys.add(key);
      state.items.push(item);
    });
    state.page = Number(data.page || nextPage);
    state.total = Number(data.total || state.declaredCount);
    state.hasMore = Boolean(data.has_more);
    if (!state.hasMore) appendInaccessibleResources();
  } catch (error) {
    $('folder-resources-error').style.display = 'block';
    $('folder-resources-error').innerHTML = `<div class="flex items-center justify-between gap-3 p-3 rounded-lg" style="background:var(--state-error-surface);color:var(--state-error);">
      <span class="text-sm">${escapeHtml(error.message)}</span>
      <button data-dom-id="folder-resources-retry" type="button" class="btn btn-text"><i data-lucide="refresh-cw" class="w-4 h-4"></i><span>重试</span></button>
    </div>`;
    $('folder-resources-retry').onclick = () => loadFolderResourcePage();
  } finally {
    state.loading = false;
    renderFolderResourceList();
  }
}

function toggleFolderSelection(fid) {
  if (selectedSourceFids.has(fid)) selectedSourceFids.delete(fid);
  else selectedSourceFids.add(fid);
  updateFolderSelectionUi();
}

function updateFolderSelectionUi() {
  document.querySelectorAll('[data-folder-id]').forEach(el => {
    const fid = Number(el.getAttribute('data-folder-id'));
    const selected = selectedSourceFids.has(fid);
    el.style.borderColor = selected ? 'var(--brand-500)' : 'var(--border)';
    el.style.background = selected ? 'var(--brand-50)' : 'var(--card)';
    const check = el.querySelector('[data-role="folder-check"]');
    if (check) check.style.display = selected ? 'flex' : 'none';
  });
  const btn = $('start-organize');
  if (!btn) return;
  if (folderSortMode) {
    btn.disabled = true;
    btn.querySelector('span').textContent = '请先保存或取消排序';
    return;
  }
  if (emptyFolderSelectionMode) {
    btn.disabled = true;
    btn.querySelector('span').textContent = '请先完成空收藏夹选择';
    return;
  }
  const count = selectedSourceFids.size;
  btn.disabled = count === 0;
  btn.querySelector('span').textContent = count ? `开始智能整理（${count} 个收藏夹）` : '开始智能整理';
}

function normalizeCategoryLimit(value) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return 14;
  return Math.max(3, Math.min(50, parsed));
}

function updateCustomCategoryLimit(writeBack = false) {
  const input = $('granularity-custom-limit');
  categoryLimit = normalizeCategoryLimit(input.value);
  if (writeBack) input.value = categoryLimit;
  $('category-limit-summary').textContent = `最多 ${categoryLimit} 个分类`;
}

function setCategoryGranularity(name) {
  categoryGranularity = name;
  if (name === 'custom') {
    $('granularity-custom-panel').style.display = 'flex';
    updateCustomCategoryLimit(true);
  } else {
    $('granularity-custom-panel').style.display = 'none';
    categoryLimit = CATEGORY_LIMIT_PRESETS[name] || CATEGORY_LIMIT_PRESETS.balanced;
    $('category-limit-summary').textContent = `最多 ${categoryLimit} 个分类`;
  }
  ['coarse', 'balanced', 'detailed', 'custom'].forEach(key => {
    const button = $(`granularity-${key}`);
    const active = key === name;
    button.style.background = active ? 'var(--card)' : 'transparent';
    button.style.color = active ? 'var(--foreground)' : 'var(--muted-foreground)';
    button.style.boxShadow = active ? 'var(--shadow-sm)' : 'none';
  });
}

function setMode(mode) {
  currentMode = mode;
  const quick = $('mode-quick'), full = $('mode-full');
  if (mode === 'quick') {
    quick.style.background = 'var(--background-50)'; quick.style.color = 'var(--foreground)'; quick.style.boxShadow = 'var(--shadow-sm)';
    full.style.background = 'transparent'; full.style.color = 'var(--muted-foreground)'; full.style.boxShadow = 'none';
  } else {
    full.style.background = 'var(--background-50)'; full.style.color = 'var(--foreground)'; full.style.boxShadow = 'var(--shadow-sm)';
    quick.style.background = 'transparent'; quick.style.color = 'var(--muted-foreground)'; quick.style.boxShadow = 'none';
  }
}

async function newSession(sourceFids) {
  if (categoryGranularity === 'custom') updateCustomCategoryLimit(true);
  const r = await api('/api/session', {
    method: 'POST',
    body: JSON.stringify({ source_fids: sourceFids, mode: currentMode, category_limit: categoryLimit }),
  });
  currentSid = r.session_id;
  showView('progress');
  runPipeline(r.session_id);
}

async function openSession(sid) {
  currentSid = sid;
  const plan = await api(`/api/session/${sid}`);
  renderReview(sid, plan);
}

function runPipeline(sid, { reset = true } = {}) {
  if (reset) {
    $('progress-percent').textContent = '0%';
    $('progress-bar').style.width = '0%';
    $('progress-status').textContent = '准备中...';
    $('progress-stats').style.display = 'none';
    renderSteps('collecting');
  } else {
    $('progress-status').textContent = '正在恢复整理进度...';
  }

  const es = replaceEventSource('pipeline', new EventSource(`/api/session/${sid}/stream`));
  es.addEventListener('stage', e => {
    const d = JSON.parse(e.data);
    updateProgress(d);
  });
  es.addEventListener('done', () => {
    closeEventSource('pipeline', es);
    openSession(sid);
  });
  es.addEventListener('fail', e => {
    closeEventSource('pipeline', es);
    const d = JSON.parse(e.data);
    if (d.code === 'CANCELLED') {
      showView('home');
      renderHome();
      return;
    }
    if (d.code === 'AI_AUTH_FAILED' || d.code === 'AI_NOT_CONFIGURED') {
      showView('config');
      loadConfig();
      return;
    }
    if (d.code === 'NOT_LOGGED_IN') {
      showView('login');
      renderLogin();
      return;
    }
    alert(d.message || '处理失败');
    showView('home');
    renderHome();
  });
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) return;
    closeEventSource('pipeline', es);
  };
  $('progress-back-home').onclick = () => {
    closeEventSource('pipeline', es);
    showView('home');
    renderHome();
  };
  $('progress-cancel').onclick = async () => {
    if (!confirm('确认取消本次整理？已分类的进度将丢弃。')) return;
    try {
      await api(`/api/session/${sid}/cancel`, { method: 'POST' });
      closeEventSource('pipeline', es);
      showView('home');
      renderHome();
    } catch (e) { alert(e.message); }
  };
}

function deriveProgressPercent(d) {
  if (typeof d.progress === 'number') return Math.max(0, Math.min(100, Math.round(d.progress * 100)));
  if (d.source_total && d.scanned != null) return Math.max(0, Math.min(99, Math.round((d.scanned / d.source_total) * 100)));
  return null;
}

function updateProgress(d) {
  if (d.stage === 'collecting') {
    renderSteps('collecting');
    if (d.source_total != null || d.scanned != null || d.skipped != null) {
      $('progress-stats').style.display = 'grid';
      $('progress-stats').innerHTML = renderStatsGrid(d.source_total, d.scanned, d.collected, d.skipped);
    }
    const pct = deriveProgressPercent(d);
    if (pct != null) {
      $('progress-percent').textContent = pct + '%';
      $('progress-bar').style.width = pct + '%';
      $('progress-status').textContent = d.collected != null ? `已获取 ${d.collected} 个可整理条目` : '正在拉取条目';
    }
  } else if (d.stage === 'classifying') {
    renderSteps('classifying');
    if (d.source_total != null || d.skipped != null) {
      $('progress-stats').style.display = 'grid';
      $('progress-stats').innerHTML = renderStatsGrid(d.source_total, null, d.total, d.skipped);
    }
    const pct = deriveProgressPercent(d);
    if (pct != null) {
      $('progress-percent').textContent = pct + '%';
      $('progress-bar').style.width = pct + '%';
      $('progress-status').textContent = d.total != null ? `共 ${d.total} 个可整理条目，AI 分析中` : 'AI 分类中';
    }
  } else if (d.stage === 'pending_review') {
    renderSteps('pending_review');
  }
}

function renderStatsGrid(sourceTotal, scanned, collected, skipped) {
  const cells = [
    { label: '收藏条目', value: sourceTotal, icon: 'layers' },
    { label: '已扫描', value: scanned, icon: 'search' },
    { label: '可整理', value: collected, icon: 'check-circle' },
    { label: '已跳过', value: skipped, icon: 'minus-circle' },
  ];
  return cells.map(c => `
    <div class="flex flex-col items-center text-center p-3 rounded-lg" style="background: var(--background-100);">
      <i data-lucide="${c.icon}" class="w-4 h-4" style="color: var(--muted-foreground);"></i>
      <span class="mt-1.5 text-lg font-bold tabular-nums" style="color: var(--foreground);">${c.value != null ? escapeHtml(c.value) : '-'}</span>
      <span class="text-xs" style="color: var(--muted-foreground);">${c.label}</span>
    </div>`).join('');
}

function renderSteps(active) {
  const steps = [
    { key: 'collecting', label: '拉取条目', icon: 'check' },
    { key: 'classifying', label: 'AI 分类', icon: 'loader' },
    { key: 'pending_review', label: '预览方案', icon: 'clock' },
  ];
  const activeIdx = steps.findIndex(s => s.key === active);
  const colorSuccess = 'var(--state-success)', colorBrand = 'var(--brand-500)', colorMuted = 'var(--muted-foreground)';
  $('step-indicator').innerHTML = steps.map((s, i) => {
    const state = i < activeIdx ? 'done' : (i === activeIdx ? 'active' : 'pending');
    const bg = state === 'done' ? colorSuccess : (state === 'active' ? colorBrand : 'var(--background-200)');
    const fg = state === 'done' ? 'var(--state-success-foreground)' : (state === 'active' ? 'var(--primary-foreground)' : colorMuted);
    const labelColor = state === 'pending' ? colorMuted : 'var(--foreground)';
    const icon = state === 'done' ? '<i data-lucide="check" class="w-5 h-5"></i>' :
                 state === 'active' ? '<span class="progress-pulse absolute inset-0 rounded-full" style="background:var(--brand-500);animation:pulse-ring 1.8s cubic-bezier(.4,0,.6,1) infinite;"></span><span class="relative w-2.5 h-2.5 rounded-full" style="background:var(--primary-foreground);"></span>' :
                 '<i data-lucide="clock" class="w-5 h-5"></i>';
    const lineColor = i < activeIdx ? colorSuccess : (i === activeIdx ? colorBrand : 'var(--background-300)');
    const line = i < steps.length - 1 ? `<div class="flex-1 h-0.5 mt-5" style="background:${lineColor};"></div>` : '';
    return `<div class="flex flex-col items-center shrink-0" style="width:104px;">
      <div class="relative w-10 h-10 rounded-full flex items-center justify-center" style="background:${bg};${state==='pending'?'border:1px solid var(--border);':''}color:${fg};">${icon}</div>
      <p class="mt-3 text-xs font-medium text-center" style="color:${labelColor};">${s.label}</p>
    </div>${line}`;
  }).join('');
  if (window.lucide) lucide.createIcons();
}

function escapeDomId(value) {
  return String(value).replace(/[^a-zA-Z0-9_-]/g, ch => ch.charCodeAt(0).toString(16));
}

function renderVersionBar(sid, versions) {
  if (!versions || !versions.length) {
    $('review-version-bar').innerHTML = '';
    return;
  }
  $('review-version-bar').innerHTML = versions.map(v => `
    <button data-dom-id="plan-version-${escapeDomId(v.version_id)}" type="button"
      class="btn ${v.is_active ? 'btn-primary' : 'btn-secondary'}"
      style="height:32px;padding:0 12px;font-size:12px;">
      方案 ${v.version_no}
    </button>`).join('');
  versions.forEach(v => {
    $(`plan-version-${escapeDomId(v.version_id)}`).onclick = async () => {
      const plan = await api(`/api/session/${sid}/versions/${v.version_id}/activate`, { method: 'POST' });
      renderReview(sid, plan);
    };
  });
}

function renderRefinePanelLegacy(sid) {
  $('review-refine-panel').innerHTML = `
    <div class="flex flex-col sm:flex-row gap-2">
      <div class="field flex-1">
        <i data-lucide="sparkles" class="w-4 h-4"></i>
        <input data-dom-id="refine-instruction" class="control" type="text" placeholder="例如：把官方的作品单独放在一个收藏夹内">
      </div>
      <button data-dom-id="refine-submit" type="button" class="btn btn-primary">
        <i data-lucide="wand-sparkles" class="w-4 h-4"></i><span>生成新方案</span>
      </button>
    </div>`;
  $('refine-submit').onclick = async () => {
    const instruction = $('refine-instruction').value.trim();
    if (!instruction) return;
    $('refine-submit').disabled = true;
    try {
      const plan = await api(`/api/session/${sid}/refine`, {
        method: 'POST',
        body: JSON.stringify({ instruction }),
      });
      renderReview(sid, plan);
    } catch (e) {
      alert(e.message);
    } finally {
      $('refine-submit').disabled = false;
    }
  };
}

function refineStageLabel(stage, kind) {
  if (kind === 'unclassified_retry' && stage === 'analyzing') return '\u6b63\u5728\u51c6\u5907\u91cd\u8bd5\u672a\u5206\u7c7b\u6761\u76ee';
  return {
    analyzing: '\u6b63\u5728\u5206\u6790\u5fae\u8c03\u8981\u6c42',
    refining: '\u6b63\u5728\u751f\u6210\u65b0\u65b9\u6848',
    merging: '\u6b63\u5728\u5408\u5e76\u4e0e\u5f52\u5e76\u5206\u7c7b',
    saving: '\u6b63\u5728\u4fdd\u5b58\u65b9\u6848',
  }[stage] || '\u6b63\u5728\u5904\u7406';
}

function renderRefinePanel(sid, errorMessage = '') {
  const planItems = (window.__lastReviewPlan && window.__lastReviewPlan.items) || [];
  const unclassifiedCount = planItems.filter(item => item.category === '\u672a\u5206\u7c7b').length;
  const noticeHtml = errorMessage
    ? `<div class="mt-3 p-3 rounded-lg text-sm" style="background:var(--state-error-surface);color:var(--state-error);">${escapeHtml(errorMessage)}</div>`
    : (refineNotice
      ? `<div class="mt-3 p-3 rounded-lg text-sm" style="background:var(--state-success-surface);color:var(--state-success);">${escapeHtml(refineNotice)}</div>`
      : '');
  $('review-refine-panel').innerHTML = `
    <div class="flex flex-col sm:flex-row gap-2">
      <div class="field flex-1">
        <i data-lucide="sparkles" class="w-4 h-4"></i>
        <input data-dom-id="refine-instruction" class="control" type="text" value="${escapeHtml(lastRefineInstruction)}" placeholder="\u4f8b\u5982\uff1a\u628a\u5b98\u65b9\u7684\u4f5c\u54c1\u5355\u72ec\u653e\u5728\u4e00\u4e2a\u6536\u85cf\u5939\u5185">
      </div>
      <button data-dom-id="refine-submit" type="button" class="btn btn-primary">
        <i data-lucide="wand-sparkles" class="w-4 h-4"></i><span>\u751f\u6210\u65b0\u65b9\u6848</span>
      </button>
      <button data-dom-id="retry-unclassified" type="button" class="btn btn-secondary" ${unclassifiedCount ? '' : 'disabled'}>
        <i data-lucide="refresh-cw" class="w-4 h-4"></i><span>\u91cd\u8bd5\u672a\u5206\u7c7b${unclassifiedCount ? ` (${unclassifiedCount})` : ''}</span>
      </button>
    </div>
    ${noticeHtml}`;
  $('refine-submit').onclick = () => startRefineJob(sid, 'refine');
  $('retry-unclassified').onclick = () => startRefineJob(sid, 'unclassified_retry');
  if (window.lucide) lucide.createIcons();
}

function renderRefineProgress(sid, kind, event = {}) {
  activeRefineKind = kind;
  lastRefineProgress = { ...event };
  const progressValue = Number(event.progress || 0);
  const percent = Math.max(0, Math.min(100, Math.round(progressValue <= 1 ? progressValue * 100 : progressValue)));
  const processed = Math.max(0, Number(event.processed || 0));
  const total = Math.max(0, Number(event.total || 0));
  const retries = Math.max(0, Number(event.retry_count || 0));
  $('review-refine-panel').innerHTML = `
    <section data-dom-id="refine-progress" class="p-4 rounded-lg" style="background:var(--background-100);border:1px solid var(--border);" aria-live="polite">
      <div class="flex items-center justify-between gap-3">
        <div class="min-w-0">
          <p data-dom-id="refine-progress-status" class="text-sm font-medium truncate" style="color:var(--foreground);">${refineStageLabel(event.stage, kind)}</p>
          <p class="mt-1 text-xs" style="color:var(--muted-foreground);">${total ? `\u5df2\u5904\u7406 ${processed}/${total}` : '\u6b63\u5728\u51c6\u5907\u6570\u636e'}${retries ? ` \u00b7 \u5df2\u91cd\u8bd5 ${retries} \u6b21` : ''}</p>
        </div>
        <strong data-dom-id="refine-progress-percent" class="text-sm tabular-nums" style="color:var(--brand-600);">${percent}%</strong>
      </div>
      <div class="mt-3 h-2 rounded-full overflow-hidden" style="background:var(--background-300);">
        <div data-dom-id="refine-progress-bar" class="progress-fill h-full rounded-full" style="width:${percent}%;"></div>
      </div>
      <div class="mt-3 flex justify-end">
        <button data-dom-id="refine-cancel" type="button" class="btn btn-text" style="color:var(--state-error);">
          <i data-lucide="x" class="w-4 h-4"></i><span>\u53d6\u6d88</span>
        </button>
      </div>
    </section>`;
  $('refine-cancel').onclick = async () => {
    $('refine-cancel').disabled = true;
    try {
      await api(`/api/session/${sid}/refine/cancel`, { method: 'POST' });
    } catch (error) {
      renderRefinePanel(sid, error.message);
    }
  };
  if (window.lucide) lucide.createIcons();
}

function clearRefineJobState() {
  activeRefineJob = null;
  activeRefineKind = null;
  lastRefineProgress = null;
}

async function startRefineJob(sid, kind = 'refine') {
  if (kind === 'refine') {
    lastRefineInstruction = $('refine-instruction').value.trim();
    if (!lastRefineInstruction) return;
  }
  refineNotice = '';
  activeRefineJob = 'starting';
  renderRefineProgress(sid, kind, { stage: 'analyzing', progress: 0 });
  try {
    const endpoint = kind === 'unclassified_retry'
      ? `/api/session/${sid}/retry-unclassified`
      : `/api/session/${sid}/refine`;
    const job = await api(endpoint, {
      method: 'POST',
      ...(kind === 'refine' ? { body: JSON.stringify({ instruction: lastRefineInstruction }) } : {}),
    });
    activeRefineJob = job.job_id;
    connectRefineStream(sid, job.job_id, job.kind || kind);
  } catch (error) {
    clearRefineJobState();
    renderRefinePanel(sid, error.message);
  }
}

function resumeSession(session) {
  currentSid = session.session_id;
  if (['draft', 'collecting', 'classifying', 'failed'].includes(session.status)) {
    showView('progress');
    runPipeline(session.session_id, { reset: false });
    return;
  }
  openSession(session.session_id);
}

function connectRefineStream(sid, jobId, kind) {
  activeRefineJob = jobId;
  activeRefineKind = kind;
  const es = replaceEventSource('refine', new EventSource(`/api/session/${sid}/refine/stream?job_id=${encodeURIComponent(jobId)}`));
  es.addEventListener('stage', event => {
    renderRefineProgress(sid, kind, JSON.parse(event.data));
  });
  es.addEventListener('done', async event => {
    const data = JSON.parse(event.data);
    closeEventSource('refine', es);
    clearRefineJobState();
    const retryResult = data.kind === 'unclassified_retry' ? (data.result || {}) : null;
    if (retryResult) {
      refineNotice = retryResult.recovered
        ? `\u5df2\u6062\u590d ${retryResult.recovered} \u6761\uff0c\u4ecd\u6709 ${retryResult.remaining || 0} \u6761\u672a\u5206\u7c7b`
        : '\u672c\u6b21\u6ca1\u6709\u6062\u590d\u65b0\u6761\u76ee\uff0c\u672a\u521b\u5efa\u7a7a\u65b9\u6848';
    } else {
      refineNotice = '\u65b0\u65b9\u6848\u5df2\u751f\u6210';
    }
    try {
      await openSession(sid);
    } catch (error) {
      renderRefinePanel(sid, error.message || '\u65e0\u6cd5\u52a0\u8f7d\u65b0\u65b9\u6848');
    }
  });
  const handleFailure = event => {
    const data = JSON.parse(event.data);
    closeEventSource('refine', es);
    clearRefineJobState();
    renderRefinePanel(sid, data.message || '\u751f\u6210\u65b9\u6848\u5931\u8d25');
  };
  es.addEventListener('failed', handleFailure);
  es.addEventListener('cancelled', event => {
    closeEventSource('refine', es);
    clearRefineJobState();
    renderRefinePanel(sid, JSON.parse(event.data).message || '\u4efb\u52a1\u5df2\u53d6\u6d88');
  });
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED && activeRefineJob === jobId) {
      renderRefinePanel(sid, '\u8fdb\u5ea6\u8fde\u63a5\u5df2\u5173\u95ed\uff0c\u8fd4\u56de\u672c\u9875\u65f6\u5c06\u81ea\u52a8\u6062\u590d');
    }
  };
}

async function restoreRefineJob(sid) {
  try {
    const active = await api(`/api/session/${sid}/refine/active`);
    if (!active.running || !active.job_id) return;
    renderRefineProgress(sid, active.kind, active.progress || {});
    connectRefineStream(sid, active.job_id, active.kind);
  } catch (error) {
    renderRefinePanel(sid, error.message);
  }
}

function toggleSkippedPanel() {
  skippedPanelCollapsed = !skippedPanelCollapsed;
  renderSkippedPanelFromItems(currentSid, cachedSkippedItems || []);
}

function toggleSkippedReason(reasonCode) {
  if (collapsedSkippedReasons.has(reasonCode)) collapsedSkippedReasons.delete(reasonCode);
  else collapsedSkippedReasons.add(reasonCode);
  renderSkippedPanelFromItems(currentSid, cachedSkippedItems || []);
}

async function renderSkippedPanel(sid) {
  const data = await api(`/api/session/${sid}/skipped-items`);
  cachedSkippedItems = data.items || [];
  renderSkippedPanelFromItems(sid, cachedSkippedItems);
}

function renderSkippedPanelFromItems(sid, items) {
  const removable = items.filter(it => it.removable && !it.removed);
  if (!items.length) {
    $('review-skipped-panel').innerHTML = '';
    return;
  }
  const byReason = {};
  items.forEach(it => {
    const key = it.reason_code || 'unknown';
    (byReason[key] = byReason[key] || []).push(it);
  });
  const reasonLabels = {};
  items.forEach(it => { if (it.reason_label) reasonLabels[it.reason_code || 'unknown'] = it.reason_label; });

  const reasonGroups = Object.keys(byReason).map(reasonCode => {
    const groupItems = byReason[reasonCode];
    const groupRemovable = groupItems.filter(it => it.removable && !it.removed);
    const collapsed = collapsedSkippedReasons.has(reasonCode);
    const rowsHtml = collapsed ? '' : groupItems.map(it => {
      const itemName = it.title || (it.bvid
        ? `无法访问的视频（BVID：${it.bvid}）`
        : `无法访问的资源（ID：${it.avid}）`);
      const removedTag = it.removed
        ? `<span class="inline-flex items-center h-5 px-2 rounded-full text-xs" style="background:var(--state-success-surface);color:var(--state-success);">已移除</span>`
        : (it.removable
          ? `<label class="inline-flex items-center gap-1 text-xs"><input type="checkbox" data-skipped-id="${it.id}" checked>可移除</label>`
          : `<span class="inline-flex items-center h-5 px-2 rounded-full text-xs" style="background:var(--background-200);color:var(--muted-foreground);">不可移除</span>`);
      const err = it.remove_error ? `<span class="text-xs" style="color:var(--state-error);">${escapeHtml(it.remove_error)}</span>` : '';
      return `<div class="flex flex-wrap items-center gap-x-3 gap-y-1 p-3 rounded-lg" style="background:var(--card);border:1px solid var(--border);">
        <div class="flex-1 min-w-0">
          <div class="text-sm truncate" style="color:var(--foreground);">${escapeHtml(itemName)}</div>
          <div class="text-xs mt-0.5" style="color:var(--muted-foreground);">
            <span>来源: 收藏夹 ${escapeHtml(String(it.source_fid))}</span>
            <span class="mx-1">·</span>
            <span>原因: ${escapeHtml(it.reason_label || it.reason_code || '')}</span>
            ${it.detail ? `<span class="mx-1">·</span><span>${escapeHtml(it.detail)}</span>` : ''}
          </div>
          ${err}
        </div>
        <div class="shrink-0">${removedTag}</div>
      </div>`;
    }).join('');
    const toggleIcon = collapsed ? 'chevron-right' : 'chevron-down';
    return `<div>
      <button type="button" data-dom-id="toggle-skipped-reason-${escapeDomId(reasonCode)}" class="w-full flex items-center gap-2 py-2 px-1 text-left" style="background:transparent;border:0;cursor:pointer;color:var(--foreground);">
        <i data-lucide="${toggleIcon}" class="w-4 h-4"></i>
        <span class="text-sm font-medium">${escapeHtml(reasonLabels[reasonCode] || reasonCode)}</span>
        <span class="text-xs" style="color:var(--muted-foreground);">${groupItems.length} 个，${groupRemovable.length} 个可移除</span>
      </button>
      <div class="flex flex-col gap-2 mt-1">${rowsHtml}</div>
    </div>`;
  }).join('');

  const panelToggleIcon = skippedPanelCollapsed ? 'chevron-right' : 'chevron-down';
  const detailsHtml = skippedPanelCollapsed ? '' : `<div class="flex flex-col gap-3 mt-3">${reasonGroups}</div>`;
  $('review-skipped-panel').innerHTML = `
    <section class="rounded-xl p-4" style="background:var(--background-100);border:1px solid var(--border);">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <button type="button" data-dom-id="toggle-skipped-panel" class="flex items-center gap-2" style="background:transparent;border:0;cursor:pointer;color:var(--foreground);">
          <i data-lucide="${panelToggleIcon}" class="w-4 h-4"></i>
          <div class="text-left">
            <h2 class="text-sm font-semibold" style="color:var(--foreground);">跳过条目</h2>
            <p class="text-xs mt-0.5" style="color:var(--muted-foreground);">共 ${items.length} 个，${removable.length} 个可从收藏夹移除</p>
          </div>
        </button>
        <button data-dom-id="remove-skipped" type="button" class="btn btn-secondary" ${removable.length ? '' : 'disabled'}>
          <i data-lucide="trash-2" class="w-4 h-4"></i><span>移除勾选的不可访问项</span>
        </button>
      </div>
      ${detailsHtml}
    </section>`;
  const lucide = window.lucide;
  if (lucide) lucide.createIcons();
  const togglePanelBtn = $('toggle-skipped-panel');
  if (togglePanelBtn) togglePanelBtn.onclick = toggleSkippedPanel;
  Object.keys(byReason).forEach(reasonCode => {
    const btn = $(`toggle-skipped-reason-${escapeDomId(reasonCode)}`);
    if (btn) btn.onclick = () => toggleSkippedReason(reasonCode);
  });
  const btn = $('remove-skipped');
  if (btn) btn.onclick = async () => {
    const selected = [...document.querySelectorAll('[data-skipped-id]:checked')].map(el => Number(el.getAttribute('data-skipped-id')));
    if (!selected.length) return;
    if (!confirm(`将从 B 站收藏夹中移除 ${selected.length} 个不可访问条目。此操作不可逆。是否继续？`)) return;
    const r = await api(`/api/session/${sid}/skipped-items/remove`, {
      method: 'POST',
      body: JSON.stringify({ item_ids: selected }),
    });
    alert(`已移除 ${r.stats.success} 个，失败 ${r.stats.failed} 个`);
    cachedSkippedItems = null;
    renderSkippedPanel(sid);
  };
}

function renderReviewFilters(cats, byCat) {
  const total = cats.reduce((sum, c) => sum + byCat[c].length, 0);
  const chips = [
    { key: 'ALL', label: '全部', count: total },
    ...cats.map(c => ({ key: c, label: c, count: byCat[c].length })),
  ];
  $('review-filter-bar').innerHTML = chips.map(chip => `
    <button data-dom-id="review-filter-${escapeDomId(chip.key)}" type="button"
      class="btn ${activeReviewFilter === chip.key ? 'btn-primary' : 'btn-secondary'}"
      style="height:32px;padding:0 12px;font-size:12px;">
      <span>${escapeHtml(chip.label)}</span>
      <span class="inline-flex items-center justify-center h-5 min-w-5 px-1.5 rounded-full text-xs"
        style="background:var(--background-100);color:var(--foreground);">${chip.count}</span>
    </button>`).join('');
  chips.forEach(chip => {
    $(`review-filter-${escapeDomId(chip.key)}`).onclick = () => {
      activeReviewFilter = chip.key;
      renderReview(currentSid, window.__lastReviewPlan);
    };
  });
}

function toggleReviewGroup(cat) {
  if (collapsedReviewGroups.has(cat)) collapsedReviewGroups.delete(cat);
  else collapsedReviewGroups.add(cat);
  renderReview(currentSid, window.__lastReviewPlan);
}

function setExecutionProgressVisible(visible) {
  const actions = $('review-actions');
  const progress = $('execution-progress');
  if (actions) actions.style.display = visible ? 'none' : 'flex';
  if (progress) progress.style.display = visible ? 'block' : 'none';
}

function updateExecutionProgress(data) {
  const total = Math.max(0, Number(data.total || 0));
  const processed = Math.max(0, Number(data.processed || 0));
  const success = Math.max(0, Number(data.success || 0));
  const failed = Math.max(0, Number(data.failed || 0));
  const ratio = typeof data.progress === 'number'
    ? data.progress
    : (total ? processed / total : 0);
  const percent = Math.max(0, Math.min(100, Math.round(ratio * 100)));

  let status = '正在执行整理方案';
  if (data.phase === 'reconciling') {
    status = '正在核验上次执行进度';
  } else if (data.phase === 'creating_folders') {
    const created = Number(data.folders_created || 0);
    const folderTotal = Number(data.folders_total || 0);
    status = folderTotal ? `正在创建目标收藏夹（${created}/${folderTotal}）` : '正在准备目标收藏夹';
  } else if (data.phase === 'moving') {
    status = data.category ? `正在移动到“${data.category}”` : '正在移动收藏条目';
  }

  $('execution-status').textContent = status;
  $('execution-percent').textContent = `${percent}%`;
  $('execution-progress-bar').style.width = `${percent}%`;
  $('execution-total').textContent = total;
  $('execution-processed').textContent = processed;
  $('execution-success').textContent = success;
  $('execution-failed').textContent = failed;
}

async function startExecutionProgress(sid, jobId = null) {
  closeEventSource('execution');
  isExecuting = true;
  setExecutionProgressVisible(true);
  updateExecutionProgress({
    phase: 'creating_folders',
    progress: 0,
    processed: 0,
    total: 0,
    success: 0,
    failed: 0,
  });

  try {
    if (!jobId) {
      const job = await api(`/api/session/${sid}/execute`, { method: 'POST' });
      jobId = job.job_id;
    }
  } catch (error) {
    isExecuting = false;
    $('execution-status').textContent = `\u6267\u884c\u5f02\u5e38\uff1a${error.message}`;
    return;
  }

  const es = replaceEventSource('execution', new EventSource(`/api/session/${sid}/execute/stream?job_id=${encodeURIComponent(jobId)}`));
  es.addEventListener('stage', event => {
    updateExecutionProgress(JSON.parse(event.data));
  });
  es.addEventListener('done', event => {
    const data = JSON.parse(event.data || '{}');
    closeEventSource('execution', es);
    isExecuting = false;
    renderResult(sid, data.stats || {});
  });
  es.addEventListener('fail', event => {
    const data = JSON.parse(event.data || '{}');
    closeEventSource('execution', es);
    isExecuting = false;
    $('execution-status').textContent = `执行异常：${data.message || '未知错误'}`;
    alert(data.message || '执行失败');
  });
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) {
      $('execution-status').textContent = '进度连接已关闭，请返回首页后重新打开任务';
    } else {
      $('execution-status').textContent = '连接中断，正在自动重连...';
    }
  };
  $('execution-back-home').onclick = () => {
    showView('home');
    renderHome();
  };
}

async function restoreExecutionProgress(sid) {
  try {
    const active = await api(`/api/session/${sid}/execute/active`);
    if (active.running && active.job_id) {
      if (active.progress) updateExecutionProgress(active.progress);
      startExecutionProgress(sid, active.job_id);
      return;
    }
    isExecuting = false;
    setExecutionProgressVisible(false);
  } catch (error) {
    $('execution-status').textContent = `\u65e0\u6cd5\u6062\u590d\u6267\u884c\u8fdb\u5ea6\uff1a${error.message}`;
  }
}

async function renderReview(sid, plan) {
  showView('review');
  currentSid = sid;
  window.__lastReviewPlan = plan;
  isExecuting = Boolean(plan.session && plan.session.status === 'executing');
  const items = plan.items;
  const videos = plan.videos || {};
  const byCat = {};
  items.forEach(it => { byCat[it.category] = byCat[it.category] || []; byCat[it.category].push(it); });
  const cats = Object.keys(byCat);
  const palette = ['var(--primary)', 'var(--chart-5)', 'var(--chart-3)', 'var(--chart-4)', 'var(--chart-1)', 'var(--chart-2)'];

  let summaryText = items.length
    ? `${items.length} 个可整理条目，分成 ${cats.length} 类。可下拉调整单个条目的分类。`
    : '没有可整理条目。可在下方查看并处理跳过条目。';
  try {
    const sess = plan.session || {};
    const st = sess.stats ? (typeof sess.stats === 'string' ? JSON.parse(sess.stats) : sess.stats) : {};
    if (st.skipped_total && st.skipped_total > 0) {
      summaryText += ` 本次跳过 ${st.skipped_total} 个不可处理条目。`;
    }
  } catch (_) {}
  $('review-summary').textContent = summaryText;

  renderVersionBar(sid, plan.versions);
  if (activeRefineJob) {
    renderRefineProgress(sid, activeRefineKind, lastRefineProgress);
  } else {
    renderRefinePanel(sid);
  }
  renderReviewFilters(cats, byCat);
  renderSkippedPanel(sid);

  const catsToRender = activeReviewFilter === 'ALL' ? cats : cats.filter(c => c === activeReviewFilter);

  $('review-plan').innerHTML = catsToRender.map((cat, ci) => {
    const color = palette[cats.indexOf(cat) % palette.length];
    const collapsed = collapsedReviewGroups.has(cat);
    const rows = byCat[cat].map(it => {
      const rid = it.resource_id != null ? it.resource_id : it.avid;
      const rtype = it.resource_type != null ? it.resource_type : 2;
      const v = videos[`${rid}:${rtype}`] || videos[rid] || {};
      const conf = Math.round((it.confidence || 0) * 100);
      const badgeBg = conf >= 90 ? 'var(--state-success-surface)' : 'var(--background-200)';
      const badgeFg = conf >= 90 ? 'var(--state-success)' : 'var(--chart-3)';
      const options = cats.map(c => `<option value="${escapeHtml(c)}" ${c === cat ? 'selected' : ''}>${escapeHtml(c)}</option>`).join('');
      return `<article class="flex flex-wrap items-center gap-x-4 gap-y-2 p-4 rounded-xl transition-all" style="background: var(--card); border: 1px solid var(--border); box-shadow: var(--shadow-xs);">
        <img src="${escapeHtml(v.cover_url || '')}" onerror="this.style.display='none'" class="shrink-0 w-20 h-12 sm:w-[96px] sm:h-[60px] rounded-lg object-cover" style="background: linear-gradient(135deg, color-mix(in srgb, ${color} 20%, var(--background-100)), var(--background-200));">
        <div class="flex-1 min-w-0 flex flex-col gap-1">
          <span class="text-sm font-medium truncate" style="color: var(--foreground);">${escapeHtml(v.title || rid)}</span>
          <span class="inline-flex items-center gap-1 text-xs truncate" style="color: var(--muted-foreground);">
            <i data-lucide="user" class="w-3.5 h-3.5 shrink-0"></i><span class="truncate">${escapeHtml(v.up_name || '')}</span>
          </span>
        </div>
        <span class="shrink-0 inline-flex items-center justify-center h-6 px-2.5 rounded-full text-xs font-semibold" style="background:${badgeBg};color:${badgeFg};">${conf}%</span>
        <div class="relative shrink-0 w-full sm:w-auto">
          <select data-dom-id="adj-${rid}-${rtype}" class="h-9 w-full sm:w-auto pl-3 pr-8 rounded-lg text-sm appearance-none cursor-pointer" style="background: var(--secondary); color: var(--secondary-foreground); border: 1px solid var(--border); outline: none; min-width: 120px;">${options}</select>
          <i data-lucide="chevron-down" class="w-4 h-4 absolute right-2.5 top-1/2 -translate-y-1/2 pointer-events-none" style="color: var(--muted-foreground);"></i>
        </div>
      </article>`;
    }).join('');
    return `<section class="mb-8">
      <button type="button" data-dom-id="toggle-cat-${escapeDomId(cat)}" class="w-full relative overflow-hidden rounded-xl mb-4 px-4 py-3 flex items-center gap-3 text-left" style="background: var(--background-100); border:0; cursor:pointer;">
        <div class="absolute left-0 top-0 bottom-0 w-1" style="background:${color};"></div>
        <i data-lucide="${collapsed ? 'chevron-right' : 'chevron-down'}" class="w-4 h-4 shrink-0" style="color: var(--foreground);"></i>
        <h2 class="text-base font-semibold tracking-tight" style="color: var(--foreground);">${escapeHtml(cat)}</h2>
        <span class="inline-flex items-center justify-center h-6 px-2.5 rounded-full text-xs font-semibold" style="background: color-mix(in srgb, ${color} 10%, transparent); color: ${color};">${byCat[cat].length}</span>
      </button>
      <div class="flex flex-col gap-3" style="${collapsed ? 'display:none;' : ''}">${rows}</div>
    </section>`;
  }).join('');

  if (items.length) {
    items.forEach(it => {
      const rid = it.resource_id != null ? it.resource_id : it.avid;
      const rtype = it.resource_type != null ? it.resource_type : 2;
      const sel = $(`adj-${rid}-${rtype}`);
      if (sel) sel.onchange = () => adjustItem(sid, rid, rtype, sel.value);
    });
  }

  catsToRender.forEach(cat => {
    const btn = $(`toggle-cat-${escapeDomId(cat)}`);
    if (btn) btn.onclick = () => toggleReviewGroup(cat);
  });

  if (window.lucide) lucide.createIcons();

  // 重置执行区域状态：执行中的会话直接恢复 SSE 监控。
  const execBtn = $('execute-confirm');
  setExecutionProgressVisible(isExecuting);
  if (isExecuting) {
    execBtn.disabled = true;
    execBtn.innerHTML = '<i data-lucide="loader" class="w-5 h-5 spin-slow"></i><span>执行中...</span>';
  } else {
    execBtn.disabled = false;
    execBtn.innerHTML = '<i data-lucide="check" class="w-5 h-5"></i><span>确认执行</span>';
  }
  if (window.lucide) lucide.createIcons();

  execBtn.onclick = () => {
    if (isExecuting) return;
    if (!confirm('确认执行？将创建新收藏夹并移动条目，此操作不可逆。')) return;
    startExecutionProgress(sid);
  };
  $('review-back-home').onclick = () => { showView('home'); renderHome(); };
  $('review-abandon').onclick = async () => {
    if (!confirm('放弃本次分类方案？该会话将被取消，不会再出现在继续列表。')) return;
    try {
      await api(`/api/session/${sid}/cancel`, { method: 'POST' });
      showView('home');
      renderHome();
    } catch (e) { alert(e.message); }
  };

  if (isExecuting) restoreExecutionProgress(sid);
  if (activeRefineJob && !eventSources.refine) {
    restoreRefineJob(sid);
  } else if (!activeRefineJob) {
    restoreRefineJob(sid);
  }
}

async function adjustItem(sid, resourceId, resourceType, newCat) {
  await api(`/api/session/${sid}/adjust`, {
    method: 'POST',
    body: JSON.stringify({ resource_id: resourceId, resource_type: resourceType, new_category: newCat }),
  });
  const plan = await api(`/api/session/${sid}`);
  renderReview(sid, plan);
}

async function renderEmptySourceFolders(sid) {
  const data = await api(`/api/session/${sid}/empty-source-folders`);
  const candidates = data.items.filter(it => it.delete_candidate && !it.deleted);
  if (!candidates.length) {
    $('empty-source-folders').innerHTML = '';
    return;
  }
  $('empty-source-folders').innerHTML = `
    <section class="rounded-xl p-4" style="background:var(--background-100);border:1px solid var(--border);">
      <div class="flex items-center justify-between gap-3">
        <div>
          <h2 class="text-sm font-semibold" style="color:var(--foreground);">空收藏夹</h2>
          <p class="text-xs mt-1" style="color:var(--muted-foreground);">整理后发现 ${candidates.length} 个源收藏夹为空，可手动选择删除。</p>
        </div>
        <button data-dom-id="delete-empty-folders" type="button" class="btn btn-secondary">
          <i data-lucide="trash-2" class="w-4 h-4"></i><span>删除选中的空收藏夹</span>
        </button>
      </div>
      <div class="mt-3 flex flex-col gap-2">
        ${candidates.map(it => `
          <label class="flex items-center gap-2 text-sm" style="color:var(--foreground);">
            <input type="checkbox" data-empty-folder-id="${it.source_fid}">
            <span>${escapeHtml(it.title)}</span>
          </label>`).join('')}
      </div>
    </section>`;
  $('delete-empty-folders').onclick = async () => {
    const selected = [...document.querySelectorAll('[data-empty-folder-id]:checked')].map(el => Number(el.getAttribute('data-empty-folder-id')));
    if (!selected.length) return;
    if (!confirm(`将删除 ${selected.length} 个空收藏夹，此操作不可逆。是否继续？`)) return;
    const r = await api(`/api/session/${sid}/empty-source-folders/delete`, {
      method: 'POST',
      body: JSON.stringify({ source_fids: selected }),
    });
    alert(`已删除 ${r.stats.success} 个，拒绝或失败 ${r.stats.failed} 个`);
    renderEmptySourceFolders(sid);
  };
}

async function renderResult(sid, stats) {
  lastResultStats = { ...stats };
  showView('result');
  $('result-stats').innerHTML = `
    <div class="flex flex-col items-center text-center p-4 sm:p-6 rounded-xl border" style="background: var(--state-success-surface); border-color: var(--border);">
      <i data-lucide="check-circle" style="width:24px;height:24px;color:var(--state-success);"></i>
      <span class="mt-3 text-3xl sm:text-4xl font-bold" style="color: var(--state-success);">${stats.success}</span>
      <span class="mt-1 text-xs" style="color: var(--muted-foreground);">个条目已移动</span>
    </div>
    <div class="flex flex-col items-center text-center p-4 sm:p-6 rounded-xl border" style="background: var(--state-error-surface); border-color: var(--border);">
      <i data-lucide="x-circle" style="width:24px;height:24px;color:var(--state-error);"></i>
      <span class="mt-3 text-3xl sm:text-4xl font-bold" style="color: var(--state-error);">${stats.failed}</span>
      <span class="mt-1 text-xs" style="color: var(--muted-foreground);">个移动失败</span>
    </div>
    <div class="flex flex-col items-center text-center p-4 sm:p-6 rounded-xl border" style="background: var(--background-200); border-color: var(--border);">
      <i data-lucide="layers" style="width:24px;height:24px;color:var(--foreground);"></i>
      <span class="mt-3 text-3xl sm:text-4xl font-bold" style="color: var(--foreground);">${stats.total}</span>
      <span class="mt-1 text-xs" style="color: var(--muted-foreground);">个条目</span>
    </div>`;

  const failedEl = $('result-failed');
  const retryBtn = $('retry-failed');
  if (stats.failed > 0) {
    const r = await api(`/api/session/${sid}/failed-items`);
    failedEl.innerHTML = `
      <div class="flex items-center gap-2 mb-4">
        <i data-lucide="alert-circle" style="width:18px;height:18px;color:var(--state-error);"></i>
        <h2 class="text-sm font-semibold" style="color: var(--foreground);">失败项</h2>
        <span class="inline-flex items-center justify-center h-5 px-2 rounded-md text-xs font-semibold" style="background: var(--state-error-surface); color: var(--state-error);">${r.items.length}</span>
      </div>
      <div class="space-y-3">
        ${r.items.map(it => `<div class="rounded-lg p-4 border" style="background: var(--state-error-surface); border-color: var(--border);">
          <p class="text-sm font-medium truncate" style="color: var(--foreground);">${escapeHtml(it.title)}</p>
          <div class="mt-1 flex items-center gap-1.5">
            <i data-lucide="info" style="width:12px;height:12px;color:var(--muted-foreground);flex-shrink:0;"></i>
            <span class="text-xs truncate" style="color: var(--muted-foreground);">${escapeHtml(it.error_message)}</span>
          </div>
        </div>`).join('')}
      </div>`;
    retryBtn.style.display = 'inline-flex';
    retryBtn.onclick = async () => {
      retryBtn.disabled = true;
      try {
        const r2 = await api(`/api/session/${sid}/retry-failed`, { method: 'POST' });
        renderResult(sid, { success: stats.success + r2.stats.success, failed: r2.stats.failed, total: stats.total });
      } catch (e) { alert(e.message); retryBtn.disabled = false; }
    };
  } else {
    failedEl.innerHTML = '';
    retryBtn.style.display = 'none';
  }

  $('back-home').onclick = () => { showView('home'); renderHome(); };
  if (window.lucide) lucide.createIcons();
  renderEmptySourceFolders(sid);
}

$('nav-settings').onclick = () => openUtilityView('config');
$('nav-account').onclick = () => openUtilityView('accounts');
$('accounts-back').onclick = async () => {
  await cancelAddAccountLogin();
  await returnFromUtilityView();
};
$('account-logout').onclick = logoutAccount;

async function logoutAccount() {
  if (!confirm('确定要退出当前 B 站账号吗？退出后需重新扫码登录。')) return;
  try {
    await api('/api/logout', { method: 'POST' });
    utilityReturnContext = null;
    currentSid = null;
    clearRefineJobState();
    showView('login');
    renderLogin();
    $('nav-account-name').textContent = '';
  } catch (e) {
    alert(e.message || '退出失败');
  }
}

async function renderAccounts() {
  try {
    const data = await api('/api/accounts');
    const accounts = data.accounts || [];
    const active = data.active;
    // 更新导航栏账号名
    $('nav-account-name').textContent = active ? active.uname : '';
    $('accounts-list').innerHTML = accounts.map(a => `
      <div class="flex items-center gap-4 p-4 rounded-xl" style="background:var(--card);border:1px solid var(--border);${active && a.account_id === active.account_id ? 'border-color:var(--brand-500);background:var(--brand-50);' : ''}">
        <div class="shrink-0 flex items-center justify-center w-10 h-10 rounded-full" style="background:var(--brand-100);">
          <i data-lucide="user" class="w-5 h-5" style="color:var(--brand-600);"></i>
        </div>
        <div class="flex-1 min-w-0">
          <p class="text-sm font-medium truncate" style="color:var(--foreground);">${escapeHtml(a.uname || a.account_id)}</p>
          <p class="text-xs mt-0.5" style="color:var(--muted-foreground);">mid: ${escapeHtml(String(a.mid))}</p>
        </div>
        ${active && a.account_id === active.account_id
          ? '<span class="shrink-0 inline-flex items-center h-6 px-2.5 rounded-full text-xs font-semibold" style="background:var(--brand-100);color:var(--brand-700);">当前</span>'
          : `<button data-dom-id="switch-account-${escapeDomId(a.account_id)}" type="button" class="btn btn-secondary shrink-0" style="height:32px;padding:0 14px;font-size:12px;">切换</button>`}
      </div>`).join('') || '<p style="color:var(--muted-foreground);">暂无账号</p>';
    accounts.forEach(a => {
      if (active && a.account_id === active.account_id) return;
      const btn = $(`switch-account-${escapeDomId(a.account_id)}`);
      if (btn) btn.onclick = async () => {
        try {
          await api(`/api/accounts/${a.account_id}/switch`, { method: 'POST' });
          resetUtilityReturnAfterAccountChange();
          renderAccounts();
          renderHome();
        } catch (e) {
          alert(e.message || '切换失败');
        }
      };
    });
    // 添加账号按钮
    $('accounts-add').innerHTML = `
      <div class="flex flex-col items-center gap-4 p-6 rounded-xl" style="background:var(--card);border:1px dashed var(--border);">
        <i data-lucide="user-plus" class="w-8 h-8" style="color:var(--brand-500);"></i>
        <p class="text-sm text-center" style="color:var(--muted-foreground);">扫码添加新 B 站账号，不会影响当前账号登录态</p>
        <button data-dom-id="account-add-start" type="button" class="btn btn-primary">
          <i data-lucide="qr-code" class="w-4 h-4"></i><span>扫码添加账号</span>
        </button>
        <div data-dom-id="account-add-qr" class="mt-4"></div>
      </div>`;
    $('account-add-start').onclick = startAddAccountLogin;
    if (window.lucide) lucide.createIcons();
  } catch (e) {
    if (e.code === 'NOT_LOGGED_IN') {
      showView('login');
      renderLogin();
      return;
    }
    alert(e.message);
  }
}

let addAccountLoginId = null;
let addAccountQrToken = 0;

async function cancelAddAccountLogin() {
  addAccountQrToken++;
  const loginId = addAccountLoginId;
  addAccountLoginId = null;
  if (!loginId) return;
  try {
    await api(`/api/accounts/login/${encodeURIComponent(loginId)}/cancel`, { method: 'POST' });
  } catch (_) {
    // 登录会话可能已经完成或过期，不应阻止页面返回。
  }
}

async function startAddAccountLogin() {
  try {
    await cancelAddAccountLogin();
    const r = await api('/api/accounts/login/start', { method: 'POST' });
    addAccountLoginId = r.login_id;
    addAccountQrToken++;
    const myToken = addAccountQrToken;
    $('account-add-qr').innerHTML = `
      <div class="flex flex-col items-center gap-3">
        <img src="${r.image}" class="w-48 h-48 rounded-xl" style="border:1px solid var(--border);">
        <p class="text-xs" style="color:var(--muted-foreground);">请使用 B 站 App 扫描二维码</p>
      </div>`;
    pollAddAccountLogin(r.qrcode_key, myToken);
  } catch (e) {
    alert(e.message);
  }
}

async function pollAddAccountLogin(qrcode_key, myToken) {
  while (myToken === addAccountQrToken) {
    try {
      const r = await api(`/api/accounts/login/poll?login_id=${encodeURIComponent(addAccountLoginId)}&qrcode_key=${encodeURIComponent(qrcode_key)}`);
      if (r.status === 'success') {
        addAccountQrToken++;
        addAccountLoginId = null;
        resetUtilityReturnAfterAccountChange();
        alert('添加账号成功');
        renderAccounts();
        return;
      }
      if (r.status === 'expired' || r.status === 'failed') {
        addAccountQrToken++;
        addAccountLoginId = null;
        alert('二维码已过期或失败，请重新扫码');
        $('account-add-qr').innerHTML = '';
        return;
      }
    } catch (e) {
      addAccountQrToken++;
      alert(e.message || '轮询失败');
      return;
    }
    await new Promise(r => setTimeout(r, 1500));
  }
}

start();
