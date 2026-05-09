// ── State ─────────────────────────────────────────────────────────────────
let uploadId = null;
let chapters = [];
let evtSrc   = null;

const CHARS_PER_MIN = 900;

// ── Upload ────────────────────────────────────────────────────────────────
const zone    = document.getElementById('drop-zone');
const fileInp = document.getElementById('epub-input');

zone.addEventListener('click',    () => fileInp.click());
zone.addEventListener('dragover',  e => { e.preventDefault(); zone.classList.add('hover'); });
zone.addEventListener('dragleave', ()  => zone.classList.remove('hover'));
zone.addEventListener('drop', e => {
  e.preventDefault();
  zone.classList.remove('hover');
  if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
});
fileInp.addEventListener('change', () => { if (fileInp.files[0]) handleFile(fileInp.files[0]); });

async function handleFile(file) {
  if (!file.name.toLowerCase().endsWith('.epub')) { setStatus('Please upload an .epub file', true); return; }
  setStatus('Parsing EPUB…');
  const fd = new FormData(); fd.append('epub', file);
  try {
    const res  = await fetch('/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) { setStatus('Error: ' + data.error, true); return; }
    uploadId = data.upload_id;
    chapters = data.chapters;
    zone.classList.add('loaded');
    zone.querySelector('p').textContent = data.filename + ' · ' + data.chapter_count + ' chapters';
    renderChapterList(chapters);
    document.getElementById('generate-btn').disabled = false;
    document.getElementById('batch-btn').disabled = false;
    setStatus('Loaded: ' + data.filename + ' — ' + data.chapter_count + ' chapters');
  } catch(e) { setStatus('Upload error: ' + e.message, true); }
}

// ── Chapter list ──────────────────────────────────────────────────────────
function readingTime(chars) {
  const mins = Math.max(1, Math.round(chars / CHARS_PER_MIN));
  if (mins < 60) return `~${mins} min`;
  const h = Math.floor(mins / 60), m = mins % 60;
  return m ? `~${h}h ${m}min` : `~${h}h`;
}

function renderChapterList(chs) {
  const list = document.getElementById('chapter-list');
  list.innerHTML = chs.map((ch, i) => `
    <div class="chapter-item" id="chi-${i}" onclick="toggleChapter(${i})">
      <input type="checkbox" class="chapter-cb" id="chk-${i}" onclick="event.stopPropagation();toggleChapter(${i})">
      <div class="chapter-body">
        <div class="chapter-title">${escHtml(ch.title)}</div>
        <div class="chapter-meta">${ch.char_count.toLocaleString()} chars · ${readingTime(ch.char_count)}</div>
      </div>
    </div>`).join('');
}

function toggleChapter(i) {
  const cb   = document.getElementById('chk-' + i);
  const item = document.getElementById('chi-' + i);
  cb.checked = !cb.checked;
  item.classList.toggle('selected', cb.checked);
}

function selectAll()  { chapters.forEach((_, i) => { document.getElementById('chk-'+i).checked = true;  document.getElementById('chi-'+i).classList.add('selected'); }); }
function selectNone() { chapters.forEach((_, i) => { document.getElementById('chk-'+i).checked = false; document.getElementById('chi-'+i).classList.remove('selected'); }); }

function getSelectedChapters() {
  return chapters.filter((_, i) => document.getElementById('chk-' + i)?.checked);
}

// ── Generate ──────────────────────────────────────────────────────────────
document.getElementById('generate-btn').addEventListener('click', generate);

async function generate() {
  const sel = getSelectedChapters();
  if (!sel.length) { setStatus('Select at least one chapter', true); return; }
  const chapter = sel[0];
  const prompt  = document.getElementById('prompt-input').value.trim();
  if (!prompt)  { setStatus('Prompt is empty', true); return; }

  document.getElementById('output-area').value = '';
  document.getElementById('script-view').innerHTML = '<p class="script-empty">Generating…</p>';

  const btn = document.getElementById('generate-btn');
  btn.disabled = true; btn.textContent = 'Generating…'; btn.classList.add('streaming');
  setPill('streaming', thinkingToggle.checked ? 'Thinking…' : 'Generating');
  setStatus(`Generating: "${chapter.title}"…`);
  setMeta('');

  const t0 = Date.now();
  try {
    const res = await fetch('/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        upload_id:  uploadId,
        chapter_id: chapter.id,
        prompt,
        model:    modelSelect.value,
        thinking: thinkingToggle.checked,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      setStatus('Error: ' + (err.error || res.statusText), true);
      setPill('error', 'Error');
      return;
    }

    const ct = res.headers.get('Content-Type') || '';
    if (ct.includes('text/plain')) {
      const reader = res.body.getReader();
      const dec    = new TextDecoder();
      let   text   = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        text += dec.decode(value, { stream: true });
        document.getElementById('output-area').value = text;
        if (_currentTab === 'script') renderScript(text);
      }
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      setMeta(text.length.toLocaleString() + ' chars · ' + elapsed + 's');
      setPill('done', 'Done');
      setStatus('Generated in ' + elapsed + 's');
      refreshCurl();
    } else {
      const data = await res.json();
      if (data.error) { setStatus('Error: ' + data.error, true); setPill('error', 'Error'); return; }
      const text = data.script || '';
      document.getElementById('output-area').value = text;
      renderScript(text);
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      setMeta(text.length.toLocaleString() + ' chars · ' + elapsed + 's');
      setPill('done', 'Done');
      setStatus('Generated in ' + elapsed + 's');
      refreshCurl();
    }
  } catch(e) {
    setStatus('Error: ' + e.message, true);
    setPill('error', 'Error');
  } finally {
    btn.disabled = false; btn.textContent = 'Generate Script'; btn.classList.remove('streaming');
  }
}

