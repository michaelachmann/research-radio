"""
Local web interface for rapid podcast script iteration.

Run with:  python -m src.webui.podcast
Then open:  http://localhost:5000
"""

import os
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import anthropic
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

from src.pdf_extractor import extract_text_from_pdf
from src.tts_elevenlabs import ElevenLabsTTS
from src.webui.shared import MODELS, create_job, event_gen, get_job
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

_HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(_HERE, "templates"),
    static_folder=os.path.join(_HERE, "static"),
)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

_paper_store: dict[str, dict] = {}

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
# Background TTS worker
# ---------------------------------------------------------------------------

def _audio_worker(script: str, output_path: str, q) -> None:
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
    import json
    el_config = {
        "apiKey":        ELEVENLABS_API_KEY or "",
        "hostVoiceId":   ELEVENLABS_HOST_VOICE_ID or "",
        "cohostVoiceId": ELEVENLABS_COHOST_VOICE_ID or "",
        "hostName":      TTS_HOST_NAME,
        "cohostName":    TTS_COHOST_NAME,
    }
    return render_template(
        "podcast.html",
        prompts_json=json.dumps(PROMPTS),
        el_config_json=json.dumps(el_config),
        models_json=json.dumps(MODELS),
        default_model=CLAUDE_MODEL,
    )


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
    _paper_store[upload_id] = {"text": text, "filename": f.filename}
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
        FORMAT_RULE = (
            "\n\nFORMAT RULE — non-negotiable: output dialogue lines ONLY. "
            "Every line must be exactly 'Host: ...' or 'Cohost: ...'. "
            "No markdown, no headers, no blank labels, nothing else."
        )
        full_prompt = f"{prompt_text}{FORMAT_RULE}\n\nPaper Content:\n{text}"

        def stream_thinking():
            try:
                client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
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

    job_id, q = create_job()
    threading.Thread(target=_audio_worker, args=(script, out_path, q), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/audio/stream/<job_id>")
def audio_stream(job_id):
    if not get_job(job_id):
        return jsonify({"error": "Job not found"}), 404
    return Response(
        stream_with_context(event_gen(job_id)),
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
