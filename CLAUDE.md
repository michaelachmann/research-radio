# Research Radio — Project Context for Claude

## Purpose

Automated pipeline that converts academic papers into podcast episodes and
structured analyses. Runs hourly via GitHub Actions. Papers come from a
JSON feed (ToRead/Paperpile), PDFs from Google Drive.

## Pipeline

```
Feed (JSON) → Drive (PDF) → [Analyzers] → Outputs
```

1. `feed_parser.py` — fetches paper metadata from a JSON feed URL
2. `drive_client.py` — finds and downloads matching PDFs from Google Drive (Paperpile naming)
3. **Analyzer framework** — runs configured analyzers per paper:
   - `podcast` → Claude script + ElevenLabs TTS → MP3 → GitHub Release → RSS feed
   - `extraction` → Claude structured JSON (methodology, findings, limitations, open questions)
   - `critical` → Claude structured JSON (strengths, weaknesses, reproducibility, novelty)
4. `feed_generator.py` — updates `docs/feed.xml` and `docs/episodes.json` (RSS)
5. `github_uploader.py` — uploads MP3s to a GitHub Release named "audio"

## Module Map

```
config.py                      — all config via .env / env vars
src/
  main.py                      — orchestrator (GitHub Actions pipeline)
  feed_parser.py               — Paper dataclass + feed fetching
  drive_client.py              — Google Drive PDF lookup/download
  claude_client.py             — Anthropic SDK wrapper (generate, generate_json)
  tts_elevenlabs.py            — ElevenLabs Text-to-Dialogue wrapper + chunking
                                  concat_mp3s() — shared module-level MP3 concat helper
  feed_generator.py            — RSS feed + Episode dataclass
  github_uploader.py           — GitHub Release asset upload
  pdf_extractor.py             — standalone PDF download/text extraction
  analyzers/
    __init__.py                — REGISTRY + load_analyzers()
    base.py                    — PaperAnalyzer ABC + AnalysisResult dataclass
    podcast.py                 — podcast episode analyzer
    extraction.py              — structured extraction analyzer
    critical.py                — critical review analyzer
  webui/                       — local interactive web interfaces
    shared.py                  — MODELS, job store, SSE event_gen() (shared by both apps)
    podcast.py                 — Flask app: paper PDF → podcast script + audio (port 5000)
    audiobook.py               — Flask app: EPUB → audiobook chapters + audio (port 5001)
    static/
      shared.css               — base layout, panels, buttons, pills (CSS custom props for theming)
      shared.js                — utility functions + model/preset init (shared by both apps)
      podcast.css              — indigo theme + turn cards, speaker badges
      podcast.js               — upload, generate, TTS, cURL builder (two-voice dialogue)
      audiobook.css            — purple theme + chapter list, batch, voice section
      audiobook.js             — upload, chapter select, generate, batch, TTS, cURL builder
    templates/
      podcast.html             — podcast app HTML (Jinja2)
      audiobook.html           — audiobook app HTML (Jinja2)
data/
  processed.json               — paper IDs already processed (prevents re-runs)
docs/
  feed.xml                     — public RSS feed (GitHub Pages)
  episodes.json                — episode metadata store
  analyses/                    — per-paper JSON analyses (committed, served via GitHub Pages)
    {paper_id}_extraction.json
    {paper_id}_critical.json
.github/workflows/
  check_papers.yml             — hourly GitHub Actions job
scripts/
  validate_sync.py             — CI consistency check
```

## Running the Local Web Interfaces

```bash
python -m src.webui.podcast    # Paper → Podcast Script Lab  → http://localhost:5000
python -m src.webui.audiobook  # EPUB  → Audiobook Studio    → http://localhost:5001
```

## Adding a New Analyzer

1. Create `src/analyzers/my_analyzer.py` with a class that:
   - Inherits `PaperAnalyzer`
   - Sets `name = "my_analyzer"`
   - Implements `analyze(self, paper, paper_text) -> AnalysisResult`
2. Import and add to `REGISTRY` in `src/analyzers/__init__.py`
3. Add `"my_analyzer"` to `ENABLED_ANALYZERS` in `.env` or the workflow

## Key Config Variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API (LLM for all analyzers) |
| `CLAUDE_MODEL` | Default: `claude-sonnet-4-6`; swap to `claude-opus-4-6` for higher quality |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS (podcast analyzer) |
| `ELEVENLABS_HOST_VOICE_ID` | Voice ID for the host speaker |
| `ELEVENLABS_COHOST_VOICE_ID` | Voice ID for the co-host speaker |
| `TTS_HOST_NAME` | Host name spoken in the script (default: Alex) |
| `TTS_COHOST_NAME` | Co-host name spoken in the script (default: Sam) |
| `ENABLED_ANALYZERS` | Comma-separated list: `podcast,extraction,critical` |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCP service account JSON |
| `GOOGLE_DRIVE_FOLDER_ID` | Paperpile PDFs folder in Drive |
| `GITHUB_TOKEN` / `GITHUB_REPO` | For release asset uploads |
| `FEED_URL` | JSON paper feed URL |
| `PODCAST_TITLE` | Podcast name used in scripts and RSS |

## Migration State

- [x] LLM: Gemini → Claude (`claude_client.py`)
- [x] TTS: Gemini TTS → ElevenLabs v3 Text-to-Dialogue (`tts_elevenlabs.py`)
- [x] Analyzer framework (`src/analyzers/`)
- [x] `src/gemini_audio.py` deleted
- [x] `google-genai` removed from `requirements.txt`

## ElevenLabs TTS Notes

- Uses `/v1/text-to-dialogue` endpoint with `eleven_v3` model
- Hard limit: 2,000 chars per request → `tts_elevenlabs.py` chunks scripts at 1,800 chars and concatenates with ffmpeg
- Supports expressive audio tags in text: `[excited]`, `[thoughtfully]`, `[laughing]`, etc.
- Supports 70+ languages including German (future: translation analyzer)

## Integration: Research Runner

This project is intended to feed into a weekly research runner that:
- Consumes `docs/analyses/{paper_id}_extraction.json` and `docs/analyses/{paper_id}_critical.json`
- These are committed to the repo and accessible via `raw.githubusercontent.com`
- A future `weekly_summary` analyzer will aggregate recent analyses into a digest

## Next Steps

- [ ] Configure `ELEVENLABS_HOST_VOICE_ID` and `ELEVENLABS_COHOST_VOICE_ID` in GitHub Secrets
- [ ] Set `TTS_HOST_NAME` and `TTS_COHOST_NAME` to preferred host names
- [ ] Update `PODCAST_TITLE`, `PODCAST_AUTHOR`, `PODCAST_DESCRIPTION` in workflow
- [ ] Add `ANTHROPIC_API_KEY` and ElevenLabs secrets to GitHub repository settings
- [ ] Future: German translation analyzer (ElevenLabs v3 supports `language_code: "deu"`)
- [ ] Future: Weekly summary aggregator consuming `docs/analyses/`