// ── Script rendering ──────────────────────────────────────────────────────
function renderScript(text) {
  const view = document.getElementById('script-view');
  if (!text.trim()) { view.innerHTML = '<p class="script-empty">No content</p>'; return; }
  const paras = text.split(/\n\n+/).map(p => p.trim()).filter(Boolean);
  view.innerHTML = paras.map(p => {
    const isHeading = p.length < 80 && !p.includes('.');
    if (isHeading) return `<div class="narration-heading">${escHtml(p)}</div>`;
    const formatted = escHtml(p).replace(/\[([^\]]+)\]/g, '<span class="el-tag">[$1]</span>');
    return `<div class="narration-para">${formatted}</div>`;
  }).join('');
}

// ── Audio creation ────────────────────────────────────────────────────────
const audioBtn = document.getElementById('create-audio-btn');
audioBtn.dataset.label = audioBtn.textContent;
audioBtn.addEventListener('click', createAudio);

async function createAudio() {
  const script  = document.getElementById('output-area').value.trim();
  const voiceId = document.getElementById('voice-id-input').value.trim() || EL_CONFIG.narratorVoiceId;
  if (!script)  { setStatus('Generate a script first', true); return; }
  if (!voiceId) { setStatus('Enter a narrator voice ID', true); return; }

  if (evtSrc) { evtSrc.close(); evtSrc = null; }
  document.getElementById('audio-log').textContent = '';
  document.getElementById('audio-download').style.display = 'none';
  audioBtn.disabled = true; audioBtn.classList.add('running');
  setAudioPill('running', 'Running');
  appendAudioLog('Starting…');

  try {
    const res  = await fetch('/audio/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ script, voice_id: voiceId }),
    });
    const data = await res.json();
    if (data.error) { appendAudioLog('Error: ' + data.error); setAudioPill('error', 'Error'); resetAudioBtn(); return; }

    evtSrc = new EventSource('/audio/stream/' + data.job_id);
    evtSrc.onmessage = function(e) {
      const msg = JSON.parse(e.data);
      if (msg.type === 'progress') {
        appendAudioLog(msg.msg);
      } else if (msg.type === 'done') {
        appendAudioLog('Done · ' + msg.size_kb + ' KB');
        setAudioPill('done', 'Done');
        const dl = document.getElementById('audio-download');
        dl.href = '/audio/file/' + msg.filename;
        dl.textContent = '⬇ ' + msg.filename + ' (' + msg.size_kb + ' KB)';
        dl.style.display = 'block';
        resetAudioBtn(); evtSrc.close(); evtSrc = null;
      } else if (msg.type === 'error') {
        appendAudioLog('Error: ' + msg.msg);
        setAudioPill('error', 'Error');
        resetAudioBtn(); evtSrc.close(); evtSrc = null;
      }
    };
    evtSrc.onerror = function() {
      appendAudioLog('Stream disconnected');
      setAudioPill('error', 'Error');
      resetAudioBtn(); evtSrc.close(); evtSrc = null;
    };
  } catch(e) {
    appendAudioLog('Error: ' + e.message);
    setAudioPill('error', 'Error');
    resetAudioBtn();
  }
}

