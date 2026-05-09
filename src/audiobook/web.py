"""
Local web interface for EPUB-to-audiobook conversion.

Run with:  python -m src.audiobook.web
Then open:  http://localhost:5001
"""

import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
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
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 is required.  pip install beautifulsoup4", file=sys.stderr)
    sys.exit(1)

from config import (
    ANTHROPIC_API_KEY,
    AUDIO_DIR,
    CLAUDE_MODEL,
    ELEVENLABS_API_KEY,
    ELEVENLABS_HOST_VOICE_ID,
)

AUDIOBOOK_DIR = os.path.join(AUDIO_DIR, "audiobook")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

_book_store: dict[str, dict] = {}
_job_store:  dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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

MODELS = [
    {"id": "claude-sonnet-4-6",        "label": "Sonnet 4.6",  "thinking": True},
    {"id": "claude-opus-4-7",           "label": "Opus 4.7",    "thinking": True},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5",   "thinking": False},
]

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
                n
                for n in names
                if n.lower().endswith((".html", ".htm", ".xhtml"))
                and "META-INF" not in n
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
                    "id": f"ch_{i}_{re.sub(r'[^a-z0-9]', '_', os.path.basename(path).lower())}",
                    "title": title,
                    "text": text,
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
        r = requests.post(
            url, headers=headers,
            json={"text": text, "model_id": "eleven_v3"},
            timeout=120,
        )
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


def _concat_mp3s(paths: list[str], output_path: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in paths:
            f.write(f"file '{p}'\n")
        list_file = f.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", output_path],
            capture_output=True,
            check=True,
        )
        return True
    except Exception:
        return False
    finally:
        os.unlink(list_file)


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
            if _concat_mp3s(ordered, output_path):
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


