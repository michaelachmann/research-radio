"""
Local web interface for rapid podcast script iteration.

Run with:  python -m src.web
Then open:  http://localhost:5000
"""

import json
import os
import queue
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

from src.pdf_extractor import extract_text_from_pdf
from src.tts_elevenlabs import ElevenLabsTTS
from config import (
    ANTHROPIC_API_KEY,
    AUDIO_DIR,
    CLAUDE_MODEL,
    ELEVENLABS_API_KEY,
    ELEVENLABS_HOST_VOICE_ID,
    ELEVENLABS_COHOST_VOICE_ID,
    TTS_HOST_NAME,
    TTS_COHOST_NAME,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

_paper_store: dict[str, dict] = {}
_job_store:   dict[str, dict] = {}

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "turns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string", "enum": ["Host", "Cohost"]},
                    "text":    {"type": "string"},
                },
                "required": ["speaker", "text"],
            },
        }
    },
    "required": ["turns"],
}

MODELS = [
    {"id": "claude-sonnet-4-6",        "label": "Sonnet 4.6",  "thinking": True},
    {"id": "claude-opus-4-7",           "label": "Opus 4.7",    "thinking": True},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5",   "thinking": False},
]

PROMPTS: dict[str, dict] = {
    "standard": {
        "label": "Standard Podcast (8–12 min)",
        "description": "Deep-dive two-host conversation with ElevenLabs markup",
        "template": (
            'You are a podcast script writer. Create an engaging episode of "Research Radio",\n'
            "a podcast featuring deep-dive discussions of recent academic papers in computational\n"
            "social science, platform studies, misinformation research, and the evolving landscape\n"
            "of social media and AI.\n\n"
            "The conversation is between two hosts:\n"
            "- Host (named Alex): guides the discussion, provides context\n"
            "- Cohost (named Sam): offers analysis, asks probing questions, adds perspective\n\n"
            "Important: These are podcast hosts discussing the paper — NOT the authors.\n"
            'Refer to authors in third person (e.g., "The researchers found..." or "According to the authors...").\n\n'
            "Guidelines:\n"
            "- Open by welcoming listeners to Research Radio; hosts introduce themselves by name, then introduce the paper's topic and authors\n"
            '- Mention authors by name naturally (e.g., "As Boyd argues..." or "The team led by Ferrara found...")\n'
            "- Explain key findings and methodology in accessible language\n"
            "- Both hosts share insights and build on each other's points\n"
            "- Discuss implications and significance for the field\n"
            "- End with clear takeaways for listeners\n"
            "- At the very end, remind listeners the full paper reference is in the episode description\n"
            "- Use natural, conversational language\n"
            "- Optionally use ElevenLabs delivery tags: [excited], [thoughtfully], [laughing], [sighs], [whispering] — sparingly\n"
            "- Target length: 8–12 minutes of dialogue (roughly 1,200–1,800 words)\n"
            '- Format EVERY line exactly as "Host: [dialogue]" or "Cohost: [dialogue]" — no other prefixes'
        ),
    },
    "teaser": {
        "label": "Short Teaser (2–3 min)",
        "description": "Hook-focused — one striking finding, why it matters, subscribe CTA",
        "template": (
            'You are a podcast script writer for "Research Radio". Write a short, punchy teaser\n'
            "episode (2–3 minutes, ~300–450 words) designed to hook listeners.\n\n"
            "Two hosts:\n"
            "- Host (Alex): sets up the premise\n"
            "- Cohost (Sam): reacts, delivers the key insight\n\n"
            "Guidelines:\n"
            "- Open with a surprising or counterintuitive finding to grab attention immediately\n"
            "- Briefly say who conducted the research and where\n"
            "- Highlight ONE or TWO most striking findings — nothing else\n"
            "- Close with: \"We'll dig deeper in the full episode — subscribe to Research Radio\"\n"
            "- Conversational, energetic tone — no methodology detail\n"
            "- ElevenLabs tags: [excited], [laughing], [thoughtfully] — sparingly\n"
            '- Format EVERY line exactly as "Host: [dialogue]" or "Cohost: [dialogue]"'
        ),
    },
    "deep_dive": {
        "label": "Technical Deep Dive (15–20 min)",
        "description": "Expert audience — methods, stats, limitations, open questions",
        "template": (
            'You are a podcast script writer for "Research Radio", targeting an audience of\n'
            "researchers and PhD students. Write a thorough, technically-engaged episode\n"
            "(15–20 minutes, ~2,200–3,000 words).\n\n"
            "Two hosts:\n"
            "- Host (Alex): leads, frames the academic context and literature\n"
            "- Cohost (Sam): probes methodology, challenges assumptions, discusses limitations\n\n"
            "Guidelines:\n"
            "- Introduce the paper and situate it in the literature\n"
            "- Explain the methodology in detail: dataset, design, statistical or computational approach\n"
            "- Walk through main findings with supporting evidence and effect sizes\n"
            "- Actively discuss limitations, confounds, and what the authors acknowledge\n"
            "- Discuss implications for the field and what future research this opens\n"
            "- Note open questions the paper raises but does not answer\n"
            "- Don't oversimplify — technical accuracy matters\n"
            "- ElevenLabs tags: [thoughtfully], [sighs], [excited] — sparingly\n"
            '- Format EVERY line exactly as "Host: [dialogue]" or "Cohost: [dialogue]"'
        ),
    },
    "explainer": {
        "label": "Plain Language Explainer (6–8 min)",
        "description": "General audience — no jargon, real-world stakes, accessible analogies",
        "template": (
            'You are a podcast script writer for "Research Radio". Write an accessible,\n'
            "jargon-free explainer episode (6–8 minutes, ~900–1,200 words) for a general\n"
            "educated audience with no research background.\n\n"
            "Two hosts:\n"
            "- Host (Alex): explains concepts plainly, uses analogies freely\n"
            '- Cohost (Sam): plays the curious non-expert — asks "but why does this matter?"\n\n'
            "Guidelines:\n"
            "- Start with the real-world question the paper answers (not 'researchers studied...')\n"
            "- Explain all technical terms immediately in plain language\n"
            "- Use everyday analogies\n"
            "- Focus on what the findings mean for society or everyday people\n"
            "- Avoid statistics unless they're striking and explainable without math\n"
            "- End with: what should listeners take away? What might change because of this research?\n"
            "- Warm, conversational tone\n"
            "- ElevenLabs tags: [excited], [laughing], [thoughtfully] — sparingly\n"
            '- Format EVERY line exactly as "Host: [dialogue]" or "Cohost: [dialogue]"'
        ),
    },
}


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _audio_worker(script: str, output_path: str, q: queue.Queue) -> None:
    try:
        tts   = ElevenLabsTTS()
        turns = tts.parse_script(script)
        if not turns:
            q.put({"type": "error", "msg": "No Host/Cohost dialogue found in script"})
            return

        chunks = tts.chunk_turns(turns)
        q.put({"type": "progress", "msg": f"Parsed {len(turns)} turns → {len(chunks)} API chunk(s)"})

        if not tts.voice_ids.get("Host") or not tts.voice_ids.get("Cohost"):
            q.put({"type": "error", "msg": "Voice IDs not set — add to .env and restart"})
            return

        q.put({"type": "progress", "msg": "Calling ElevenLabs Text-to-Dialogue…"})
        ok = tts.generate(script, output_path)

        if ok:
            size_kb = os.path.getsize(output_path) // 1024
            q.put({"type": "done", "filename": os.path.basename(output_path), "size_kb": size_kb})
        else:
            q.put({"type": "error", "msg": "Generation failed — check API key and voice IDs"})

    except Exception as e:
        q.put({"type": "error", "msg": str(e)})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    el_config = {
        "apiKey":       ELEVENLABS_API_KEY or "",
        "hostVoiceId":  ELEVENLABS_HOST_VOICE_ID or "",
        "cohostVoiceId": ELEVENLABS_COHOST_VOICE_ID or "",
        "hostName":     TTS_HOST_NAME,
        "cohostName":   TTS_COHOST_NAME,
    }
    page = _PAGE.replace('"__PROMPTS_JSON__"', json.dumps(PROMPTS))
    page = page.replace('"__EL_CONFIG__"',    json.dumps(el_config))
    page = page.replace('"__MODELS_JSON__"',  json.dumps(MODELS))
    page = page.replace('"__DEFAULT_MODEL__"', json.dumps(CLAUDE_MODEL))
    return page


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("pdf")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "File must be a .pdf"}), 400

    text = extract_text_from_pdf(f.read())
    if not text:
        return jsonify({"error": "Could not extract text from PDF"}), 400

    upload_id = str(uuid.uuid4())
    _paper_store[upload_id] = {
        "text":     text,
        "filename": f.filename,
        "title":    os.path.splitext(f.filename)[0],
    }
    return jsonify({"upload_id": upload_id, "filename": f.filename, "char_count": len(text)})