// ── Batch processing ──────────────────────────────────────────────────────
document.getElementById('batch-btn').addEventListener('click', batchProcess);

let batchEvtSrc = null;

async function batchProcess() {
  const sel     = getSelectedChapters();
  if (!sel.length) { setStatus('Select at least one chapter', true); return; }
  const voiceId = document.getElementById('voice-id-input').value.trim() || EL_CONFIG.narratorVoiceId;
  const prompt  = document.getElementById('prompt-input').value.trim();
  if (!voiceId) { setStatus('Enter a narrator voice ID', true); return; }
  if (!prompt)  { setStatus('Prompt is empty', true); return; }

  const batchSection = document.getElementById('batch-section');
  batchSection.style.display = 'flex';
  renderBatchItems(sel);
  document.getElementById('batch-pill').className = 'pill pill-running';
  document.getElementById('batch-pill').textContent = `0 / ${sel.length}`;

  const btn = document.getElementById('batch-btn');
  btn.disabled = true;
  setPill('running', 'Batch');

  if (batchEvtSrc) { batchEvtSrc.close(); batchEvtSrc = null; }

  try {
    const res = await fetch('/batch/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        upload_id:   uploadId,
        chapter_ids: sel.map(c => c.id),
        voice_id:    voiceId,
        prompt,
        model:       modelSelect.value,
      }),
    });
    const data = await res.json();
    if (data.error) { setStatus('Error: ' + data.error, true); btn.disabled = false; return; }

    let doneCount = 0;
    batchEvtSrc = new EventSource('/batch/stream/' + data.job_id);
    batchEvtSrc.onmessage = function(e) {
      const msg = JSON.parse(e.data);
      if (msg.type === 'chapter_start') {
        updateBatchItem(msg.idx, 'running', 'Generating script…');
      } else if (msg.type === 'script_done') {
        updateBatchItem(msg.idx, 'running', 'Converting to audio…');
      } else if (msg.type === 'audio_progress') {
        updateBatchItem(msg.idx, 'running', msg.msg);
      } else if (msg.type === 'chapter_done') {
        doneCount++;
        document.getElementById('batch-pill').textContent = `${doneCount} / ${sel.length}`;
        updateBatchItem(msg.idx, 'done', `Done · ${msg.size_kb} KB`);
        addBatchDownload(msg.idx, msg.filename, msg.size_kb);
      } else if (msg.type === 'chapter_error') {
        updateBatchItem(msg.idx, 'error', msg.msg);
      } else if (msg.type === 'all_done') {
        document.getElementById('batch-pill').className = 'pill pill-done';
        document.getElementById('batch-pill').textContent = `${doneCount} / ${sel.length} done`;
        setPill('done', 'Done');
        setStatus(`Batch complete: ${doneCount} of ${sel.length} chapters`);
        btn.disabled = false;
        btn.textContent = '⚡ Batch: Generate + Convert All';
        batchEvtSrc.close(); batchEvtSrc = null;
      } else if (msg.type === 'error') {
        setStatus('Batch error: ' + msg.msg, true);
        setPill('error', 'Error');
        btn.disabled = false;
        batchEvtSrc.close(); batchEvtSrc = null;
      }
    };
    batchEvtSrc.onerror = function() {
      setStatus('Batch stream disconnected', true);
      btn.disabled = false;
      btn.textContent = '⚡ Batch: Generate + Convert All';
      batchEvtSrc.close(); batchEvtSrc = null;
    };
  } catch(e) {
    setStatus('Error: ' + e.message, true);
    setPill('error', 'Error');
    btn.disabled = false;
  }
}

function renderBatchItems(chs) {
  const list = document.getElementById('batch-items');
  list.innerHTML = chs.map((ch, i) => `
    <div class="batch-item" id="bi-${i}">
      <div class="batch-item-hd">
        <span class="batch-item-title">${escHtml(ch.title)}</span>
        <span class="pill pill-idle" id="bip-${i}">Pending</span>
      </div>
      <div class="batch-item-msg" id="bim-${i}"></div>
    </div>`).join('');
}