def _generate_script(chapter: dict, prompt: str, model: str):
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
                "name": "generate_narration",
                "description": "Output the audiobook narration script.",
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
    except Exception as e:
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

        safe = re.sub(r"[^\w\s-]", "", chapter["title"])[:35].strip().replace(" ", "_")
        filename = f"{i+1:02d}_{safe}.mp3"
        output_path = os.path.join(AUDIOBOOK_DIR, filename)

        audio_q: queue.Queue = queue.Queue()
        _audio_worker(script, voice_id, output_path, audio_q)

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
        "apiKey":          ELEVENLABS_API_KEY or "",
        "narratorVoiceId": ELEVENLABS_HOST_VOICE_ID or "",
    }
    page = _PAGE.replace('"__PROMPTS_JSON__"', json.dumps(PROMPTS))
    page = page.replace('"__EL_CONFIG__"',     json.dumps(el_config))
    page = page.replace('"__MODELS_JSON__"',   json.dumps(MODELS))
    page = page.replace('"__DEFAULT_MODEL__"', json.dumps(CLAUDE_MODEL))
    return page


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("epub")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400
    if not f.filename.lower().endswith(".epub"):
        return jsonify({"error": "File must be a .epub"}), 400
    try:
        file_bytes = f.read()
        chapters = _parse_epub(file_bytes)
        if not chapters:
            return jsonify({"error": "No readable chapters found in EPUB"}), 400
        upload_id = str(uuid.uuid4())
        _book_store[upload_id] = {"chapters": chapters, "filename": f.filename}
        return jsonify({
            "upload_id":     upload_id,
            "filename":      f.filename,
            "chapter_count": len(chapters),
            "chapters": [
                {"id": ch["id"], "title": ch["title"], "char_count": ch["char_count"]}
                for ch in chapters
            ],
        })
    except zipfile.BadZipFile:
        return jsonify({"error": "Invalid EPUB (not a valid zip file)"}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


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

    chapter = next((ch for ch in book["chapters"] if ch["id"] == chapter_id), None)
    if not chapter:
        return jsonify({"error": "Chapter not found"}), 404

    text = chapter["text"]
    if len(text) > 80000:
        text = text[:80000] + "\n\n[Content truncated at 80,000 chars]"

    full_prompt = f"{prompt_text}\n\nChapter: {chapter['title']}\n\n{text}"

    if use_thinking:
        full_prompt += (
            "\n\nOUTPUT RULE: output ONLY the narration text with ElevenLabs tags. "
            "No headers, no labels, no meta-commentary whatsoever."
        )

        def stream_thinking():
            try:
                yield f"{chapter['title']}\n\n"
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

        return Response(
            stream_with_context(stream_thinking()),
            mimetype="text/plain; charset=utf-8",
        )
    else:
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
                    return jsonify({"script": f"{chapter['title']}\n\n{script}"})
            return jsonify({"error": "Model returned no structured output"}), 500
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500


@app.route("/audio/create", methods=["POST"])
def audio_create():
    data     = request.json or {}
    script   = (data.get("script") or "").strip()
    voice_id = (data.get("voice_id") or ELEVENLABS_HOST_VOICE_ID or "").strip()
    if not script:
        return jsonify({"error": "No script provided"}), 400
    if not voice_id:
        return jsonify({"error": "No voice ID"}), 400
    if not ELEVENLABS_API_KEY:
        return jsonify({"error": "ELEVENLABS_API_KEY not set in .env"}), 400

    os.makedirs(AUDIOBOOK_DIR, exist_ok=True)
    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(AUDIOBOOK_DIR, f"chapter_{ts}.mp3")

    job_id = str(uuid.uuid4())
    q = queue.Queue()
    _job_store[job_id] = {"queue": q}
    threading.Thread(target=_audio_worker, args=(script, voice_id, out_path, q), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/batch/create", methods=["POST"])
def batch_create():
    data        = request.json or {}
    upload_id   = data.get("upload_id")
    chapter_ids = data.get("chapter_ids", [])
    voice_id    = (data.get("voice_id") or ELEVENLABS_HOST_VOICE_ID or "").strip()
    prompt      = (data.get("prompt") or "").strip()
    model       = data.get("model") or CLAUDE_MODEL

    book = _book_store.get(upload_id)
    if not book:
        return jsonify({"error": "Upload not found"}), 404
    if not voice_id:
        return jsonify({"error": "No voice ID"}), 400
    if not ELEVENLABS_API_KEY:
        return jsonify({"error": "ELEVENLABS_API_KEY not set in .env"}), 400
    if not prompt:
        return jsonify({"error": "Prompt is empty"}), 400

    id_set = set(chapter_ids)
    chapters = [ch for ch in book["chapters"] if ch["id"] in id_set]
    if not chapters:
        return jsonify({"error": "No matching chapters found"}), 404

    order = {cid: i for i, cid in enumerate(chapter_ids)}
    chapters.sort(key=lambda c: order.get(c["id"], 999))

    os.makedirs(AUDIOBOOK_DIR, exist_ok=True)

    job_id = str(uuid.uuid4())
    q = queue.Queue()
    _job_store[job_id] = {"queue": q}
    threading.Thread(
        target=_batch_worker,
        args=(chapters, prompt, model, voice_id, q),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "chapter_count": len(chapters)})


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
                if msg["type"] in ("done", "error", "all_done"):
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
    path = os.path.join(AUDIOBOOK_DIR, safe)
    if not os.path.exists(path):
        return "File not found", 404
    return send_file(path, mimetype="audio/mpeg", as_attachment=True, download_name=safe)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Research Radio — Audiobook Lab</title>
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    body{
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
      background:#f1f5f9;color:#0f172a;
      height:100vh;display:flex;flex-direction:column;overflow:hidden;
    }

    header{
      padding:.6rem 1.4rem;
      background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);
      border-bottom:1px solid #334155;
      display:flex;align-items:center;gap:.75rem;flex-shrink:0;
    }
    .header-logo{font-size:1.15rem;line-height:1}
    header h1{
      font-size:.95rem;font-weight:700;letter-spacing:-.015em;
      background:linear-gradient(90deg,#e2e8f0 30%,#a78bfa 100%);
      -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
    }
    .header-badge{
      font-size:.63rem;background:rgba(167,139,250,.15);
      color:#c4b5fd;padding:.15rem .52rem;border-radius:999px;
      font-weight:600;border:1px solid rgba(167,139,250,.25);letter-spacing:.03em;
    }

    .workspace{flex:1;display:grid;grid-template-columns:320px 1fr 360px;overflow:hidden}

    .panel{display:flex;flex-direction:column;overflow:hidden}
    .panel-hd{
      display:flex;align-items:center;justify-content:space-between;
      padding:.45rem .9rem;border-bottom:1px solid #e2e8f0;
      background:#f8fafc;flex-shrink:0;
    }
    .panel-hd h2{font-size:.63rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:#94a3b8}
    .panel-bd{flex:1;overflow-y:auto;padding:.85rem;display:flex;flex-direction:column;gap:.75rem}

    .panel-left{background:#fff;border-right:1px solid #e2e8f0}

    .section-label{
      display:block;font-size:.63rem;font-weight:700;
      text-transform:uppercase;letter-spacing:.08em;color:#94a3b8;margin-bottom:.3rem;
    }

    /* Upload */
    .upload-zone{
      border:2px dashed #cbd5e1;border-radius:10px;
      padding:.9rem .75rem;text-align:center;cursor:pointer;
      transition:border-color .15s,background .15s;user-select:none;
    }
    .upload-zone:hover,.upload-zone.drag-over{border-color:#8b5cf6;background:#f5f3ff}
    .upload-zone.loaded{border-color:#22c55e;background:#f0fdf4;cursor:default;padding:.55rem .75rem}
    .upload-zone input{display:none}
    .upload-icon{font-size:1.4rem;margin-bottom:.28rem}
    .upload-zone p{font-size:.76rem;color:#94a3b8}
    .upload-zone .file-name{font-weight:600;color:#0f172a;font-size:.82rem}
    .upload-zone .file-meta{font-size:.65rem;color:#94a3b8;margin-top:.1rem}

    /* Chapter list */
    .chapter-section{display:none;flex-direction:column;gap:.35rem}
    .chapter-controls{display:flex;align-items:center;gap:.35rem}
    .chapter-controls .section-label{margin-bottom:0;flex:none}
    .selection-info{font-size:.63rem;color:#7c3aed;font-weight:500;margin-left:auto}
    .chapter-list{
      max-height:160px;overflow-y:auto;
      border:1px solid #e2e8f0;border-radius:8px;
    }
    .chapter-item{
      display:flex;align-items:flex-start;gap:.5rem;
      padding:.42rem .72rem;cursor:pointer;
      border-bottom:1px solid #f1f5f9;
      border-left:3px solid transparent;
      transition:background .1s,border-color .1s;
    }
    .chapter-item:last-child{border-bottom:none}
    .chapter-item:hover{background:#f8fafc}
    .chapter-item.checked{background:#f5f3ff;border-left-color:#8b5cf6}
    .chapter-cb{
      margin-top:.15rem;flex-shrink:0;width:.85rem;height:.85rem;
      accent-color:#8b5cf6;cursor:pointer;
    }
    .chapter-body{min-width:0;flex:1}
    .chapter-title{font-size:.77rem;font-weight:600;color:#0f172a;
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .chapter-meta{font-size:.62rem;color:#94a3b8;margin-top:.05rem}

    select{
      width:100%;padding:.38rem .55rem;border:1px solid #cbd5e1;border-radius:7px;
      font-size:.82rem;background:#fff;color:#0f172a;cursor:pointer;
    }
    select:focus{outline:none;border-color:#8b5cf6;box-shadow:0 0 0 3px rgba(139,92,246,.12)}
    .preset-desc{font-size:.67rem;color:#94a3b8;margin-top:.22rem;min-height:.85rem}

    textarea{
      width:100%;padding:.55rem .65rem;border:1px solid #cbd5e1;border-radius:7px;
      font-size:.75rem;font-family:'SF Mono','Fira Code',monospace;
      line-height:1.65;resize:vertical;color:#0f172a;background:#fff;
    }
    textarea:focus{outline:none;border-color:#8b5cf6;box-shadow:0 0 0 3px rgba(139,92,246,.12)}
    #prompt-input{min-height:150px}
    .hint{font-size:.64rem;color:#cbd5e1}

    .model-row{display:grid;grid-template-columns:1fr auto;align-items:end;gap:.6rem;flex-shrink:0}
    .thinking-col{display:flex;flex-direction:column;gap:.28rem}
    .toggle-wrap{display:flex;align-items:center;gap:.45rem;padding:.38rem 0}
    .toggle-wrap span{font-size:.78rem;color:#64748b}
    input[type=checkbox].toggle{
      width:1.8rem;height:1.05rem;appearance:none;-webkit-appearance:none;
      background:#cbd5e1;border-radius:999px;position:relative;cursor:pointer;transition:background .2s;flex-shrink:0;
    }
    input[type=checkbox].toggle:checked{background:#8b5cf6}
    input[type=checkbox].toggle::after{
      content:'';position:absolute;width:.82rem;height:.82rem;background:#fff;border-radius:50%;
      top:.11rem;left:.11rem;transition:transform .18s;box-shadow:0 1px 3px rgba(0,0,0,.2);
    }
    input[type=checkbox].toggle:checked::after{transform:translateX(.75rem)}
    input[type=checkbox].toggle:disabled{opacity:.35;cursor:not-allowed}

    .btn-generate{
      width:100%;padding:.58rem;
      background:linear-gradient(135deg,#8b5cf6,#7c3aed);
      color:#fff;border:none;border-radius:8px;font-size:.85rem;font-weight:600;
      cursor:pointer;transition:filter .15s;flex-shrink:0;
      box-shadow:0 2px 8px rgba(139,92,246,.3);
    }
    .btn-generate:hover:not(:disabled){filter:brightness(1.08)}
    .btn-generate:disabled{opacity:.4;cursor:not-allowed;box-shadow:none}
    .btn-generate.streaming{background:linear-gradient(135deg,#38bdf8,#0ea5e9)}

    .btn-batch{
      width:100%;padding:.58rem;
      background:linear-gradient(135deg,#f59e0b,#d97706);
      color:#fff;border:none;border-radius:8px;font-size:.85rem;font-weight:600;
      cursor:pointer;transition:filter .15s;flex-shrink:0;
      box-shadow:0 2px 8px rgba(245,158,11,.25);
    }
    .btn-batch:hover:not(:disabled){filter:brightness(1.08)}
    .btn-batch:disabled{opacity:.4;cursor:not-allowed;box-shadow:none}

    /* Middle panel */
    .panel-mid{background:#f8fafc;border-right:1px solid #e2e8f0}
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
    .tab-btn.active{background:#fff;color:#8b5cf6;border-color:#e2e8f0;box-shadow:0 1px 3px rgba(0,0,0,.06)}
    .tab-btn:hover:not(.active){color:#64748b;background:rgba(255,255,255,.5)}

    .pill{font-size:.63rem;padding:.14rem .44rem;border-radius:999px;font-weight:600}
    .pill-idle     {background:#e2e8f0;color:#94a3b8}
    .pill-streaming{background:#fef9c3;color:#854d0e}
    .pill-done     {background:#dcfce7;color:#166534}
    .pill-error    {background:#fee2e2;color:#991b1b}
    .pill-running  {background:#dbeafe;color:#1e40af}

    .btn-sm{
      padding:.26rem .65rem;background:#fff;border:1px solid #e2e8f0;
      border-radius:6px;font-size:.71rem;cursor:pointer;color:#374151;
      transition:background .1s,border-color .1s;white-space:nowrap;
    }
    .btn-sm:hover:not(:disabled){background:#f8fafc;border-color:#cbd5e1}

    #script-view{
      flex:1;background:#fff;border:1px solid #e2e8f0;border-radius:10px;
      padding:.9rem;overflow-y:auto;display:flex;flex-direction:column;gap:.65rem;
    }
    .script-empty{font-size:.8rem;color:#cbd5e1;text-align:center;padding:2.5rem 1rem;align-self:center}

    /* Chapter heading inside narration */
    .narration-heading{
      font-size:.9rem;font-weight:700;color:#0f172a;
      border-bottom:2px solid #ede9fe;padding-bottom:.35rem;
      margin-bottom:.1rem;
    }
    .narration-para{
      padding:.55rem .8rem .6rem;border-radius:8px;
      background:#faf5ff;border-left:3px solid #a78bfa;
    }
    .narration-para p{font-size:.84rem;line-height:1.75;color:#1e293b}
    .el-tag{
      font-size:.7rem;font-weight:600;background:#ede9fe;color:#7c3aed;
      padding:.05rem .28rem;border-radius:4px;
      font-family:'SF Mono','Fira Code',monospace;white-space:nowrap;
    }

    #output-area{
      flex:1;font-family:'SF Mono','Fira Code',monospace;
      font-size:.75rem;line-height:1.7;
      background:#fff;border:1px solid #e2e8f0;border-radius:10px;
      padding:.8rem .95rem;resize:none;color:#1e293b;
    }
    #output-area:focus{outline:none;border-color:#8b5cf6}
    .output-meta{font-size:.66rem;color:#cbd5e1;text-align:right;flex-shrink:0}

    /* Right panel */
    .panel-right{background:#fff}

    .voice-section{
      border:1px solid #e2e8f0;border-radius:10px;
      padding:.75rem;display:flex;flex-direction:column;gap:.45rem;flex-shrink:0;
    }
    #voice-id-input{
      width:100%;padding:.38rem .55rem;border:1px solid #cbd5e1;border-radius:7px;
      font-size:.78rem;font-family:'SF Mono','Fira Code',monospace;color:#0f172a;
    }
    #voice-id-input:focus{outline:none;border-color:#8b5cf6;box-shadow:0 0 0 3px rgba(139,92,246,.12)}
    .voice-hint{font-size:.64rem;color:#94a3b8}

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
      transition:filter .15s;box-shadow:0 2px 6px rgba(34,197,94,.25);
    }
    .btn-audio:hover:not(:disabled){filter:brightness(1.08)}
    .btn-audio:disabled{opacity:.4;cursor:not-allowed;box-shadow:none}
    .btn-audio.running{background:linear-gradient(135deg,#38bdf8,#0ea5e9);box-shadow:none}

    .audio-log{
      background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;
      padding:.45rem .6rem;font-family:'SF Mono','Fira Code',monospace;
      font-size:.68rem;line-height:1.55;color:#64748b;max-height:80px;overflow-y:auto;
    }
    .download-link{
      display:inline-flex;align-items:center;gap:.35rem;
      padding:.42rem .75rem;background:#f0fdf4;border:1px solid #86efac;border-radius:7px;
      color:#166534;font-size:.79rem;font-weight:600;text-decoration:none;transition:background .12s;
    }
    .download-link:hover{background:#dcfce7}

    /* Batch progress */
    .batch-section{
      border:1px solid #fde68a;border-radius:10px;
      background:#fffbeb;padding:.75rem;
      display:flex;flex-direction:column;gap:.5rem;flex-shrink:0;
    }
    .batch-overall{
      display:flex;align-items:center;justify-content:space-between;
    }
    .batch-items{display:flex;flex-direction:column;gap:.35rem}
    .batch-item{
      background:#fff;border:1px solid #e2e8f0;border-radius:7px;
      padding:.45rem .65rem;display:flex;flex-direction:column;gap:.2rem;
    }
    .batch-item-hd{display:flex;align-items:center;gap:.4rem}
    .batch-item-title{font-size:.75rem;font-weight:600;color:#0f172a;
      flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .batch-item-msg{font-size:.65rem;color:#64748b;font-family:'SF Mono','Fira Code',monospace}
    .batch-item.state-running .batch-item-title{color:#1e40af}
    .batch-item.state-done   .batch-item-title{color:#166534}
    .batch-item.state-error  .batch-item-title{color:#991b1b}
    .batch-dl{margin-top:.2rem;font-size:.72rem;padding:.28rem .55rem}

    .curl-divider{
      font-size:.63rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
      color:#cbd5e1;display:flex;align-items:center;gap:.5rem;flex-shrink:0;
    }
    .curl-divider::after{content:'';flex:1;height:1px;background:#e2e8f0}

    .warn-bar{
      font-size:.71rem;color:#92400e;
      background:#fffbeb;border:1px solid #fde68a;border-radius:7px;padding:.38rem .6rem;
    }
    .chunk-block{flex-shrink:0}
    .chunk-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:.3rem}
    .chunk-label{font-size:.68rem;font-weight:600;color:#94a3b8}
    .code-box{
      background:#0f172a;color:#e2e8f0;border-radius:7px;
      padding:.65rem .75rem;font-family:'SF Mono','Fira Code',monospace;
      font-size:.67rem;line-height:1.55;white-space:pre;overflow-x:auto;max-height:200px;overflow-y:auto;
    }
    .ffmpeg-block{border-top:1px solid #e2e8f0;padding-top:.75rem;flex-shrink:0}
    #curl-empty{font-size:.79rem;color:#94a3b8;text-align:center;padding:1.5rem 1rem}

    .statusbar{
      padding:.28rem 1.4rem;font-size:.68rem;color:#94a3b8;
      background:#fff;border-top:1px solid #e2e8f0;flex-shrink:0;
    }
  </style>
</head>
<body>

<header>
  <span class="header-logo">📖</span>
  <h1>Research Radio</h1>
  <span class="header-badge">Audiobook Lab</span>
</header>

<div class="workspace">

  <!-- ── Left: setup ── -->
  <div class="panel panel-left">
    <div class="panel-hd"><h2>Setup</h2></div>
    <div class="panel-bd">

      <div>
        <label class="section-label">Book (EPUB)</label>
        <div class="upload-zone" id="upload-zone">
          <input type="file" id="epub-input" accept=".epub">
          <div class="upload-icon">📚</div>
          <p id="upload-hint">Click or drop an EPUB here</p>
          <p id="file-name" class="file-name" style="display:none"></p>
          <p id="file-meta" class="file-meta" style="display:none"></p>
        </div>
      </div>

      <!-- Chapter list -->
      <div class="chapter-section" id="chapter-section">
        <div class="chapter-controls">
          <label class="section-label">Chapters</label>
          <button class="btn-sm" onclick="selectAll()" style="padding:.18rem .5rem;font-size:.65rem">All</button>
          <button class="btn-sm" onclick="clearAll()" style="padding:.18rem .5rem;font-size:.65rem">None</button>
          <span class="selection-info" id="selection-info"></span>
        </div>
        <div class="chapter-list" id="chapter-list"></div>
      </div>

      <!-- Mode / preset -->
      <div>
        <label class="section-label" for="preset-select">Mode</label>
        <select id="preset-select"></select>
        <p class="preset-desc" id="preset-desc"></p>
      </div>

      <!-- Prompt -->
      <div style="display:flex;flex-direction:column;gap:.28rem">
        <label class="section-label" for="prompt-input">
          Prompt <span style="font-weight:400;color:#cbd5e1;text-transform:none;letter-spacing:0">(editable)</span>
        </label>
        <textarea id="prompt-input" rows="8" placeholder="Select a mode or write a custom prompt…"></textarea>
        <p class="hint">Chapter text is appended automatically.</p>
      </div>

      <!-- Model -->
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
      <button class="btn-batch"    id="batch-btn"    disabled>⚡ Batch: Generate + Convert All</button>

    </div>
  </div>

  <!-- ── Middle: script ── -->
  <div class="panel panel-mid">
    <div class="panel-hd">
      <h2>Narration Script</h2>
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
        <p class="script-empty">Generate a narration to see it here.</p>
      </div>
      <textarea id="output-area" placeholder="Narration script will appear here…" readonly style="display:none;flex:1"></textarea>
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

      <div class="voice-section">
        <label class="section-label">Narrator Voice ID</label>
        <input type="text" id="voice-id-input" placeholder="ElevenLabs voice ID">
        <p class="voice-hint">From elevenlabs.io → Voices. Used for Create Audio, Batch, and cURL.</p>
      </div>

      <div class="audio-section">
        <div class="audio-controls">
          <button class="btn-audio" id="create-audio-btn" onclick="createAudio()" disabled>▶ Create Audio</button>
          <span class="pill pill-idle" id="audio-pill" style="display:none"></span>
        </div>
        <div class="audio-log" id="audio-log" style="display:none"></div>
        <a id="audio-download" class="download-link" href="#" style="display:none" download>⬇ Download MP3</a>
      </div>

      <!-- Batch progress (shown during/after batch) -->
      <div class="batch-section" id="batch-section" style="display:none">
        <div class="batch-overall">
          <span class="section-label" style="margin:0">Batch Progress</span>
          <span class="pill pill-idle" id="batch-pill">0 / 0</span>
        </div>
        <div class="batch-items" id="batch-items"></div>
      </div>

      <div class="curl-divider">cURL Commands</div>
      <div id="curl-chunks">
        <p id="curl-empty">Generate a narration to build curl commands.</p>
      </div>

    </div>
  </div>

</div>

<div class="statusbar" id="statusbar">Ready — upload an EPUB to begin.</div>

<script>
const PROMPTS       = "__PROMPTS_JSON__";
const EL_CONFIG     = "__EL_CONFIG__";
const MODELS        = "__MODELS_JSON__";
const DEFAULT_MODEL = "__DEFAULT_MODEL__";

const CHARS_PER_CHUNK = 4000;
const CHARS_PER_MIN   = 800;   // rough narration speed estimate

let uploadId    = null;
let _chapters   = [];          // all chapter objects from upload
let _selectedIds = new Set();  // currently checked chapter IDs
let _currentTab = 'script';

document.getElementById('voice-id-input').value = EL_CONFIG.narratorVoiceId || '';

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
thinkingToggle.addEventListener('change', () => { thinkingLabel.textContent = thinkingToggle.checked ? 'on' : 'off'; });
syncThinkingToggle();

// ── Presets ───────────────────────────────────────────────────
const presetSelect = document.getElementById('preset-select');
Object.entries(PROMPTS).forEach(([key, p]) => {
  const opt = document.createElement('option'); opt.value = key; opt.textContent = p.label;
  presetSelect.appendChild(opt);
});
function applyPreset(key) {
  const p = PROMPTS[key]; if (!p) return;
  document.getElementById('prompt-input').value = p.template;
  document.getElementById('preset-desc').textContent = p.description;
}
applyPreset(Object.keys(PROMPTS)[0]);
presetSelect.addEventListener('change', () => applyPreset(presetSelect.value));

// ── Reading time ──────────────────────────────────────────────
function readingTime(chars) {
  const mins = Math.max(1, Math.round(chars / CHARS_PER_MIN));
  if (mins < 60) return `~${mins} min`;
  const h = Math.floor(mins / 60), m = mins % 60;
  return m ? `~${h}h ${m}min` : `~${h}h`;
}

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
function renderScript(text) {
  const view = document.getElementById('script-view');
  if (!text.trim()) { view.innerHTML = '<p class="script-empty">No narration text found.</p>'; return; }
  const lines = text.split('\n');
  let html = '';
  let inPara = false;
  // First non-empty line is treated as the chapter heading
  let headingDone = false;
  const paras = text.split(/\n\n+/).map(p => p.trim()).filter(Boolean);
  paras.forEach((p, i) => {
    if (i === 0 && !p.includes('[') && p.length < 120) {
      // Treat as chapter heading
      html += `<div class="narration-heading">${escHtml(p)}</div>`;
    } else {
      const content = escHtml(p).replace(/\[([^\]]+)\]/g, '<span class="el-tag">[$1]</span>');
      html += `<div class="narration-para"><p>${content}</p></div>`;
    }
  });
  view.innerHTML = html || '<p class="script-empty">No narration text found.</p>';
}

// ── Upload ────────────────────────────────────────────────────
const zone      = document.getElementById('upload-zone');
const epubInput = document.getElementById('epub-input');
zone.addEventListener('click', () => { if (!uploadId) epubInput.click(); });
zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
zone.addEventListener('drop', e => {
  e.preventDefault(); zone.classList.remove('drag-over');
  if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
});
epubInput.addEventListener('change', () => { if (epubInput.files[0]) handleFile(epubInput.files[0]); });

async function handleFile(file) {
  if (!file.name.toLowerCase().endsWith('.epub')) { setStatus('Please upload an .epub file', true); return; }
  setStatus('Parsing EPUB…');
  const fd = new FormData(); fd.append('epub', file);
  try {
    const res  = await fetch('/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) { setStatus('Error: ' + data.error, true); return; }
    uploadId = data.upload_id;
    zone.classList.add('loaded');
    document.getElementById('upload-hint').style.display = 'none';
    const fn = document.getElementById('file-name'); fn.textContent = data.filename; fn.style.display = '';
    const fm = document.getElementById('file-meta'); fm.textContent = `${data.chapter_count} chapters found`; fm.style.display = '';
    renderChapterList(data.chapters);
    setStatus(`Loaded: ${data.filename} — ${data.chapter_count} chapters`);
  } catch(e) { setStatus('Upload failed: ' + e.message, true); }
}

// ── Chapter list ──────────────────────────────────────────────
function renderChapterList(chapters) {
  _chapters = chapters;
  _selectedIds = new Set();
  document.getElementById('chapter-section').style.display = 'flex';
  const list = document.getElementById('chapter-list');
  list.innerHTML = chapters.map(ch => `
    <label class="chapter-item" data-id="${escHtml(ch.id)}">
      <input type="checkbox" class="chapter-cb" value="${escHtml(ch.id)}" onchange="onChapterToggle(this)">
      <div class="chapter-body">
        <div class="chapter-title">${escHtml(ch.title)}</div>
        <div class="chapter-meta">${ch.char_count.toLocaleString()} chars · ${readingTime(ch.char_count)}</div>
      </div>
    </label>`).join('');
  updateSelection();
}

function onChapterToggle(cb) {
  const id = cb.value;
  if (cb.checked) _selectedIds.add(id);
  else _selectedIds.delete(id);
  cb.closest('.chapter-item').classList.toggle('checked', cb.checked);
  updateSelection();
}

function selectAll() {
  _chapters.forEach(ch => { _selectedIds.add(ch.id); });
  document.querySelectorAll('.chapter-cb').forEach(cb => { cb.checked = true; cb.closest('.chapter-item').classList.add('checked'); });
  updateSelection();
}
function clearAll() {
  _selectedIds.clear();
  document.querySelectorAll('.chapter-cb').forEach(cb => { cb.checked = false; cb.closest('.chapter-item').classList.remove('checked'); });
  updateSelection();
}

function getSelectedChapters() {
  return _chapters.filter(ch => _selectedIds.has(ch.id));
}

function updateSelection() {
  const sel = getSelectedChapters();
  const totalChars = sel.reduce((s, c) => s + c.char_count, 0);
  const info = document.getElementById('selection-info');
  if (!sel.length) {
    info.textContent = 'none selected';
  } else {
    info.textContent = `${sel.length} · ${readingTime(totalChars)}`;
  }
  const hasSel = sel.length > 0;
  document.getElementById('generate-btn').disabled = !hasSel;
  document.getElementById('batch-btn').disabled    = !hasSel;
}

// ── Generate (single — uses first selected chapter) ───────────
document.getElementById('generate-btn').addEventListener('click', generate);

async function generate() {
  const sel = getSelectedChapters();
  if (!sel.length) return;
  const chapter = sel[0];

  const prompt = document.getElementById('prompt-input').value.trim();
  if (!prompt) { setStatus('Prompt is empty', true); return; }

  const out = document.getElementById('output-area');
  out.value = ''; out.setAttribute('readonly', '');
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
      headers: {'Content-Type': 'application/json'},
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
      setStatus('Error: ' + (err.error || res.statusText), true); setPill('error', 'Error'); return;
    }

    const ct = res.headers.get('Content-Type') || '';
    if (ct.includes('text/plain')) {
      switchTab('raw'); out.removeAttribute('readonly');
      setPill('streaming', 'Streaming');
      const reader = res.body.getReader(); const dec = new TextDecoder();
      let total = 0;
      while (true) {
        const {done, value} = await reader.read(); if (done) break;
        const chunk = dec.decode(value, {stream: true});
        out.value += chunk; out.scrollTop = out.scrollHeight;
        total += chunk.length; setMeta(total.toLocaleString() + ' chars');
      }
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      setPill('done', 'Done'); setStatus(`Done in ${elapsed}s`);
      setMeta(`${total.toLocaleString()} chars · ${elapsed}s`);
      renderScript(out.value); switchTab('script');
    } else {
      const data = await res.json();
      if (data.error) { setStatus('Error: ' + data.error, true); setPill('error', 'Error'); return; }
      out.value = data.script || ''; out.removeAttribute('readonly');
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      setPill('done', 'Done');
      setStatus(`Done in ${elapsed}s`);
      setMeta(`${(out.value.length).toLocaleString()} chars · ${elapsed}s`);
      renderScript(out.value); switchTab('script');
    }

    document.getElementById('create-audio-btn').disabled = false;
    refreshCurl();
  } catch(e) {
    setStatus('Error: ' + e.message, true); setPill('error', 'Error');
  } finally {
    btn.disabled = false; btn.textContent = 'Generate Script'; btn.classList.remove('streaming');
    updateSelection();
  }
}

// ── Batch process ─────────────────────────────────────────────
document.getElementById('batch-btn').addEventListener('click', batchProcess);

async function batchProcess() {
  const sel = getSelectedChapters();
  if (!sel.length) { setStatus('Select at least one chapter', true); return; }
  const voiceId = document.getElementById('voice-id-input').value.trim();
  const prompt  = document.getElementById('prompt-input').value.trim();
  if (!voiceId) { setStatus('Enter a narrator voice ID', true); return; }
  if (!prompt)  { setStatus('Prompt is empty', true); return; }

  // Render batch items
  const batchSection = document.getElementById('batch-section');
  batchSection.style.display = '';
  renderBatchItems(sel);
  document.getElementById('batch-pill').className = 'pill pill-running';
  document.getElementById('batch-pill').textContent = `0 / ${sel.length}`;

  const btn = document.getElementById('batch-btn');
  btn.disabled = true; btn.textContent = '⏳ Processing…';
  setPill('running', 'Batch');
  setStatus(`Starting batch: ${sel.length} chapter(s)…`);

  try {
    const res = await fetch('/batch/create', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        upload_id:   uploadId,
        chapter_ids: sel.map(c => c.id),
        voice_id:    voiceId,
        prompt,
        model:       modelSelect.value,
      }),
    });
    const data = await res.json();
    if (data.error) {
      setStatus('Error: ' + data.error, true); setPill('error', 'Error');
      btn.disabled = false; btn.textContent = '⚡ Batch: Generate + Convert All'; return;
    }

    let doneCount = 0;
    const evtSrc = new EventSource('/audio/stream/' + data.job_id);
    evtSrc.onmessage = function(e) {
      const msg = JSON.parse(e.data);
      switch (msg.type) {
        case 'chapter_start':
          updateBatchItem(msg.idx, 'running', `Generating script…`);
          setStatus(`Chapter ${msg.idx+1}/${msg.total}: "${msg.title}"`);
          break;
        case 'script_done':
          updateBatchItem(msg.idx, 'running', 'Script done — converting to audio…');
          // Preview script in middle panel
          document.getElementById('output-area').value = msg.script;
          renderScript(msg.script); switchTab('script');
          break;
        case 'audio_progress':
          updateBatchItem(msg.idx, 'running', msg.msg);
          break;
        case 'chapter_done':
          doneCount++;
          updateBatchItem(msg.idx, 'done', `${msg.size_kb} KB`);
          addBatchDownload(msg.idx, msg.filename);
          document.getElementById('batch-pill').textContent = `${doneCount} / ${sel.length}`;
          document.getElementById('create-audio-btn').disabled = false;
          refreshCurl();
          break;
        case 'chapter_error':
          updateBatchItem(msg.idx, 'error', msg.msg);
          break;
        case 'all_done':
          document.getElementById('batch-pill').className = 'pill pill-done';
          document.getElementById('batch-pill').textContent = `${msg.count} done`;
          setPill('done', 'Batch Done');
          setStatus(`Batch complete — ${msg.count} chapter(s) converted.`);
          btn.disabled = false; btn.textContent = '⚡ Batch: Generate + Convert All';
          evtSrc.close();
          break;
        case 'error':
          setStatus('Error: ' + msg.msg, true); setPill('error', 'Error');
          btn.disabled = false; btn.textContent = '⚡ Batch: Generate + Convert All';
          evtSrc.close();
          break;
      }
    };
    evtSrc.onerror = function() {
      setStatus('Batch stream disconnected', true);
      btn.disabled = false; btn.textContent = '⚡ Batch: Generate + Convert All';
      evtSrc.close();
    };
  } catch(e) {
    setStatus('Error: ' + e.message, true); setPill('error', 'Error');
    btn.disabled = false; btn.textContent = '⚡ Batch: Generate + Convert All';
  }
}

function renderBatchItems(chapters) {
  const container = document.getElementById('batch-items');
  container.innerHTML = chapters.map((ch, i) => `
    <div class="batch-item" id="batch-item-${i}">
      <div class="batch-item-hd">
        <span class="batch-item-title">${escHtml(ch.title)}</span>
        <span class="pill pill-idle" id="batch-pill-${i}">Waiting</span>
      </div>
      <div class="batch-item-msg" id="batch-msg-${i}"></div>
    </div>`).join('');
}

function updateBatchItem(idx, state, msg) {
  const item = document.getElementById(`batch-item-${idx}`);
  if (!item) return;
  item.className = `batch-item state-${state}`;
  const pill = document.getElementById(`batch-pill-${idx}`);
  if (pill) {
    pill.className = `pill pill-${state === 'running' ? 'running' : state === 'done' ? 'done' : 'error'}`;
    pill.textContent = state === 'running' ? '…' : state;
  }
  const msgEl = document.getElementById(`batch-msg-${idx}`);
  if (msgEl && msg) msgEl.textContent = msg;
}

function addBatchDownload(idx, filename) {
  const item = document.getElementById(`batch-item-${idx}`);
  if (!item) return;
  let dl = item.querySelector('.batch-dl');
  if (!dl) {
    dl = document.createElement('a');
    dl.className = 'download-link batch-dl';
    dl.download = filename;
    item.appendChild(dl);
  }
  dl.href = '/audio/file/' + filename;
  dl.textContent = '⬇ ' + filename;
  dl.style.display = '';
}

// ── Single Create Audio ───────────────────────────────────────
async function createAudio() {
  const script  = document.getElementById('output-area').value.trim();
  const voiceId = document.getElementById('voice-id-input').value.trim();
  if (!script)  { setStatus('Generate a narration script first', true); return; }
  if (!voiceId) { setStatus('Enter a narrator voice ID', true); return; }

  const btn = document.getElementById('create-audio-btn');
  btn.disabled = true; btn.classList.add('running'); btn.textContent = '⏳ Generating…';

  const logEl = document.getElementById('audio-log');
  logEl.style.display = ''; logEl.textContent = '';
  document.getElementById('audio-download').style.display = 'none';
  setAudioPill('running', 'Running');

  try {
    const res = await fetch('/audio/create', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({script, voice_id: voiceId}),
    });
    const data = await res.json();
    if (data.error) { appendAudioLog('Error: ' + data.error); setAudioPill('error', 'Error'); resetAudioBtn(); return; }

    const evtSrc = new EventSource('/audio/stream/' + data.job_id);
    evtSrc.onmessage = function(e) {
      const msg = JSON.parse(e.data);
      if (msg.type === 'progress') {
        appendAudioLog(msg.msg);
      } else if (msg.type === 'done') {
        appendAudioLog(`Done · ${msg.size_kb} KB`);
        setAudioPill('done', 'Done');
        const dl = document.getElementById('audio-download');
        dl.href = '/audio/file/' + msg.filename; dl.download = msg.filename;
        dl.textContent = '⬇ Download ' + msg.filename; dl.style.display = '';
        resetAudioBtn(); evtSrc.close(); setStatus('Audio saved: ' + msg.filename);
      } else if (msg.type === 'error') {
        appendAudioLog('Error: ' + msg.msg); setAudioPill('error', 'Error'); resetAudioBtn(); evtSrc.close();
      }
    };
    evtSrc.onerror = function() { appendAudioLog('Connection lost'); setAudioPill('error', 'Error'); resetAudioBtn(); evtSrc.close(); };
  } catch(e) { appendAudioLog('Failed: ' + e.message); setAudioPill('error', 'Error'); resetAudioBtn(); }
}
function resetAudioBtn() {
  const btn = document.getElementById('create-audio-btn');
  btn.disabled = false; btn.classList.remove('running'); btn.textContent = '▶ Create Audio';
}
function appendAudioLog(msg) {
  const el = document.getElementById('audio-log');
  el.textContent += (el.textContent ? '\n' : '') + msg; el.scrollTop = el.scrollHeight;
}
function setAudioPill(state, label) {
  const p = document.getElementById('audio-pill');
  p.style.display = ''; p.className = 'pill pill-' + state; p.textContent = label;
}

// ── cURL builder ──────────────────────────────────────────────
function chunkScript(text) {
  const paras = text.split(/\n\n+/).map(p => p.trim()).filter(Boolean);
  const chunks = []; let cur = [], curLen = 0;
  for (const p of paras) {
    if (cur.length && curLen + p.length + 2 > CHARS_PER_CHUNK) {
      chunks.push(cur.join('\n\n')); cur = []; curLen = 0;
    }
    cur.push(p); curLen += p.length + 2;
  }
  if (cur.length) chunks.push(cur.join('\n\n'));
  return chunks.length ? chunks : [text];
}

function sqEscape(s) { return s.replace(/'/g, "'\\''"); }

function refreshCurl() {
  const script  = document.getElementById('output-area').value.trim();
  const el      = document.getElementById('curl-chunks');
  const voiceId = document.getElementById('voice-id-input').value.trim() || 'VOICE_ID';
  const apiKey  = EL_CONFIG.apiKey || 'YOUR_ELEVENLABS_API_KEY';

  if (!script) { el.innerHTML = '<p id="curl-empty">Generate a narration to build curl commands.</p>'; return; }

  const chunks = chunkScript(script);
  const single = chunks.length === 1;
  let html = !document.getElementById('voice-id-input').value.trim()
    ? `<div class="warn-bar">Enter a narrator voice ID to get accurate cURL commands.</div>` : '';

  chunks.forEach((chunk, i) => {
    const outFile = single ? 'chapter.mp3' : `part_${i}.mp3`;
    const body = JSON.stringify({text: chunk, model_id: 'eleven_v3'}, null, 2);
    const cmd = `curl -s -X POST 'https://api.elevenlabs.io/v1/text-to-speech/${voiceId}' \\\n` +
                `  -H 'xi-api-key: ${apiKey}' \\\n` +
                `  -H 'Content-Type: application/json' \\\n` +
                `  --output ${outFile} \\\n` +
                `  -d '${sqEscape(body)}'`;
    html += `<div class="chunk-block">
      <div class="chunk-hd">
        <span class="chunk-label">Part ${i+1}/${chunks.length} · ${chunk.length.toLocaleString()} chars</span>
        <button class="btn-sm" onclick="copyCode(this)">Copy</button>
      </div>
      <div class="code-box" data-cmd="${encodeURIComponent(cmd)}">${escHtml(cmd)}</div>
    </div>`;
  });

  if (chunks.length > 1) {
    const list = chunks.map((_, i) => `file 'part_${i}.mp3'`).join('\n');
    const ff = `# filelist.txt:\n${list}\n\nffmpeg -f concat -safe 0 -i filelist.txt -c copy chapter.mp3`;
    html += `<div class="ffmpeg-block">
      <div class="chunk-hd">
        <span class="chunk-label">ffmpeg concat → chapter.mp3</span>
        <button class="btn-sm" onclick="copyCode(this)">Copy</button>
      </div>
      <div class="code-box" data-cmd="${encodeURIComponent(ff)}">${escHtml(ff)}</div>
    </div>`;
  }
  el.innerHTML = html;
}

document.getElementById('voice-id-input').addEventListener('input', () => {
  if (document.getElementById('output-area').value) refreshCurl();
});

function copyCode(btn) {
  const box = btn.closest('.chunk-block,.ffmpeg-block').querySelector('.code-box');
  navigator.clipboard.writeText(decodeURIComponent(box.dataset.cmd)).then(() => {
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
    app.run(debug=True, port=5001, threaded=True)
