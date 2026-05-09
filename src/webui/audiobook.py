"""
Local web interface for EPUB-to-audiobook conversion.

Run with:  python -m src.webui.audiobook
Then open:  http://localhost:5001
"""

import io
import json
import os
import queue
import re
import sys
import threading
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import anthropic
import requests
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 is required.  pip install beautifulsoup4", file=sys.stderr)
    sys.exit(1)

from src.tts_elevenlabs import concat_mp3s
from src.webui.shared import MODELS, create_job, event_gen, get_job
from config import (
    ANTHROPIC_API_KEY,
    AUDIO_DIR,
    CLAUDE_MODEL,
    ELEVENLABS_API_KEY,
    ELEVENLABS_HOST_VOICE_ID,
)

AUDIOBOOK_DIR = os.path.join(AUDIO_DIR, "audiobook")

_HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(_HERE, "templates"),
    static_folder=os.path.join(_HERE, "static"),
)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

_book_store: dict[str, dict] = {}

NARRATION_SCHEMA = {
    "type": "object",
    "properties": {
        "script": {
            "type": "string",
            "description": "Complete narration script with ElevenLabs expression tags",
        }
    },
    "required": ["script"],
}

PROMPTS: dict[str, dict] = {
    "faithful": {
        "label": "Faithful Narration",
        "description": "Preserve all content with light pacing tags for clean audiobook narration",
        "template": (
            "You are preparing an audiobook narration script.\n\n"
            "Format the following chapter text for ElevenLabs text-to-speech narration "
            "by a single narrator voice.\n\n"
            "Guidelines:\n"
            "- Preserve ALL content faithfully — no summarizing, no omissions\n"
            "- Clean up OCR artifacts and ebook formatting noise "
            "(remove repeated page headers, page numbers, etc.)\n"
            "- Organize into natural reading paragraphs\n"
            "- Add ElevenLabs expression tags SPARINGLY where they genuinely improve narration:\n"
            "  [slowly] — solemn, heavy, or important moments\n"
            "  [fast] — excited or rushed speech in dialogue\n"
            "  [whispers] — whispered speech or secrets\n"
            "  [sighs] — sighing moments\n"
            "  [laughing] — laughter\n"
            "  [excited] — surprise, high energy\n"
            "  [thoughtfully] — reflection, careful weighing\n"
            "- Output ONLY the narration text with tags — "
            "no chapter headers, no labels, no meta-commentary"
        ),
    },
    "expressive": {
        "label": "Expressive Narration",
        "description": "Richer expression tags to bring characters and scenes to life",
        "template": (
            "You are preparing an expressive audiobook narration script.\n\n"
            "Format the following chapter text for ElevenLabs text-to-speech narration "
            "with rich, dramatic delivery that brings every scene to life.\n\n"
            "Guidelines:\n"
            "- Preserve ALL content faithfully\n"
            "- Clean up OCR/ebook formatting artifacts\n"
            "- Add ElevenLabs expression tags generously at emotionally appropriate moments:\n"
            "  [slowly] — solemn builds, heavy moments, slow tension\n"
            "  [fast] — rushing thoughts, action sequences, excited dialogue\n"
            "  [whispers] — secrets, fear, intimate moments\n"
            "  [sighs] — weariness, resignation, relief\n"
            "  [laughing] — joy, amusement\n"
            "  [excited] — revelation, surprise, delight\n"
            "  [thoughtfully] — deep reflection, careful consideration\n"
            "- Match each piece of dialogue to the character's emotional state\n"
            "- Place tags at the START of the passage they apply to\n"
            "- Output ONLY the narration text with tags — no chapter headers, no meta-commentary"
        ),
    },
    "german": {
        "label": "German Translation",
        "description": "Translate English to German and format as audiobook narration",
        "template": (
            "You are preparing a German audiobook narration script from English source material.\n\n"
            "Step 1 — Translate the chapter text from English to German:\n"
            "- Produce a natural, literary German translation — not a literal word-for-word version\n"
            "- Preserve the author's style, tone, and register\n"
            "- Use proper German typography: „Anführungszeichen“ for dialogue\n"
            "- Clean up any OCR artifacts or ebook formatting noise\n\n"
            "Step 2 — Format the German text for ElevenLabs narration:\n"
            "- Add expression tags SPARINGLY where they improve the listening experience:\n"
            "  [slowly], [fast], [whispers], [sighs], [laughing], [excited], [thoughtfully]\n"
            "- Organize into natural reading paragraphs\n\n"
            "Output ONLY the final German narration text with tags — "
            "no English text, no headers, no meta-commentary"
        ),
    },
    "german_expressive": {
        "label": "German — Expressive",
        "description": "Literary German translation with rich expressive delivery tags",
        "template": (
            "You are preparing an expressive German audiobook narration script from English source.\n\n"
            "Step 1 — Translate to natural, literary German:\n"
            "- Preserve the author's style, tone, and emotional register\n"
            "- Use „German quotation marks“ for all dialogue\n"
            "- Clean up OCR artifacts and ebook formatting noise\n\n"
            "Step 2 — Add rich ElevenLabs expression tags throughout:\n"
            "Use [slowly], [fast], [whispers], [sighs], [laughing], [excited], [thoughtfully] "
            "generously to bring characters and scenes to life. "
            "Match each dialogue passage to the character's emotional state.\n\n"
            "Output ONLY the final German narration text with tags — "
            "no English, no headers, no meta-commentary"
        ),
    },
}