@app.route("/generate", methods=["POST"])
def generate():
    data         = request.json or {}
    upload_id    = data.get("upload_id")
    prompt_text  = (data.get("prompt") or "").strip()
    model        = data.get("model") or CLAUDE_MODEL
    use_thinking = bool(data.get("thinking"))

    paper = _paper_store.get(upload_id)
    if not paper:
        return jsonify({"error": "Upload not found — please re-upload the PDF"}), 404
    if not prompt_text:
        return jsonify({"error": "Prompt is empty"}), 400

    text = paper["text"]
    if len(text) > 60000:
        text = text[:60000] + "\n\n[Content truncated]"

    if use_thinking:
        # tool_choice + thinking are mutually exclusive in the API.
        # Stream plain text instead; append a strict format rule so the
        # (highly instruction-following) thinking model stays on format.
        FORMAT_RULE = (
            "\n\nFORMAT RULE — non-negotiable: output dialogue lines ONLY. "
            "Every line must be exactly 'Host: ...' or 'Cohost: ...'. "
            "No markdown, no headers, no blank labels, nothing else."
        )
        full_prompt = f"{prompt_text}{FORMAT_RULE}\n\nPaper Content:\n{text}"

        def stream_thinking():
            try:
                client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                # Opus 4+ uses adaptive thinking; older models use enabled+budget
                thinking_cfg = (
                    {"type": "adaptive"}
                    if "opus-4" in model
                    else {"type": "enabled", "budget_tokens": 10000}
                )
                with client.messages.stream(
                    model=model,
                    max_tokens=20000,
                    thinking=thinking_cfg,
                    messages=[{"role": "user", "content": full_prompt}],
                ) as stream:
                    for chunk in stream.text_stream:
                        yield chunk
            except Exception as e:
                traceback.print_exc()
                yield f"\n\n[Error: {e}]"

        return Response(stream_with_context(stream_thinking()), mimetype="text/plain; charset=utf-8")

    else:
        # Non-thinking: force structured JSON via tool_use — no markdown possible.
        full_prompt = f"{prompt_text}\n\nPaper Content:\n{text}"
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            response = client.messages.create(
                model=model,
                max_tokens=8192,
                temperature=0.7,
                tools=[{
                    "name":         "generate_script",
                    "description":  "Output the podcast script as structured dialogue turns.",
                    "input_schema": SCRIPT_SCHEMA,
                }],
                tool_choice={"type": "tool", "name": "generate_script"},
                messages=[{"role": "user", "content": full_prompt}],
            )
            for block in response.content:
                if block.type == "tool_use":
                    return jsonify({"turns": block.input.get("turns", [])})
            return jsonify({"error": "Model returned no structured output"}), 500

        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500


