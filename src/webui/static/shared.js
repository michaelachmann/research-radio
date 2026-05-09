// ── Utility functions ──────────────────────────────────────────────────────

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function sqEscape(s) {
  return s.replace(/'/g, "'\\''");
}

function copyCode(btn) {
  const block = btn.closest('.chunk-block,.ffmpeg-block');
  const code  = block ? block.querySelector('.code-box') : null;
  if (!code) return;
  navigator.clipboard.writeText(code.textContent).then(() => {
    const orig = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(() => btn.textContent = orig, 1200);
  });
}

function copyOutput() {
  const area = document.getElementById('output-area');
  if (!area || !area.value.trim()) return;
  const btns = document.querySelectorAll('.panel-mid .btn-sm');
  navigator.clipboard.writeText(area.value).then(() => {
    btns.forEach(b => { if (b.textContent.includes('Copy')) { b.textContent = 'Copied'; setTimeout(() => b.textContent = 'Copy', 1200); }});
  });
}

function setStatus(msg, isError = false) {
  const bar = document.getElementById('statusbar');
  if (!bar) return;
  bar.textContent = msg;
  bar.style.color = isError ? '#dc2626' : '#64748b';
}

function setPill(state, label) {
  const el = document.getElementById('status-pill');
  if (!el) return;
  el.className = 'pill pill-' + state;
  el.textContent = label;
}

function setMeta(text) {
  const el = document.getElementById('output-meta');
  if (el) el.textContent = text;
}

function setAudioPill(state, label) {
  const el = document.getElementById('audio-pill');
  if (!el) return;
  el.className = 'pill pill-' + state;
  el.textContent = label;
}

function resetAudioBtn() {
  const btn = document.getElementById('create-audio-btn');
  if (!btn) return;
  btn.disabled = false;
  btn.classList.remove('running');
  btn.textContent = btn.dataset.label || 'Convert to Audio';
}

function appendAudioLog(msg) {
  const log = document.getElementById('audio-log');
  if (!log) return;
  log.textContent += (log.textContent ? '\n' : '') + msg;
  log.scrollTop = log.scrollHeight;
}

// ── Tab switching ──────────────────────────────────────────────────────────

let _currentTab = 'script';

function switchTab(tab) {
  _currentTab = tab;
  ['script','raw'].forEach(t => {
    const btn = document.getElementById('tab-' + t);
    const el  = t === 'script'
      ? document.getElementById('script-view')
      : document.getElementById('output-area');
    if (btn) btn.classList.toggle('active', t === tab);
    if (el)  el.style.display = (t === tab ? '' : 'none');
  });
}

// ── Shared initialization (runs after inline config block) ─────────────────

const modelSelect    = document.getElementById('model-select');
const thinkingToggle = document.getElementById('thinking-toggle');
const thinkingLabel  = document.getElementById('thinking-label');

if (modelSelect && typeof MODELS !== 'undefined') {
  MODELS.forEach(m => {
    const opt = document.createElement('option');
    opt.value = m.id;
    opt.textContent = m.label;
    if (m.id === DEFAULT_MODEL) opt.selected = true;
    modelSelect.appendChild(opt);
  });
}

function syncThinkingToggle() {
  if (!thinkingToggle || !modelSelect) return;
  const m = (typeof MODELS !== 'undefined') && MODELS.find(x => x.id === modelSelect.value);
  thinkingToggle.disabled = !m?.thinking;
  if (!m?.thinking) thinkingToggle.checked = false;
  thinkingLabel.textContent = thinkingToggle.checked ? 'on' : 'off';
}

if (modelSelect) modelSelect.addEventListener('change', syncThinkingToggle);
if (thinkingToggle) thinkingToggle.addEventListener('change', () => {
  thinkingLabel.textContent = thinkingToggle.checked ? 'on' : 'off';
});
syncThinkingToggle();

const presetSelect = document.getElementById('preset-select');

if (presetSelect && typeof PROMPTS !== 'undefined') {
  Object.entries(PROMPTS).forEach(([key, p]) => {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = p.label;
    presetSelect.appendChild(opt);
  });
}

function applyPreset(key) {
  if (typeof PROMPTS === 'undefined') return;
  const p = PROMPTS[key];
  if (!p) return;
  const inp = document.getElementById('prompt-input');
  const desc = document.getElementById('preset-desc');
  if (inp)  inp.value = p.template;
  if (desc) desc.textContent = p.description;
}

if (presetSelect && typeof PROMPTS !== 'undefined') {
  applyPreset(Object.keys(PROMPTS)[0]);
  presetSelect.addEventListener('change', () => applyPreset(presetSelect.value));
}

switchTab('script');
