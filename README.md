# Paper-to-Podcast & Analysis Pipeline

An automated pipeline that converts academic papers into podcast episodes and structured analyses using AI. It fetches papers from a reading list, retrieves PDFs from Google Drive, generates conversational podcast scripts and structured academic analyses with Claude, and produces multi-speaker audio using ElevenLabs.

## About This Project

This repository contains the **code** for generating AI-powered podcast discussions and structured analyses of academic papers.

**Note:** "FG's Research Radio" is a podcast produced using this code, focusing on computational social science, platform studies, and misinformation research. If you use this code to create your own podcast, please choose a different name and branding for your show.

## Listen to FG's Research Radio

- [RSS Feed](https://fabiogiglietto.github.io/research-radio/feed.xml)
- [Spotify](https://open.spotify.com/show/5V99ieB2ljNvcwPZ53EoPX)
- [Apple Podcasts](https://podcasts.apple.com/us/podcast/research-radio/id1866587707)

## Related Project: ToRead

This project is designed to work with [ToRead](https://github.com/fabiogiglietto/toread), which converts Paperpile BibTeX exports into JSON feeds enriched with academic metadata (DOIs, citation counts, open access status).

**The full pipeline:**
1. **Paperpile** - Curate papers in your "To Read" folder
2. **ToRead** - Automatically exports to a JSON feed with enriched metadata
3. **Research-Radio** - Converts papers from the feed into podcast episodes and structured analyses

## Features

- Fetches papers from a JSON feed (compatible with [ToRead](https://github.com/fabiogiglietto/toread))
- Retrieves PDFs from Google Drive (Paperpile integration)
- **Pluggable analyzer framework** — run multiple analysis types per paper:
  - `podcast` — generates a natural two-host conversation script with Claude, produces multi-speaker audio with ElevenLabs
  - `extraction` — extracts methodology, key findings, limitations, and open questions as structured JSON
  - `critical` — produces a critical review covering strengths, weaknesses, reproducibility, and novelty
- Publishes as an RSS podcast feed
- Commits analysis JSONs to `docs/analyses/` for use by downstream tools (e.g. weekly research summaries)
- Automated via GitHub Actions (hourly checks for new papers)

## Requirements

- Python 3.11+
- Anthropic API key (Claude)
- ElevenLabs API key + two voice IDs
- Google Cloud service account with Drive API access
- ffmpeg (for audio concatenation)
- GitHub account (for releases and Actions)

## Setup

1. **Clone and install dependencies:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
   cd YOUR_REPO
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

3. **Set up Google Cloud:**
   - Create a service account with Drive API access
   - Download the JSON key to `credentials/service-account.json`
   - Share your Drive folder with the service account email

4. **Configure GitHub Secrets** (for Actions):
   - `ANTHROPIC_API_KEY`: Your Anthropic API key
   - `ELEVENLABS_API_KEY`: Your ElevenLabs API key
   - `ELEVENLABS_HOST_VOICE_ID`: ElevenLabs voice ID for the host
   - `ELEVENLABS_COHOST_VOICE_ID`: ElevenLabs voice ID for the co-host
   - `GCP_SA_KEY`: Contents of your service account JSON

5. **Customize your podcast:**
   Edit `.env` to set your podcast name, description, host names, and voices:
   ```
   PODCAST_TITLE=Your Podcast Name
   PODCAST_DESCRIPTION=Your podcast description
   PODCAST_AUTHOR=Your Name
   TTS_HOST_NAME=Alex
   TTS_COHOST_NAME=Sam
   ELEVENLABS_HOST_VOICE_ID=<voice-id-from-elevenlabs>
   ELEVENLABS_COHOST_VOICE_ID=<voice-id-from-elevenlabs>
   ```

## Usage

**Run locally:**
```bash
python src/main.py
```

**Run specific analyzers only:**
```bash
ENABLED_ANALYZERS=extraction,critical python src/main.py
```

**Automated (GitHub Actions):**
The workflow runs hourly, checking for new papers and generating episodes automatically.

## How It Works

1. **Feed Parser** — Fetches papers from a JSON feed
2. **Drive Client** — Finds and downloads matching PDFs from Google Drive
3. **Analyzer Framework** — Runs configured analyzers per paper:
   - **Podcast analyzer** — Claude generates a two-host script with expressive delivery cues, ElevenLabs converts it to audio (chunked at 1,800 chars/request, stitched with ffmpeg)
   - **Extraction analyzer** — Claude extracts structured metadata into `docs/analyses/{id}_extraction.json`
   - **Critical analyzer** — Claude produces a critical review into `docs/analyses/{id}_critical.json`
4. **GitHub Uploader** — Uploads audio files to GitHub Releases
5. **Feed Generator** — Creates/updates the RSS podcast feed

## Analyzer Outputs

### Extraction JSON (`docs/analyses/{paper_id}_extraction.json`)
```json
{
  "paper_id": "...",
  "paper_title": "...",
  "analyzed_at": "...",
  "one_sentence_summary": "...",
  "research_context": "...",
  "methodology": {
    "type": "computational",
    "description": "...",
    "datasets": ["..."],
    "tools_and_methods": ["..."]
  },
  "key_findings": ["..."],
  "limitations": ["..."],
  "open_questions": ["..."],
  "future_work": ["..."],
  "keywords": ["..."]
}
```

### Critical JSON (`docs/analyses/{paper_id}_critical.json`)
```json
{
  "paper_id": "...",
  "strengths": ["..."],
  "weaknesses": ["..."],
  "reproducibility": "high|medium|low",
  "novelty": "high|medium|low",
  "significance": "...",
  "recommended_for": ["..."],
  "overall_assessment": "..."
}
```

## Adding a New Analyzer

1. Create `src/analyzers/my_analyzer.py` subclassing `PaperAnalyzer`
2. Set `name = "my_analyzer"` and implement `analyze(paper, paper_text) -> AnalysisResult`
3. Register it in `src/analyzers/__init__.py`
4. Add it to `ENABLED_ANALYZERS`

## LLM Model Configuration

The default model is `claude-sonnet-4-6`. To use a more powerful model:
```
CLAUDE_MODEL=claude-opus-4-6
```

## License

This code is released under the MIT License. See [LICENSE](LICENSE) for details.

You are free to use, modify, and distribute this code to create your own paper-to-podcast pipeline. However, please create your own podcast identity (name, branding, description) rather than using "FG's Research Radio."

## Acknowledgments

Built with:
- [Anthropic Claude](https://www.anthropic.com/) for script generation and structured analysis
- [ElevenLabs](https://elevenlabs.io/) for multi-speaker TTS (Eleven v3 Text-to-Dialogue)
- [Google Drive API](https://developers.google.com/drive) for PDF access
- [feedgen](https://github.com/lkiesow/python-feedgen) for RSS feed generation