# ---------------------------------------------------------------------------
# EPUB parsing
# ---------------------------------------------------------------------------

def _parse_epub(file_bytes: bytes) -> list[dict]:
    chapters = []
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
        names = set(z.namelist())
        opf_path = None

        if "META-INF/container.xml" in names:
            try:
                root = ET.fromstring(z.read("META-INF/container.xml"))
                for el in root.iter():
                    if el.tag.endswith("rootfile"):
                        opf_path = el.get("full-path")
                        break
            except Exception:
                pass

        if not opf_path:
            opf_path = next((n for n in names if n.endswith(".opf")), None)

        spine_paths: list[str] = []
        ncx_titles: dict[str, str] = {}

        if opf_path and opf_path in names:
            try:
                opf_xml = z.read(opf_path).decode("utf-8", errors="replace")
                root = ET.fromstring(opf_xml)
                tag = root.tag
                ns = tag[: tag.index("}") + 1] if tag.startswith("{") else ""
                opf_dir = "/".join(opf_path.split("/")[:-1])

                manifest: dict[str, dict] = {}
                for el in root.iter(f"{ns}item"):
                    mid = el.get("id")
                    href = el.get("href", "")
                    media = el.get("media-type", "")
                    if mid and href:
                        full = (opf_dir + "/" + href).lstrip("/") if opf_dir else href
                        manifest[mid] = {"href": full, "media": media}

                for el in root.iter(f"{ns}itemref"):
                    idref = el.get("idref")
                    if idref and idref in manifest:
                        info = manifest[idref]
                        if info["media"] in ("application/xhtml+xml", "text/html", ""):
                            spine_paths.append(info["href"])

                ncx_item = next(
                    (v for v in manifest.values() if "dtbncx" in v["media"]), None
                )
                if ncx_item and ncx_item["href"] in names:
                    try:
                        ncx_root = ET.fromstring(
                            z.read(ncx_item["href"]).decode("utf-8", errors="replace")
                        )
                        nns = ""
                        if ncx_root.tag.startswith("{"):
                            nns = ncx_root.tag[: ncx_root.tag.index("}") + 1]
                        for navpoint in ncx_root.iter(f"{nns}navPoint"):
                            label_el = navpoint.find(f".//{nns}text")
                            content_el = navpoint.find(f"{nns}content")
                            if label_el is not None and content_el is not None:
                                src = content_el.get("src", "").split("#")[0]
                                if opf_dir:
                                    src = (opf_dir + "/" + src).lstrip("/")
                                if label_el.text:
                                    ncx_titles[src] = label_el.text.strip()
                    except Exception:
                        pass
            except Exception:
                pass

        if not spine_paths:
            spine_paths = sorted(
                n for n in names
                if n.lower().endswith((".html", ".htm", ".xhtml")) and "META-INF" not in n
            )

        seen: set[str] = set()
        for i, path in enumerate(spine_paths):
            if path in seen or path not in names:
                continue
            seen.add(path)
            try:
                html_text = z.read(path).decode("utf-8", errors="replace")
                soup = BeautifulSoup(html_text, "html.parser")
                for tag in soup.find_all(["nav", "header", "footer", "aside", "script", "style"]):
                    tag.decompose()

                title = ncx_titles.get(path)
                if not title:
                    for ht in ("h1", "h2", "h3"):
                        el = soup.find(ht)
                        if el and el.get_text().strip():
                            title = el.get_text().strip()[:80]
                            break
                if not title:
                    t = soup.find("title")
                    if t and t.get_text().strip():
                        title = t.get_text().strip()[:80]
                if not title:
                    title = os.path.splitext(os.path.basename(path))[0]

                text = soup.get_text(separator="\n", strip=True)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
                if len(text) < 150:
                    continue

                chapters.append({
                    "id":         f"ch_{i}_{re.sub(r'[^a-z0-9]', '_', os.path.basename(path).lower())}",
                    "title":      title,
                    "text":       text,
                    "char_count": len(text),
                })
            except Exception:
                continue

    return chapters


