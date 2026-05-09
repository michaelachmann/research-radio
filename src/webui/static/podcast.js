// ── State ─────────────────────────────────────────────────────────────────
let uploadId = null;
let evtSrc   = null;

// ── Upload ────────────────────────────────────────────────────────────────
const zone    = document.getElementById('drop-zone');
const fileInp = document.getElementById('pdf-input');

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
  if (!file.name.toLowerCase().endsWith('.pdf')) { setStatus('Please upload a PDF', true); return; }
  setStatus('Uploading…');
  const fd = new FormData(); fd.append('pdf', file);
  try {
    const res  = await fetch('/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) { setStatus('Error: ' + data.error, true); return; }
    uploadId = data.upload_id;
    zone.classList.add('loaded');
    zone.querySelector('p').textContent = data.filename + ' · ' + data.char_count.toLocaleString() + ' chars';
    document.getElementById('generate-btn').disabled = false;
    setStatus('Ready: ' + data.filename);
  } catch(e) { setStatus('Upload error: ' + e.message, true); }
}

// ── Generate ──────────────────────────────────────────────────────────────
document.getElementById('generate-btn').addEventListener('click', generate);

async function generate() {
  if (!uploadId) { setStatus('Upload a PDF first', true); return; }
  const prompt = document.getElementById('prompt-input').value.trim();
  if (!prompt)  { setStatus('Prompt is empty', true); return; }

  document.getElementById('output-area').value = '';
  document.getElementById('script-view').innerHTML = '<p class="script-empty">Generating…</p>';

  const btn = document.getElementById('generate-btn');
  btn.disabled = true; btn.textContent = 'Generating…'; btn.classList.add('streaming');
  setPill('streaming', thinkingToggle.checked ? 'Thinking…' : 'Generating');
  setStatus('Sending to ' + modelSelect.options[modelSelect.selectedIndex].text + '…');
  setMeta('');

  const t0 = Date.now();
  try {
    const res = await fetch('/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        upload_id: uploadId,
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
      // Streaming thinking mode — accumulate text
      const reader = res.body.getReader();
      const dec    = new TextDecoder();
      let   text   = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        text += dec.decode(value, { stream: true });
        document.getElementById('output-area').value = text;
        if (_currentTab === 'script') renderScript(parseScript(text));
      }
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      setMeta(text.length.toLocaleString() + ' chars · ' + elapsed + 's');
      setPill('done', 'Done');
      setStatus('Generated in ' + elapsed + 's');
      refreshCurl();
    } else {
      // Structured JSON mode
      const data = await res.json();
      if (data.error) { setStatus('Error: ' + data.error, true); setPill('error', 'Error'); return; }
      const turns = data.turns || [];
      const text  = turns.map(t => t.speaker + ': ' + t.text).join('\n');
      document.getElementById('output-area').value = text;
      renderScript(turns);
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      setMeta(turns.length + ' turns · ' + text.length.toLocaleString() + ' chars · ' + elapsed + 's');
      setPill('done', 'Done');
      setStatus('Generated ' + turns.length + ' turns in ' + elapsed + 's');
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
function renderScript(turns) {
  const view = document.getElementById('script-view');
  if (!turns.length) { view.innerHTML = '<p class="script-empty">No dialogue turns found</p>'; return; }
  view.innerHTML = turns.map(t => {
    const cls = t.speaker === 'Host' ? 'turn-host' : 'turn-cohost';
    const dialogue = escHtml(t.text).replace(/\[([^\]]+)\]/g, '<span class="el-tag">[$1]</span>');
    return `<div class="turn ${cls}"><span class="speaker-badge">${escHtml(t.speaker)}</span>${dialogue}</div>`;
  }).join('');
}

// ── Audio creation ────────────────────────────────────────────────────────
const audioBtn = document.getElementById('create-audio-btn');
audioBtn.dataset.label = audioBtn.textContent;
audioBtn.addEventListener('click', createAudio);

async function createAudio() {
  const script = document.getElementById('output-area').value.trim();
  if (!script) { setStatus('Generate a script first', true); return; }

  if (evtSrc) { evtSrc.close(); evtSrc = null; }
  document.getElementById('audio-log').textContent = '';
  document.getElementById('audio-download').style.display = 'none';
  audioBtn.disabled = true; audioBtn.classList.add('running');
  setAudioPill('running', 'Running');
  appendAudioLog('Starting…');

  try {
    const res  = await fetch('/audio/create', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ script }) });
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