function updateBatchItem(idx, state, msg) {
  const pill = document.getElementById('bip-' + idx);
  const msgEl = document.getElementById('bim-' + idx);
  const item  = document.getElementById('bi-'  + idx);
  if (pill)  { pill.className = 'pill pill-' + state; pill.textContent = state === 'running' ? '…' : state === 'done' ? 'Done' : 'Error'; }
  if (msgEl) msgEl.textContent = msg;
  if (item)  { item.className = 'batch-item' + (state === 'done' ? ' done' : state === 'error' ? ' error' : ''); }
}

function addBatchDownload(idx, filename, sizeKb) {
  const msgEl = document.getElementById('bim-' + idx);
  if (msgEl) msgEl.innerHTML = `<a class="batch-dl" href="/audio/file/${filename}" download>⬇ ${filename} (${sizeKb} KB)</a>`;
}

// ── cURL builder (single-voice TTS) ──────────────────────────────────────
function chunkScript(text) {
  const paras = text.split(/\n\n+/).map(p => p.trim()).filter(Boolean);
  const chunks = []; let cur = ''; const MAX = CHARS_PER_CHUNK;
  for (const p of paras) {
    if (cur && cur.length + p.length + 2 > MAX) { chunks.push(cur); cur = ''; }
    cur = cur ? cur + '\n\n' + p : p;
  }
  if (cur) chunks.push(cur);
  return chunks.length ? chunks : [text];
}

function buildCurlAudio(text, voiceId, outFile) {
  const body = JSON.stringify({ text, model_id: 'eleven_v3' });
  return `curl -s -X POST 'https://api.elevenlabs.io/v1/text-to-speech/${sqEscape(voiceId || 'VOICE_ID')}' \\\n` +
         `  -H 'xi-api-key: ${sqEscape(EL_CONFIG.apiKey || 'YOUR_API_KEY')}' \\\n` +
         `  -H 'Content-Type: application/json' \\\n` +
         `  -d '${sqEscape(body)}' \\\n` +
         `  --output '${outFile}'`;
}

function buildFFmpeg(n) {
  const list = Array.from({length: n}, (_, i) => `file 'part_${i}.mp3'`).join('\\n');
  return `printf '${list}' > list.txt && ffmpeg -y -f concat -safe 0 -i list.txt -c copy chapter.mp3`;
}

function refreshCurl() {
  const text    = document.getElementById('output-area').value.trim();
  const voiceId = document.getElementById('voice-id-input').value.trim() || EL_CONFIG.narratorVoiceId;
  const box     = document.getElementById('curl-box');
  if (!text) { box.innerHTML = '<p id="curl-empty">Generate a script first to see cURL commands.</p>'; return; }

  const chunks = chunkScript(text);
  const single = chunks.length === 1;
  let html = (!voiceId)
    ? '<div class="warn-bar">⚠ No narrator voice ID — enter one above or set ELEVENLABS_HOST_VOICE_ID in .env</div>'
    : '';

  chunks.forEach((chunk, i) => {
    const outFile = single ? 'chapter.mp3' : `part_${i}.mp3`;
    const cmd = buildCurlAudio(chunk, voiceId, outFile);
    html += `<div class="chunk-block">
      <div class="chunk-hd">
        <span class="chunk-label">Chunk ${i+1}/${chunks.length} · ${chunk.length.toLocaleString()} chars</span>
        <button class="btn-sm" onclick="copyCode(this)">Copy</button>
      </div>
      <div class="code-box">${escHtml(cmd)}</div>
    </div>`;
  });

  if (chunks.length > 1) {
    html += `<div class="ffmpeg-block">
      <div class="ffmpeg-label curl-divider">ffmpeg concat</div>
      <div class="chunk-hd"><span class="chunk-label">Merge ${chunks.length} parts</span><button class="btn-sm" onclick="copyCode(this)">Copy</button></div>
      <div class="code-box">${escHtml(buildFFmpeg(chunks.length))}</div>
    </div>`;
  }
  box.innerHTML = html;
}

document.getElementById('output-area').addEventListener('input', refreshCurl);
document.getElementById('voice-id-input').addEventListener('input', refreshCurl);