# ---------------------------------------------------------------------------
# TTS helpers
# ---------------------------------------------------------------------------

def _chunk_text(text: str, max_chars: int = 4000) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if current and current_len + len(para) + 2 > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks or [text]


def _call_tts(text: str, voice_id: str, output_path: str) -> tuple[bool, str]:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers,
                          json={"text": text, "model_id": "eleven_v3"}, timeout=120)
        if r.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(r.content)
            return True, ""
        try:
            msg = r.json().get("detail", {})
            msg = msg.get("message", str(msg)) if isinstance(msg, dict) else str(msg)
        except Exception:
            msg = r.text[:200]
        return False, msg
    except Exception as e:
        return False, str(e)


def _audio_worker(script: str, voice_id: str, output_path: str, q: queue.Queue) -> None:
    try:
        chunks = _chunk_text(script)
        q.put({"type": "progress", "msg": f"Split into {len(chunks)} chunk(s) for TTS…"})

        if len(chunks) == 1:
            q.put({"type": "progress", "msg": f"Calling ElevenLabs ({len(chunks[0]):,} chars)…"})
            ok, err = _call_tts(chunks[0], voice_id, output_path)
            if ok:
                size_kb = os.path.getsize(output_path) // 1024
                q.put({"type": "done", "filename": os.path.basename(output_path), "size_kb": size_kb})
            else:
                q.put({"type": "error", "msg": err or "TTS request failed"})
        else:
            chunk_paths = [output_path.replace(".mp3", f"_part{i}.mp3") for i in range(len(chunks))]
            results: dict[int, tuple[bool, str]] = {}

            q.put({"type": "progress", "msg": f"Generating {len(chunks)} chunks in parallel…"})
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {
                    executor.submit(_call_tts, chunk, voice_id, path): i
                    for i, (chunk, path) in enumerate(zip(chunks, chunk_paths))
                }
                for future in as_completed(futures):
                    i = futures[future]
                    ok, err = future.result()
                    results[i] = (ok, err)
                    done_n = sum(1 for o, _ in results.values() if o)
                    q.put({"type": "progress",
                           "msg": f"Chunk {i+1} {'done' if ok else 'FAILED'} ({done_n}/{len(chunks)} complete)"})

            failed = [i for i, (ok, _) in results.items() if not ok]
            if failed:
                msgs = "; ".join(results[i][1] for i in failed[:3])
                q.put({"type": "error", "msg": f"Chunk(s) {[i+1 for i in failed]} failed: {msgs}"})
                return

            q.put({"type": "progress", "msg": "Concatenating with ffmpeg…"})
            ordered = [chunk_paths[i] for i in sorted(results)]
            if concat_mp3s(ordered, output_path):
                for p in chunk_paths:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
                size_kb = os.path.getsize(output_path) // 1024
                q.put({"type": "done", "filename": os.path.basename(output_path), "size_kb": size_kb})
            else:
                q.put({"type": "error", "msg": "ffmpeg concat failed — is ffmpeg installed?"})
    except Exception as e:
        traceback.print_exc()
        q.put({"type": "error", "msg": str(e)})


