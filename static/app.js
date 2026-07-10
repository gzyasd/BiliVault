const $ = (id) => document.querySelector(`[data-dom-id="${id}"]`);

let currentMode = 'quick';
let currentSid = null;
let activeEventSource = null;
let qrPollToken = 0;
let lastStableView = 'home';
let activeReviewFilter = 'ALL';
let isExecuting = false;
const collapsedReviewGroups = new Set();
const selectedSourceFids = new Set();
let skippedPanelCollapsed = false;
const collapsedSkippedReasons = new Set();
let cachedSkippedItems = null;

function escapeHtml(value) {
  const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return String(value ?? '').replace(/[&<>"']/g, ch => map[ch]);
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

function cleanupPollingAndSSE() {
  if (activeEventSource) {
    activeEventSource.close();
    activeEventSource = null;
  }
  qrPollToken++;
}

function showView(name) {
  cleanupPollingAndSSE();
  document.querySelectorAll('[data-view]').forEach(s => s.classList.remove('active'));
  document.querySelector(`[data-view="${name}"]`).classList.add('active');
  if (window.lucide) lucide.createIcons();
  const stableViews = ['home', 'config', 'login'];
  if (stableViews.includes(name)) lastStableView = name;
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
          default_privacy: 1,
          ai_batch_size: Number($('config-ai-batch-size').value || 100),
        }),
      });
      start();
    } catch (e) { alert(e.message); }
  };
  $('config-cancel').onclick = () => { start(); };
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

async function renderHome() {
  try {
    selectedSourceFids.clear();
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
      $('resume-continue').onclick = () => openSession(s.session_id);
    } else {
      resumeEl.style.display = 'none';
    }

    const data = await api('/api/folders');
    const colors = ['var(--brand-100)', 'var(--state-success-surface)', 'var(--state-error-surface)', 'var(--brand-50)'];
    const iconColors = ['var(--brand-600)', 'var(--state-success)', 'var(--state-error)', 'var(--brand-500)'];
    $('folder-list').innerHTML = data.folders.map((f, i) => `
      <div data-dom-id="select-folder-${f.fid}" data-folder-id="${f.fid}" class="flex items-center gap-4 p-4 rounded-xl cursor-pointer transition-all hover:shadow-md hover:-translate-y-0.5" style="background: var(--card); border: 1px solid var(--border); box-shadow: var(--shadow-xs);">
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
        <i data-lucide="chevron-right" class="shrink-0" style="width:18px;height:18px;color:var(--icon-300);"></i>
      </div>`).join('') || '<p style="color:var(--muted-foreground);">暂无收藏夹</p>';

    data.folders.forEach(f => {
      $(`select-folder-${f.fid}`).onclick = () => toggleFolderSelection(f.fid);
    });
    if (window.lucide) lucide.createIcons();

    $('start-organize').onclick = () => {
      if (!selectedSourceFids.size) return;
      newSession([...selectedSourceFids]);
    };

    $('mode-quick').onclick = () => setMode('quick');
    $('mode-full').onclick = () => setMode('full');
  } catch (e) {
    if (e.code === 'NOT_LOGGED_IN') {
      showView('login');
      renderLogin();
      return;
    }
    alert(e.message);
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
  const count = selectedSourceFids.size;
  btn.disabled = count === 0;
  btn.querySelector('span').textContent = count ? `开始智能整理（${count} 个收藏夹）` : '开始智能整理';
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
  const r = await api('/api/session', { method: 'POST', body: JSON.stringify({ source_fids: sourceFids, mode: currentMode }) });
  currentSid = r.session_id;
  showView('progress');
  runPipeline(r.session_id);
}

async function openSession(sid) {
  currentSid = sid;
  const plan = await api(`/api/session/${sid}`);
  renderReview(sid, plan);
}