@app.route("/audio/create", methods=["POST"])
def audio_create():
    data   = request.json or {}
    script = (data.get("script") or "").strip()
    if not script:
        return jsonify({"error": "No script provided"}), 400
    if not ELEVENLABS_API_KEY:
        return jsonify({"error": "ELEVENLABS_API_KEY not set in .env"}), 400

    os.makedirs(AUDIO_DIR, exist_ok=True)
    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(AUDIO_DIR, f"script_{ts}.mp3")

    job_id = str(uuid.uuid4())
    q      = queue.Queue()
    _job_store[job_id] = {"queue": q}

    threading.Thread(target=_audio_worker, args=(script, out_path, q), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/audio/stream/<job_id>")
def audio_stream(job_id):
    job = _job_store.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    def event_gen():
        q = job["queue"]
        while True:
            try:
                msg = q.get(timeout=25)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["type"] in ("done", "error"):
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'

    return Response(
        stream_with_context(event_gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/audio/file/<filename>")
def audio_file(filename):
    safe = os.path.basename(filename)
    path = os.path.join(AUDIO_DIR, safe)
    if not os.path.exists(path):
        return "File not found", 404
    return send_file(path, mimetype="audio/mpeg", as_attachment=True, download_name=safe)


# ---------------------------------------------------------------------------
# Page (single-file, no external deps)
# ---------------------------------------------------------------------------

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Research Radio — Script Lab</title>
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

    body{
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
      background:#f1f5f9;color:#0f172a;
      height:100vh;display:flex;flex-direction:column;overflow:hidden;
    }

    /* ── Header ── */
    header{
      padding:.6rem 1.4rem;
      background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);
      border-bottom:1px solid #334155;
      display:flex;align-items:center;gap:.75rem;flex-shrink:0;
    }
    .header-logo{font-size:1.15rem;line-height:1}
    header h1{
      font-size:.95rem;font-weight:700;letter-spacing:-.015em;
      background:linear-gradient(90deg,#e2e8f0 30%,#818cf8 100%);
      -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
    }
    .header-badge{
      font-size:.63rem;background:rgba(129,140,248,.15);
      color:#a5b4fc;padding:.15rem .52rem;border-radius:999px;
      font-weight:600;border:1px solid rgba(129,140,248,.25);letter-spacing:.03em;
    }

    .workspace{flex:1;display:grid;grid-template-columns:300px 1fr 360px;overflow:hidden}

    /* ── Shared panel chrome ── */
    .panel{display:flex;flex-direction:column;overflow:hidden}
    .panel-hd{
      display:flex;align-items:center;justify-content:space-between;
      padding:.45rem .9rem;border-bottom:1px solid #e2e8f0;
      background:#f8fafc;flex-shrink:0;
    }
    .panel-hd h2{
      font-size:.63rem;font-weight:700;text-transform:uppercase;
      letter-spacing:.09em;color:#94a3b8;
    }
    .panel-bd{flex:1;overflow-y:auto;padding:.85rem;display:flex;flex-direction:column;gap:.8rem}

    /* ── Left panel ── */
    .panel-left{background:#fff;border-right:1px solid #e2e8f0}

    .section-label{
      display:block;font-size:.63rem;font-weight:700;
      text-transform:uppercase;letter-spacing:.08em;color:#94a3b8;margin-bottom:.3rem;
    }

    .upload-zone{
      border:2px dashed #cbd5e1;border-radius:10px;
      padding:1rem .75rem;text-align:center;cursor:pointer;
      transition:border-color .15s,background .15s;user-select:none;
    }
    .upload-zone:hover,.upload-zone.drag-over{border-color:#6366f1;background:#eef2ff}
    .upload-zone.loaded{border-color:#22c55e;background:#f0fdf4}
    .upload-zone input{display:none}
    .upload-icon{font-size:1.5rem;margin-bottom:.3rem}
    .upload-zone p{font-size:.77rem;color:#94a3b8}
    .upload-zone .file-name{font-weight:600;color:#0f172a;font-size:.82rem}
    .upload-zone .char-info{font-size:.67rem;color:#94a3b8;margin-top:.1rem}

    select{
      width:100%;padding:.38rem .55rem;border:1px solid #cbd5e1;border-radius:7px;
      font-size:.82rem;background:#fff;color:#0f172a;cursor:pointer;
    }
    select:focus{outline:none;border-color:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.12)}

    .preset-desc{font-size:.67rem;color:#94a3b8;margin-top:.22rem;min-height:.85rem}

    textarea{
      width:100%;padding:.55rem .65rem;border:1px solid #cbd5e1;border-radius:7px;
      font-size:.75rem;font-family:'SF Mono','Fira Code',monospace;
      line-height:1.65;resize:vertical;color:#0f172a;background:#fff;
    }
    textarea:focus{outline:none;border-color:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.12)}
    #prompt-input{flex:1;min-height:180px}

    .hint{font-size:.64rem;color:#cbd5e1}

    /* model row */
    .model-row{display:grid;grid-template-columns:1fr auto;align-items:end;gap:.6rem;flex-shrink:0}
    .thinking-col{display:flex;flex-direction:column;gap:.28rem}
    .toggle-wrap{display:flex;align-items:center;gap:.45rem;padding:.38rem 0}
    .toggle-wrap span{font-size:.78rem;color:#64748b}

    input[type=checkbox].toggle{
      width:1.8rem;height:1.05rem;appearance:none;-webkit-appearance:none;
      background:#cbd5e1;border-radius:999px;position:relative;cursor:pointer;
      transition:background .2s;flex-shrink:0;
    }
    input[type=checkbox].toggle:checked{background:#6366f1}
    input[type=checkbox].toggle::after{
      content:'';position:absolute;
      width:.82rem;height:.82rem;background:#fff;border-radius:50%;
      top:.11rem;left:.11rem;transition:transform .18s;
      box-shadow:0 1px 3px rgba(0,0,0,.2);
    }
    input[type=checkbox].toggle:checked::after{transform:translateX(.75rem)}
    input[type=checkbox].toggle:disabled{opacity:.35;cursor:not-allowed}

    .btn-generate{
      width:100%;padding:.6rem;
      background:linear-gradient(135deg,#6366f1,#4f46e5);
      color:#fff;border:none;border-radius:8px;font-size:.88rem;font-weight:600;
      cursor:pointer;transition:opacity .15s,filter .15s;flex-shrink:0;
      box-shadow:0 2px 8px rgba(99,102,241,.3);
    }
    .btn-generate:hover:not(:disabled){filter:brightness(1.08)}
    .btn-generate:disabled{opacity:.4;cursor:not-allowed;box-shadow:none}
    .btn-generate.streaming{
      background:linear-gradient(135deg,#38bdf8,#0ea5e9);
      box-shadow:0 2px 8px rgba(56,189,248,.3);
    }

    /* ── Middle panel ── */
    .panel-mid{background:#f8fafc;border-right:1px solid #e2e8f0}

    /* Tab bar */
    .tab-bar{
      display:flex;gap:.3rem;padding:.45rem .9rem;
      border-bottom:1px solid #e2e8f0;background:#f1f5f9;flex-shrink:0;
    }
    .tab-btn{
      padding:.28rem .75rem;font-size:.72rem;font-weight:600;
      border:1px solid transparent;border-radius:6px;
      color:#94a3b8;background:transparent;cursor:pointer;
      transition:color .12s,background .12s,border-color .12s;
    }
    .tab-btn.active{
      background:#fff;color:#6366f1;
      border-color:#e2e8f0;
      box-shadow:0 1px 3px rgba(0,0,0,.06);
    }
    .tab-btn:hover:not(.active){color:#64748b;background:rgba(255,255,255,.5)}

    /* Pill */
    .pill{font-size:.63rem;padding:.14rem .44rem;border-radius:999px;font-weight:600}
    .pill-idle    {background:#e2e8f0;color:#94a3b8}
    .pill-streaming{background:#fef9c3;color:#854d0e}
    .pill-done    {background:#dcfce7;color:#166534}
    .pill-error   {background:#fee2e2;color:#991b1b}
    .pill-running {background:#dbeafe;color:#1e40af}

    .btn-sm{
      padding:.26rem .65rem;background:#fff;border:1px solid #e2e8f0;
      border-radius:6px;font-size:.71rem;cursor:pointer;color:#374151;
      transition:background .1s,border-color .1s;white-space:nowrap;
    }
    .btn-sm:hover:not(:disabled){background:#f8fafc;border-color:#cbd5e1}
    .btn-sm:disabled{opacity:.4;cursor:not-allowed}

    /* Script view */
    #script-view{
      flex:1;background:#fff;border:1px solid #e2e8f0;border-radius:10px;
      padding:.8rem;overflow-y:auto;display:flex;flex-direction:column;gap:.5rem;
    }
    .script-empty{
      font-size:.8rem;color:#cbd5e1;text-align:center;
      padding:2.5rem 1rem;align-self:center;
    }

    /* Turn cards */
    .turn{
      display:flex;flex-direction:column;gap:.28rem;
      padding:.6rem .8rem .65rem;border-radius:9px;
      border-left:3px solid transparent;
    }
    .turn-host  {background:#eff6ff;border-left-color:#3b82f6}
    .turn-cohost{background:#f5f3ff;border-left-color:#8b5cf6}
    .speaker-badge{
      display:inline-flex;align-items:center;
      font-size:.58rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;
      padding:.13rem .48rem;border-radius:999px;width:fit-content;
    }
    .turn-host   .speaker-badge{background:#3b82f6;color:#fff}
    .turn-cohost .speaker-badge{background:#8b5cf6;color:#fff}
    .dialogue{font-size:.83rem;line-height:1.65;color:#1e293b}
    .el-tag{
      font-size:.7rem;font-weight:600;
      background:#ede9fe;color:#7c3aed;
      padding:.05rem .28rem;border-radius:4px;
      font-family:'SF Mono','Fira Code',monospace;
      white-space:nowrap;
    }

    #output-area{
      flex:1;font-family:'SF Mono','Fira Code',monospace;
      font-size:.75rem;line-height:1.7;
      background:#fff;border:1px solid #e2e8f0;border-radius:10px;
      padding:.8rem .95rem;resize:none;color:#1e293b;
    }
    #output-area:focus{outline:none;border-color:#6366f1}
    .output-meta{font-size:.66rem;color:#cbd5e1;text-align:right;flex-shrink:0}

    /* ── Right panel ── */
    .panel-right{background:#fff}

    .audio-section{
      border:1px solid #e2e8f0;border-radius:10px;
      padding:.8rem;display:flex;flex-direction:column;gap:.55rem;flex-shrink:0;
    }
    .audio-controls{display:flex;align-items:center;gap:.55rem}
    .btn-audio{
      flex:1;padding:.5rem .75rem;
      background:linear-gradient(135deg,#22c55e,#16a34a);
      color:#fff;border:none;border-radius:7px;
      font-size:.83rem;font-weight:600;cursor:pointer;
      transition:opacity .15s,filter .15s;
      box-shadow:0 2px 6px rgba(34,197,94,.25);
    }
    .btn-audio:hover:not(:disabled){filter:brightness(1.08)}
    .btn-audio:disabled{opacity:.4;cursor:not-allowed;box-shadow:none}
    .btn-audio.running{
      background:linear-gradient(135deg,#38bdf8,#0ea5e9);
      box-shadow:none;color:#fff;
    }

    .audio-log{
      background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;
      padding:.45rem .6rem;font-family:'SF Mono','Fira Code',monospace;
      font-size:.68rem;line-height:1.55;color:#64748b;
      max-height:80px;overflow-y:auto;
    }

    .download-link{
      display:inline-flex;align-items:center;gap:.35rem;
      padding:.42rem .75rem;
      background:#f0fdf4;border:1px solid #86efac;border-radius:7px;
      color:#166534;font-size:.79rem;font-weight:600;
      text-decoration:none;transition:background .12s;
    }
    .download-link:hover{background:#dcfce7}

    .curl-divider{
      font-size:.63rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
      color:#cbd5e1;display:flex;align-items:center;gap:.5rem;flex-shrink:0;
    }
    .curl-divider::after{content:'';flex:1;height:1px;background:#e2e8f0}

    .warn-bar{
      font-size:.71rem;color:#92400e;
      background:#fffbeb;border:1px solid #fde68a;
      border-radius:7px;padding:.38rem .6rem;
    }

    .chunk-block{flex-shrink:0}
    .chunk-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:.3rem}
    .chunk-label{font-size:.68rem;font-weight:600;color:#94a3b8}

    .code-box{
      background:#0f172a;color:#e2e8f0;border-radius:7px;
      padding:.65rem .75rem;font-family:'SF Mono','Fira Code',monospace;
      font-size:.67rem;line-height:1.55;white-space:pre;
      overflow-x:auto;max-height:200px;overflow-y:auto;
    }

    .ffmpeg-block{border-top:1px solid #e2e8f0;padding-top:.75rem;flex-shrink:0}
    .ffmpeg-label{font-size:.67rem;color:#94a3b8;margin-bottom:.28rem}

    #curl-empty{font-size:.79rem;color:#94a3b8;text-align:center;padding:1.5rem 1rem}

    .statusbar{
      padding:.28rem 1.4rem;font-size:.68rem;color:#94a3b8;
      background:#fff;border-top:1px solid #e2e8f0;flex-shrink:0;
    }
  </style>
</head>
<body>

<header>
  <span class="header-logo">🎙</span>
  <h1>Research Radio</h1>
  <span class="header-badge">Script Lab</span>
</header>

<div class="workspace">

  <!-- ── Left: controls ── -->
  <div class="panel panel-left">
    <div class="panel-hd"><h2>Setup</h2></div>
    <div class="panel-bd">

      <div>
        <label class="section-label">Paper (PDF)</label>
        <div class="upload-zone" id="upload-zone">
          <input type="file" id="pdf-input" accept=".pdf">
          <div class="upload-icon">📄</div>
          <p id="upload-hint">Click or drop a PDF here</p>
          <p id="file-name" class="file-name" style="display:none"></p>
          <p id="char-info" class="char-info" style="display:none"></p>
        </div>
      </div>

      <div>
        <label class="section-label" for="preset-select">Preset</label>
        <select id="preset-select"></select>
        <p class="preset-desc" id="preset-desc"></p>
      </div>

      <div style="flex:1;display:flex;flex-direction:column;gap:.28rem">
        <label class="section-label" for="prompt-input">
          Prompt <span style="font-weight:400;color:#cbd5e1;text-transform:none;letter-spacing:0">(editable)</span>
        </label>
        <textarea id="prompt-input" rows="11" placeholder="Select a preset or write a custom prompt…"></textarea>
        <p class="hint">Paper text is appended automatically.</p>
      </div>

      <div class="model-row">
        <div>
          <label class="section-label" for="model-select">Model</label>
          <select id="model-select"></select>
        </div>
        <div class="thinking-col">
          <label class="section-label">Thinking</label>
          <div class="toggle-wrap">
            <input type="checkbox" class="toggle" id="thinking-toggle" disabled>
            <span id="thinking-label">off</span>
          </div>
        </div>
      </div>

      <button class="btn-generate" id="generate-btn" disabled>Generate Script</button>

    </div>
  </div>

  <!-- ── Middle: script ── -->
  <div class="panel panel-mid">
    <div class="panel-hd">
      <h2>Generated Script</h2>
      <div style="display:flex;align-items:center;gap:.4rem">
        <span class="pill pill-idle" id="status-pill">Idle</span>
        <button class="btn-sm" onclick="copyOutput()">Copy</button>
        <button class="btn-sm" onclick="refreshCurl()">↻ cURL</button>
      </div>
    </div>
    <div class="tab-bar">
      <button class="tab-btn active" id="tab-script" onclick="switchTab('script')">Script</button>
      <button class="tab-btn" id="tab-raw" onclick="switchTab('raw')">Raw</button>
    </div>
    <div class="panel-bd">
      <div id="script-view">
        <p class="script-empty">Generate a script to see it here.</p>
      </div>
      <textarea id="output-area" placeholder="Generated script will appear here…" readonly style="display:none;flex:1"></textarea>
      <div class="output-meta" id="output-meta"></div>
    </div>
  </div>

  <!-- ── Right: ElevenLabs ── -->
  <div class="panel panel-right">
    <div class="panel-hd">
      <h2>ElevenLabs</h2>
      <button class="btn-sm" onclick="refreshCurl()">Refresh cURL</button>
    </div>
    <div class="panel-bd">

      <!-- Create Audio -->
      <div class="audio-section">
        <div class="audio-controls">
          <button class="btn-audio" id="create-audio-btn" onclick="createAudio()" disabled>▶ Create Audio</button>
          <span class="pill pill-idle" id="audio-pill" style="display:none"></span>
        </div>
        <div class="audio-log" id="audio-log" style="display:none"></div>
        <a id="audio-download" class="download-link" href="#" style="display:none" download>⬇ Download MP3</a>
      </div>

      <!-- cURL commands -->
      <div class="curl-divider">cURL Commands</div>
      <div id="curl-chunks">
        <p id="curl-empty">Generate a script to build curl commands.</p>
      </div>

    </div>
  </div>

</div>

<div class="statusbar" id="statusbar">Ready — upload a PDF to begin.</div>

<script>
const PROMPTS       = "__PROMPTS_JSON__";
const EL_CONFIG     = "__EL_CONFIG__";
const MODELS        = "__MODELS_JSON__";
const DEFAULT_MODEL = "__DEFAULT_MODEL__";

const CHARS_PER_CHUNK = 1800;

let uploadId    = null;
let _currentTab = 'script';

// ── Models ────────────────────────────────────────────────────
const modelSelect = document.getElementById('model-select');
MODELS.forEach(m => {
  const opt = document.createElement('option');
  opt.value = m.id; opt.textContent = m.label;
  if (m.id === DEFAULT_MODEL) opt.selected = true;
  modelSelect.appendChild(opt);
});

const thinkingToggle = document.getElementById('thinking-toggle');
const thinkingLabel  = document.getElementById('thinking-label');

function syncThinkingToggle() {
  const m = MODELS.find(x => x.id === modelSelect.value);
  thinkingToggle.disabled = !m?.thinking;
  if (!m?.thinking) thinkingToggle.checked = false;
  thinkingLabel.textContent = thinkingToggle.checked ? 'on' : 'off';
}
modelSelect.addEventListener('change', syncThinkingToggle);
thinkingToggle.addEventListener('change', () => {
  thinkingLabel.textContent = thinkingToggle.checked ? 'on' : 'off';
});
syncThinkingToggle();

// ── Presets ───────────────────────────────────────────────────
const presetSelect = document.getElementById('preset-select');
Object.entries(PROMPTS).forEach(([key, p]) => {
  const opt = document.createElement('option');
  opt.value = key; opt.textContent = p.label;
  presetSelect.appendChild(opt);
});
function applyPreset(key) {
  const p = PROMPTS[key]; if (!p) return;
  document.getElementById('prompt-input').value = p.template;
  document.getElementById('preset-desc').textContent = p.description;
}
applyPreset(Object.keys(PROMPTS)[0]);
presetSelect.addEventListener('change', () => applyPreset(presetSelect.value));

// ── Tab switcher ──────────────────────────────────────────────
function switchTab(tab) {
  _currentTab = tab;
  document.getElementById('tab-script').classList.toggle('active', tab === 'script');
  document.getElementById('tab-raw').classList.toggle('active', tab === 'raw');
  document.getElementById('script-view').style.display  = tab === 'script' ? '' : 'none';
  document.getElementById('output-area').style.display  = tab === 'raw'    ? '' : 'none';
}
switchTab('script');

// ── Script renderer ───────────────────────────────────────────
function renderScript(turns) {
  const view = document.getElementById('script-view');
  if (!turns.length) {
    view.innerHTML = '<p class="script-empty">No dialogue turns found.</p>';
    return;
  }
  view.innerHTML = turns.map(t => {
    const cls      = t.speaker === 'Host' ? 'turn-host' : 'turn-cohost';
    const dialogue = escHtml(t.text)
      .replace(/\[([^\]]+)\]/g, '<span class="el-tag">[$1]</span>');
    return `<div class="turn ${cls}">
      <span class="speaker-badge">${escHtml(t.speaker)}</span>
      <p class="dialogue">${dialogue}</p>
    </div>`;
  }).join('');
}

// ── Upload ────────────────────────────────────────────────────
const zone     = document.getElementById('upload-zone');
const pdfInput = document.getElementById('pdf-input');

zone.addEventListener('click', () => pdfInput.click());
zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
zone.addEventListener('drop', e => {
  e.preventDefault(); zone.classList.remove('drag-over');
  if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
});
pdfInput.addEventListener('change', () => { if (pdfInput.files[0]) handleFile(pdfInput.files[0]); });

async function handleFile(file) {
  if (!file.name.toLowerCase().endsWith('.pdf')) { setStatus('Please upload a .pdf file', true); return; }
  setStatus('Extracting PDF text…');
  const fd = new FormData(); fd.append('pdf', file);
  try {
    const res  = await fetch('/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) { setStatus('Error: ' + data.error, true); return; }
    uploadId = data.upload_id;
    zone.classList.add('loaded');
    document.getElementById('upload-hint').style.display = 'none';
    const fn = document.getElementById('file-name');
    fn.textContent = data.filename; fn.style.display = '';
    const ci = document.getElementById('char-info');
    ci.textContent = data.char_count.toLocaleString() + ' chars extracted'; ci.style.display = '';
    document.getElementById('generate-btn').disabled = false;
    setStatus('PDF loaded: ' + data.filename);
  } catch(e) { setStatus('Upload failed: ' + e.message, true); }
}

// ── Generate ──────────────────────────────────────────────────
document.getElementById('generate-btn').addEventListener('click', generate);

async function generate() {
  if (!uploadId) return;
  const prompt = document.getElementById('prompt-input').value.trim();
  if (!prompt) { setStatus('Prompt is empty', true); return; }

  const out = document.getElementById('output-area');
  out.value = ''; out.setAttribute('readonly', '');
  document.getElementById('script-view').innerHTML = '<p class="script-empty">Generating…</p>';

  const btn = document.getElementById('generate-btn');
  btn.disabled = true; btn.textContent = 'Generating…'; btn.classList.add('streaming');
  setPill('streaming', thinkingToggle.checked ? 'Thinking…' : 'Generating');
  setStatus('Sending to Claude ' + modelSelect.options[modelSelect.selectedIndex].text + '…');
  setMeta('');

  const t0 = Date.now();
  try {
    const res = await fetch('/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
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
      document.getElementById('script-view').innerHTML = '<p class="script-empty">Error — see status bar.</p>';
      return;
    }

    const ct = res.headers.get('Content-Type') || '';

    if (ct.includes('text/plain')) {
      // Thinking mode — stream raw text into Raw tab, render on completion
      switchTab('raw');
      out.removeAttribute('readonly');
      setPill('streaming', 'Streaming');
      setStatus('Receiving…');
      const reader = res.body.getReader(); const decoder = new TextDecoder();
      let total = 0;
      while (true) {
        const {done, value} = await reader.read(); if (done) break;
        const chunk = decoder.decode(value, {stream: true});
        out.value += chunk; out.scrollTop = out.scrollHeight;
        total += chunk.length; setMeta(total.toLocaleString() + ' chars');
      }
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      setPill('done', 'Done');
      setStatus(`Done in ${elapsed}s — ${total.toLocaleString()} chars`);
      setMeta(`${total.toLocaleString()} chars · ${elapsed}s`);
      const turns = parseScript(out.value);
      renderScript(turns);
      if (turns.length) switchTab('script');

    } else {
      // Non-thinking mode — structured JSON turns
      const data = await res.json();
      if (data.error) {
        setStatus('Error: ' + data.error, true); setPill('error', 'Error');
        document.getElementById('script-view').innerHTML = '<p class="script-empty">Error — see status bar.</p>';
        return;
      }
      const turns = data.turns || [];
      out.value = turns.map(t => `${t.speaker}: ${t.text}`).join('\n');
      out.removeAttribute('readonly');
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      setPill('done', 'Done');
      setStatus(`Done in ${elapsed}s — ${turns.length} turns`);
      setMeta(`${turns.length} turns · ${out.value.length.toLocaleString()} chars · ${elapsed}s`);
      renderScript(turns);
      switchTab('script');
    }

    document.getElementById('create-audio-btn').disabled = false;
    refreshCurl();

  } catch(e) {
    setStatus('Error: ' + e.message, true); setPill('error', 'Error');
  } finally {
    btn.disabled = false; btn.textContent = 'Generate Script'; btn.classList.remove('streaming');
  }
}

// ── Create Audio ──────────────────────────────────────────────
async function createAudio() {
  const script = document.getElementById('output-area').value.trim();
  if (!script) { setStatus('Generate a script first', true); return; }

  const btn = document.getElementById('create-audio-btn');
  btn.disabled = true; btn.classList.add('running'); btn.textContent = '⏳ Generating…';

  const logEl = document.getElementById('audio-log');
  logEl.style.display = ''; logEl.textContent = '';
  document.getElementById('audio-download').style.display = 'none';
  setAudioPill('running', 'Running');

  try {
    const res  = await fetch('/audio/create', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({script}),
    });
    const data = await res.json();
    if (data.error) {
      appendAudioLog('Error: ' + data.error);
      setAudioPill('error', 'Error');
      resetAudioBtn(); return;
    }

    const evtSrc = new EventSource('/audio/stream/' + data.job_id);
    evtSrc.onmessage = function(e) {
      const msg = JSON.parse(e.data);
      if (msg.type === 'progress') {
        appendAudioLog(msg.msg);
      } else if (msg.type === 'done') {
        appendAudioLog('Done · ' + msg.size_kb + ' KB');
        setAudioPill('done', 'Done');
        const dl = document.getElementById('audio-download');
        dl.href = '/audio/file/' + msg.filename;
        dl.download = msg.filename;
        dl.textContent = '⬇ Download ' + msg.filename;
        dl.style.display = '';
        resetAudioBtn(); evtSrc.close();
        setStatus('Audio saved: ' + msg.filename);
      } else if (msg.type === 'error') {
        appendAudioLog('Error: ' + msg.msg);
        setAudioPill('error', 'Error');
        resetAudioBtn(); evtSrc.close();
      }
    };
    evtSrc.onerror = function() {
      appendAudioLog('Connection lost');
      setAudioPill('error', 'Error');
      resetAudioBtn(); evtSrc.close();
    };
  } catch(e) {
    appendAudioLog('Failed: ' + e.message);
    setAudioPill('error', 'Error');
    resetAudioBtn();
  }
}

function resetAudioBtn() {
  const btn = document.getElementById('create-audio-btn');
  btn.disabled = false; btn.classList.remove('running'); btn.textContent = '▶ Create Audio';
}
function appendAudioLog(msg) {
  const el = document.getElementById('audio-log');
  el.textContent += (el.textContent ? '\n' : '') + msg;
  el.scrollTop = el.scrollHeight;
}
function setAudioPill(state, label) {
  const p = document.getElementById('audio-pill');
  p.style.display = ''; p.className = 'pill pill-' + state; p.textContent = label;
}

// ── cURL builder ──────────────────────────────────────────────
function parseScript(text) {
  const turns = [];
  for (const line of text.split('\n')) {
    const l = line.trim();
    if      (l.startsWith(EL_CONFIG.hostName + ':'))
      turns.push({speaker:'Host',   text: l.slice(EL_CONFIG.hostName.length + 1).trim()});
    else if (l.startsWith(EL_CONFIG.cohostName + ':'))
      turns.push({speaker:'Cohost', text: l.slice(EL_CONFIG.cohostName.length + 1).trim()});
    else if (l.startsWith('Host:'))
      turns.push({speaker:'Host',   text: l.slice(5).trim()});
    else if (l.startsWith('Cohost:'))
      turns.push({speaker:'Cohost', text: l.slice(7).trim()});
  }
  return turns;
}

function chunkTurns(turns) {
  const chunks = []; let cur = [], len = 0;
  for (const t of turns) {
    if (cur.length && len + t.text.length > CHARS_PER_CHUNK) {chunks.push(cur); cur = []; len = 0;}
    cur.push(t); len += t.text.length;
  }
  if (cur.length) chunks.push(cur);
  return chunks;
}

function sqEscape(s) { return s.replace(/'/g, "'\\''"); }

function buildCurl(chunk, outFile) {
  const inputs = chunk.map(t => ({
    text:     t.text,
    voice_id: t.speaker === 'Host'
      ? (EL_CONFIG.hostVoiceId   || 'HOST_VOICE_ID')
      : (EL_CONFIG.cohostVoiceId || 'COHOST_VOICE_ID'),
  }));
  const body = JSON.stringify({model_id: 'eleven_v3', inputs}, null, 2);
  const key  = EL_CONFIG.apiKey || 'YOUR_ELEVENLABS_API_KEY';
  return (
    `curl -s -X POST 'https://api.elevenlabs.io/v1/text-to-dialogue' \\\n` +
    `  -H 'xi-api-key: ${key}' \\\n` +
    `  -H 'Content-Type: application/json' \\\n` +
    `  --output ${outFile} \\\n` +
    `  -d '${sqEscape(body)}'`
  );
}

function buildFFmpeg(n) {
  const list = Array.from({length:n}, (_,i) => `file 'chunk_${i}.mp3'`).join('\n');
  return `# filelist.txt:\n${list}\n\nffmpeg -f concat -safe 0 -i filelist.txt -c copy output.mp3`;
}

function refreshCurl() {
  const script = document.getElementById('output-area').value.trim();
  const el     = document.getElementById('curl-chunks');
  if (!script) { el.innerHTML = '<p id="curl-empty">Generate a script to build curl commands.</p>'; return; }

  const turns = parseScript(script);
  if (!turns.length) { el.innerHTML = '<p id="curl-empty">No Host/Cohost lines found.</p>'; return; }

  const chunks = chunkTurns(turns);
  const missingVoices = !EL_CONFIG.hostVoiceId || !EL_CONFIG.cohostVoiceId;
  let html = missingVoices
    ? `<div class="warn-bar">Voice IDs not set — add ELEVENLABS_HOST_VOICE_ID / COHOST to .env and restart.</div>`
    : '';

  chunks.forEach((chunk, i) => {
    const outFile = chunks.length === 1 ? 'output.mp3' : `chunk_${i}.mp3`;
    const cmd = buildCurl(chunk, outFile);
    const chars = chunk.reduce((s,t) => s + t.text.length, 0);
    html += `<div class="chunk-block">
      <div class="chunk-hd">
        <span class="chunk-label">Chunk ${i+1}/${chunks.length} · ${chars.toLocaleString()} chars · ${chunk.length} turns</span>
        <button class="btn-sm" onclick="copyCode(this)">Copy</button>
      </div>
      <div class="code-box" data-cmd="${encodeURIComponent(cmd)}">${escHtml(cmd)}</div>
    </div>`;
  });

  if (chunks.length > 1) {
    const ff = buildFFmpeg(chunks.length);
    html += `<div class="ffmpeg-block">
      <div class="chunk-hd">
        <span class="ffmpeg-label">ffmpeg concat → output.mp3</span>
        <button class="btn-sm" onclick="copyCode(this)">Copy</button>
      </div>
      <div class="code-box" data-cmd="${encodeURIComponent(ff)}">${escHtml(ff)}</div>
    </div>`;
  }

  el.innerHTML = html;
}

function copyCode(btn) {
  const box  = btn.closest('.chunk-block,.ffmpeg-block').querySelector('.code-box');
  const text = decodeURIComponent(box.dataset.cmd);
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = 'Copy', 1500);
  });
}
function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function copyOutput() {
  const text = document.getElementById('output-area').value; if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    const btns = document.querySelectorAll('.panel-mid .btn-sm');
    btns[0].textContent = 'Copied!'; setTimeout(() => btns[0].textContent = 'Copy', 1500);
  });
}

function setStatus(msg, isError) {
  const bar = document.getElementById('statusbar');
  bar.textContent = msg; bar.style.color = isError ? '#b91c1c' : '#94a3b8';
}
function setPill(state, label) {
  const p = document.getElementById('status-pill'); p.className = 'pill pill-' + state; p.textContent = label;
}
function setMeta(text) { document.getElementById('output-meta').textContent = text; }
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