// ── Script parsing ────────────────────────────────────────────────────────
function parseScript(text) {
  const turns = [];
  for (const line of text.split('\n')) {
    const l = line.trim();
    if      (l.startsWith(EL_CONFIG.hostName + ':'))   turns.push({ speaker: 'Host',   text: l.slice(EL_CONFIG.hostName.length + 1).trim() });
    else if (l.startsWith(EL_CONFIG.cohostName + ':')) turns.push({ speaker: 'Cohost', text: l.slice(EL_CONFIG.cohostName.length + 1).trim() });
    else if (l.startsWith('Host:'))   turns.push({ speaker: 'Host',   text: l.slice(5).trim() });
    else if (l.startsWith('Cohost:')) turns.push({ speaker: 'Cohost', text: l.slice(7).trim() });
  }
  return turns;
}

function chunkTurns(turns) {
  const chunks = []; let cur = [], len = 0;
  for (const t of turns) {
    if (cur.length && len + t.text.length > CHARS_PER_CHUNK) { chunks.push(cur); cur = []; len = 0; }
    cur.push(t); len += t.text.length;
  }
  if (cur.length) chunks.push(cur);
  return chunks;
}

// ── cURL builder ──────────────────────────────────────────────────────────
function buildCurl(chunk, outFile) {
  const inputs = chunk.map(t => ({
    text:     t.text,
    voice_id: t.speaker === 'Host'
      ? (EL_CONFIG.hostVoiceId   || 'HOST_VOICE_ID')
      : (EL_CONFIG.cohostVoiceId || 'COHOST_VOICE_ID'),
  }));
  const body = JSON.stringify({ model_id: 'eleven_v3', inputs });
  return `curl -s -X POST 'https://api.elevenlabs.io/v1/text-to-dialogue' \\\n` +
         `  -H 'xi-api-key: ${sqEscape(EL_CONFIG.apiKey || 'YOUR_API_KEY')}' \\\n` +
         `  -H 'Content-Type: application/json' \\\n` +
         `  -d '${sqEscape(body)}' \\\n` +
         `  --output '${outFile}'`;
}

function buildFFmpeg(n) {
  const list = Array.from({length: n}, (_, i) => `file 'chunk_${i}.mp3'`).join('\\n');
  return `printf '${list}' > list.txt && ffmpeg -y -f concat -safe 0 -i list.txt -c copy output.mp3`;
}

function refreshCurl() {
  const text = document.getElementById('output-area').value.trim();
  const box  = document.getElementById('curl-box');
  if (!text) { box.innerHTML = '<p id="curl-empty">Generate a script first to see cURL commands.</p>'; return; }

  const turns  = parseScript(text);
  const chunks = chunkTurns(turns);
  if (!chunks.length) { box.innerHTML = '<p id="curl-empty">No Host/Cohost turns found.</p>'; return; }

  const noVoices = !EL_CONFIG.hostVoiceId || !EL_CONFIG.cohostVoiceId;
  let html = noVoices ? '<div class="warn-bar">⚠ Voice IDs not set — set ELEVENLABS_HOST_VOICE_ID / ELEVENLABS_COHOST_VOICE_ID in .env</div>' : '';

  chunks.forEach((chunk, i) => {
    const outFile = chunks.length === 1 ? 'output.mp3' : `chunk_${i}.mp3`;
    const cmd     = buildCurl(chunk, outFile);
    const chars   = chunk.reduce((s, t) => s + t.text.length, 0);
    html += `<div class="chunk-block">
      <div class="chunk-hd">
        <span class="chunk-label">Chunk ${i+1}/${chunks.length} · ${chars.toLocaleString()} chars · ${chunk.length} turns</span>
        <button class="btn-sm" onclick="copyCode(this)">Copy</button>
      </div>
      <div class="code-box">${escHtml(cmd)}</div>
    </div>`;
  });

  if (chunks.length > 1) {
    html += `<div class="ffmpeg-block">
      <div class="ffmpeg-label curl-divider">ffmpeg concat</div>
      <div class="chunk-hd"><span class="chunk-label">Merge ${chunks.length} chunks</span><button class="btn-sm" onclick="copyCode(this)">Copy</button></div>
      <div class="code-box">${escHtml(buildFFmpeg(chunks.length))}</div>
    </div>`;
  }
  box.innerHTML = html;
}

document.getElementById('output-area').addEventListener('input', refreshCurl);