def _generate_script(chapter: dict, prompt: str, model: str) -> str | None:
    """Synchronous non-streaming generation used by the batch pipeline."""
    text = chapter["text"]
    if len(text) > 80000:
        text = text[:80000] + "\n\n[Content truncated]"
    full_prompt = f"{prompt}\n\nChapter: {chapter['title']}\n\n{text}"
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            temperature=0.7,
            tools=[{
                "name":         "generate_narration",
                "description":  "Output the audiobook narration script.",
                "input_schema": NARRATION_SCHEMA,
            }],
            tool_choice={"type": "tool", "name": "generate_narration"},
            messages=[{"role": "user", "content": full_prompt}],
        )
        for block in response.content:
            if block.type == "tool_use":
                script = block.input.get("script", "").strip()
                return f"{chapter['title']}\n\n{script}" if script else None
        return None
    except Exception:
        traceback.print_exc()
        return None


def _batch_worker(
    chapters: list[dict], prompt: str, model: str, voice_id: str, q: queue.Queue
) -> None:
    total = len(chapters)
    for i, chapter in enumerate(chapters):
        q.put({"type": "chapter_start", "idx": i, "total": total, "title": chapter["title"]})

        script = _generate_script(chapter, prompt, model)
        if not script:
            q.put({"type": "chapter_error", "idx": i, "title": chapter["title"],
                   "msg": "Script generation failed"})
            continue

        q.put({"type": "script_done", "idx": i, "title": chapter["title"], "script": script})

        safe     = re.sub(r"[^\w\s-]", "", chapter["title"])[:35].strip().replace(" ", "_")
        filename = f"{i+1:02d}_{safe}.mp3"
        out_path = os.path.join(AUDIOBOOK_DIR, filename)

        audio_q: queue.Queue = queue.Queue()
        _audio_worker(script, voice_id, out_path, audio_q)

        while True:
            msg = audio_q.get()
            if msg["type"] == "progress":
                q.put({"type": "audio_progress", "idx": i, "msg": msg["msg"]})
            elif msg["type"] == "done":
                q.put({"type": "chapter_done", "idx": i, "title": chapter["title"],
                       "filename": filename, "size_kb": msg["size_kb"]})
                break
            elif msg["type"] == "error":
                q.put({"type": "chapter_error", "idx": i, "title": chapter["title"],
                       "msg": msg["msg"]})
                break

    q.put({"type": "all_done", "count": total})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    el_config = {
        "apiKey":         ELEVENLABS_API_KEY or "",
        "narratorVoiceId": ELEVENLABS_HOST_VOICE_ID or "",
    }
    return render_template(
        "audiobook.html",
        prompts_json=json.dumps(PROMPTS),
        el_config_json=json.dumps(el_config),
        models_json=json.dumps(MODELS),
        default_model=CLAUDE_MODEL,
    )


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("epub")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400
    if not f.filename.lower().endswith(".epub"):
        return jsonify({"error": "File must be a .epub"}), 400

    file_bytes = f.read()
    chapters   = _parse_epub(file_bytes)
    if not chapters:
        return jsonify({"error": "No readable chapters found in EPUB"}), 400

    upload_id = str(uuid.uuid4())
    _book_store[upload_id] = {"filename": f.filename, "chapters": chapters}
    return jsonify({
        "upload_id":     upload_id,
        "filename":      f.filename,
        "chapter_count": len(chapters),
        "chapters":      [
            {"id": c["id"], "title": c["title"],
             "char_count": c["char_count"],
             "order": i}
            for i, c in enumerate(chapters)
        ],
    })