function runPipeline(sid) {
  $('progress-percent').textContent = '0%';
  $('progress-bar').style.width = '0%';
  $('progress-status').textContent = '准备中...';
  $('progress-stats').style.display = 'none';
  renderSteps('collecting');

  const es = new EventSource(`/api/session/${sid}/stream`);
  activeEventSource = es;
  es.addEventListener('stage', e => {
    const d = JSON.parse(e.data);
    updateProgress(d);
  });
  es.addEventListener('done', () => {
    es.close();
    activeEventSource = null;
    openSession(sid);
  });
  es.addEventListener('fail', e => {
    es.close();
    activeEventSource = null;
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
    es.close();
    activeEventSource = null;
  };
  $('progress-back-home').onclick = () => {
    es.close();
    activeEventSource = null;
    showView('home');
    renderHome();
  };
  $('progress-cancel').onclick = async () => {
    if (!confirm('确认取消本次整理？已分类的进度将丢弃。')) return;
    try {
      await api(`/api/session/${sid}/cancel`, { method: 'POST' });
      es.close();
      activeEventSource = null;
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

function renderRefinePanel(sid) {
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
      const removedTag = it.removed
        ? `<span class="inline-flex items-center h-5 px-2 rounded-full text-xs" style="background:var(--state-success-surface);color:var(--state-success);">已移除</span>`
        : (it.removable
          ? `<label class="inline-flex items-center gap-1 text-xs"><input type="checkbox" data-skipped-id="${it.id}" checked>可移除</label>`
          : `<span class="inline-flex items-center h-5 px-2 rounded-full text-xs" style="background:var(--background-200);color:var(--muted-foreground);">不可移除</span>`);
      const err = it.remove_error ? `<span class="text-xs" style="color:var(--state-error);">${escapeHtml(it.remove_error)}</span>` : '';
      return `<div class="flex flex-wrap items-center gap-x-3 gap-y-1 p-3 rounded-lg" style="background:var(--card);border:1px solid var(--border);">
        <div class="flex-1 min-w-0">
          <div class="text-sm truncate" style="color:var(--foreground);">${escapeHtml(it.title || ('avid ' + it.avid))}</div>
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

async function renderReview(sid, plan) {
  showView('review');
  currentSid = sid;
  window.__lastReviewPlan = plan;
  const items = plan.items;
  const videos = plan.videos || {};
  const byCat = {};
  items.forEach(it => { byCat[it.category] = byCat[it.category] || []; byCat[it.category].push(it); });
  const cats = Object.keys(byCat);
  const palette = ['var(--primary)', 'var(--chart-5)', 'var(--chart-3)', 'var(--chart-4)', 'var(--chart-1)', 'var(--chart-2)'];

  let summaryText = `${items.length} 个可整理条目，分成 ${cats.length} 类。可下拉调整单个条目的分类。`;
  try {
    const sess = plan.session || {};
    const st = sess.stats ? (typeof sess.stats === 'string' ? JSON.parse(sess.stats) : sess.stats) : {};
    if (st.skipped_total && st.skipped_total > 0) {
      summaryText += ` 本次跳过 ${st.skipped_total} 个不可处理条目。`;
    }
  } catch (_) {}
  $('review-summary').textContent = summaryText;

  renderVersionBar(sid, plan.versions);
  renderRefinePanel(sid);
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

  // 重置执行按钮状态：防止上一次执行残留的 disabled 加载到本次预览
  const execBtn = $('execute-confirm');
  if (isExecuting) {
    execBtn.disabled = true;
    execBtn.innerHTML = '<i data-lucide="loader" class="w-5 h-5 spin-slow"></i><span>执行中...</span>';
  } else {
    execBtn.disabled = false;
    execBtn.innerHTML = '<i data-lucide="check" class="w-5 h-5"></i><span>确认执行</span>';
  }
  if (window.lucide) lucide.createIcons();

  execBtn.onclick = async () => {
    if (isExecuting) return;
    if (!confirm('确认执行？将创建新收藏夹并移动条目，此操作不可逆。')) return;
    // 进入执行中状态：切换加载动画，禁用按钮
    isExecuting = true;
    execBtn.disabled = true;
    execBtn.innerHTML = '<i data-lucide="loader" class="w-5 h-5 spin-slow"></i><span>执行中...</span>';
    if (window.lucide) lucide.createIcons();
    try {
      const r = await api(`/api/session/${sid}/execute`, { method: 'POST' });
      isExecuting = false;
      renderResult(sid, r.stats);
    } catch (e) {
      alert(e.message);
      isExecuting = false;
      execBtn.disabled = false;
      execBtn.innerHTML = '<i data-lucide="check" class="w-5 h-5"></i><span>确认执行</span>';
      if (window.lucide) lucide.createIcons();
    }
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

$('nav-settings').onclick = () => { showView('config'); loadConfig(); };
$('nav-account').onclick = () => { showView('accounts'); renderAccounts(); };
$('accounts-back').onclick = () => { showView(lastStableView); if (lastStableView === 'home') renderHome(); };
$('account-logout').onclick = logoutAccount;

async function logoutAccount() {
  if (!confirm('确定要退出当前 B 站账号吗？退出后需重新扫码登录。')) return;
  try {
    await api('/api/logout', { method: 'POST' });
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

async function startAddAccountLogin() {
  try {
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
        alert('添加账号成功');
        renderAccounts();
        return;
      }
      if (r.status === 'expired' || r.status === 'failed') {
        addAccountQrToken++;
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