@app.route("/generate", methods=["POST"])
def generate():
    data         = request.json or {}
    upload_id    = data.get("upload_id")
    chapter_id   = data.get("chapter_id")
    prompt_text  = (data.get("prompt") or "").strip()
    model        = data.get("model") or CLAUDE_MODEL
    use_thinking = bool(data.get("thinking"))

    book = _book_store.get(upload_id)
    if not book:
        return jsonify({"error": "Upload not found — please re-upload the EPUB"}), 404

    chapter = next((c for c in book["chapters"] if c["id"] == chapter_id), None)
    if not chapter:
        return jsonify({"error": "Chapter not found"}), 404
    if not prompt_text:
        return jsonify({"error": "Prompt is empty"}), 400

    text = chapter["text"]
    if len(text) > 80000:
        text = text[:80000] + "\n\n[Content truncated]"

    if use_thinking:
        full_prompt = (
            f"{prompt_text}\n\n"
            "FORMAT RULE — output ONLY the narration text. No labels, no commentary.\n\n"
            f"Chapter: {chapter['title']}\n\n{text}"
        )

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
        full_prompt = f"{prompt_text}\n\nChapter: {chapter['title']}\n\n{text}"
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            response = client.messages.create(
                model=model,
                max_tokens=8192,
                temperature=0.7,
                tools=[{
                    "name":         "generate_narration",
                    "description":  "Output the audiobook narration script.",
                    "input_schema": NARRATION_SCHEMA,
                }],
                tool_choice={"type": "tool", "name": "generate_narration"},
                messages=[{"role": "user", "content": full_prompt}],
            )
            for block in response.content:
                if block.type == "tool_use":
                    script = block.input.get("script", "").strip()
                    if script:
                        return jsonify({"script": f"{chapter['title']}\n\n{script}"})
            return jsonify({"error": "Model returned no structured output"}), 500
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500


@app.route("/audio/create", methods=["POST"])
def audio_create():
    data     = request.json or {}
    script   = (data.get("script") or "").strip()
    voice_id = data.get("voice_id") or ELEVENLABS_HOST_VOICE_ID
    if not script:
        return jsonify({"error": "No script provided"}), 400
    if not voice_id:
        return jsonify({"error": "No voice ID — set ELEVENLABS_HOST_VOICE_ID or pass voice_id"}), 400
    if not ELEVENLABS_API_KEY:
        return jsonify({"error": "ELEVENLABS_API_KEY not set in .env"}), 400

    os.makedirs(AUDIOBOOK_DIR, exist_ok=True)
    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(AUDIOBOOK_DIR, f"chapter_{ts}.mp3")

    job_id, q = create_job()
    threading.Thread(target=_audio_worker, args=(script, voice_id, out_path, q), daemon=True).start()
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
    path = os.path.join(AUDIOBOOK_DIR, safe)
    if not os.path.exists(path):
        return "File not found", 404
    return send_file(path, mimetype="audio/mpeg", as_attachment=True, download_name=safe)


@app.route("/batch/create", methods=["POST"])
def batch_create():
    data        = request.json or {}
    upload_id   = data.get("upload_id")
    chapter_ids = data.get("chapter_ids", [])
    voice_id    = data.get("voice_id") or ELEVENLABS_HOST_VOICE_ID
    model       = data.get("model") or CLAUDE_MODEL
    prompt      = (data.get("prompt") or "").strip()

    book = _book_store.get(upload_id)
    if not book:
        return jsonify({"error": "Upload not found"}), 404
    if not voice_id:
        return jsonify({"error": "No voice ID"}), 400
    if not ELEVENLABS_API_KEY:
        return jsonify({"error": "ELEVENLABS_API_KEY not set in .env"}), 400
    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400

    id_set   = set(chapter_ids)
    chapters = [c for c in book["chapters"] if c["id"] in id_set]
    if not chapters:
        return jsonify({"error": "No matching chapters found"}), 400
    chapters.sort(key=lambda c: book["chapters"].index(c))

    os.makedirs(AUDIOBOOK_DIR, exist_ok=True)
    job_id, q = create_job()
    threading.Thread(
        target=_batch_worker,
        args=(chapters, prompt, model, voice_id, q),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/batch/stream/<job_id>")
def batch_stream(job_id):
    if not get_job(job_id):
        return jsonify({"error": "Job not found"}), 404
    terminal = ("all_done", "error")
    return Response(
        stream_with_context(event_gen(job_id, terminal_types=terminal)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
