"""
Podcast Analyzer - Generates a two-host podcast episode from a paper.

Uses Claude to generate a conversation script (with ElevenLabs audio delivery
tags), then ElevenLabs Text-to-Dialogue for multi-speaker audio. Uploads the
MP3 to a GitHub Release and adds an entry to the RSS feed.
"""

import os
import random
import re
from datetime import datetime, timezone
from typing import Optional

from .base import PaperAnalyzer, AnalysisResult
from src.claude_client import ClaudeClient
from src.tts_elevenlabs import ElevenLabsTTS
from src.github_uploader import upload_audio_to_release
from src.feed_generator import create_episode_from_paper, add_episode
from config import (
    ANTHROPIC_API_KEY,
    ELEVENLABS_API_KEY,
    AUDIO_DIR,
    PODCAST_TITLE,
    TTS_HOST_NAME,
    TTS_COHOST_NAME,
)


def _sanitize_filename(paper_id: str) -> str:
    name = paper_id.replace("bibtex:", "").replace("/", "_").replace("\\", "_")
    return name[:100]


class PodcastAnalyzer(PaperAnalyzer):
    """Generates a podcast episode (script → audio → GitHub Release → RSS)."""

    name = "podcast"

    def __init__(self):
        self.claude = ClaudeClient()
        self.tts = ElevenLabsTTS()

    # ------------------------------------------------------------------
    # Script generation
    # ------------------------------------------------------------------

    def _generate_script(self, paper_text: str, paper_title: str) -> Optional[str]:
        host_name = TTS_HOST_NAME
        cohost_name = TTS_COHOST_NAME

        # ~5% chance of including a light self-aware AI humour moment
        ai_humor = ""
        if random.random() < 0.05:
            ai_humor = (
                "\n- Include one brief, self-aware joke about being AI-generated hosts "
                "(e.g., a playful quip about mispronunciations or audio glitches). "
                "Keep it light and don't overdo it."
            )

        prompt = f"""You are a podcast script writer. Create an engaging episode of "{PODCAST_TITLE}",
a podcast featuring deep-dive discussions of recent academic papers in computational
social science, platform studies, misinformation research, and the evolving landscape
of social media and AI.

The conversation is between two hosts:
- Host (named {host_name}): guides the discussion, provides context
- Cohost (named {cohost_name}): offers analysis, asks probing questions, adds perspective

Important: These are podcast hosts discussing the paper — NOT the authors.
Refer to authors in third person (e.g., "The researchers found..." or "According to the authors...").

Guidelines:
- Open by welcoming listeners to {PODCAST_TITLE}; hosts introduce themselves by name, then introduce the paper's topic and authors
- Mention authors by name naturally (e.g., "As Boyd argues..." or "The team led by Ferrara found...")
- Explain key findings and methodology in accessible language
- Both hosts share insights and build on each other's points
- Discuss implications and significance for the field
- End with clear takeaways for listeners
- At the very end, remind listeners the full paper reference is in the episode description, and encourage them to subscribe
- Use natural, conversational language{ai_humor}
- Optionally use ElevenLabs delivery tags for expressive audio: [excited], [thoughtfully], [laughing], [sighs], [whispering], etc. — use sparingly and naturally
- Target length: 8–12 minutes of dialogue (roughly 1,200–1,800 words)
- Format EVERY line exactly as "Host: [dialogue]" or "Cohost: [dialogue]" — no other prefixes

Paper Title: {paper_title}

Paper Content:
{paper_text[:60000]}

Generate the podcast script now:"""

        return self.claude.generate(prompt, max_tokens=8192, temperature=0.7)

    def _generate_episode_title(self, script: str, paper_title: str) -> Optional[str]:
        prompt = f"""You are a podcast producer for "{PODCAST_TITLE}".

Based on the following podcast transcript, generate a compelling episode title that:
- Is catchy and engaging for podcast listeners
- Captures the main theme or most interesting finding discussed
- Is concise (ideally 5–10 words, maximum 15 words)
- Sounds like a podcast episode title, not an academic paper title
- Does NOT start with "{PODCAST_TITLE}:" (that prefix is added separately)

Original paper title (for context): {paper_title}

Podcast transcript:
{script[:15000]}

Output ONLY the episode title — no quotes, no explanation."""

        title = self.claude.generate(prompt, max_tokens=100, temperature=0.9)
        if title:
            title = title.strip().strip("\"'")
        return title

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def analyze(self, paper, paper_text: str) -> AnalysisResult:
        print(f"  [podcast] Generating podcast for: {paper.title}")

        os.makedirs(AUDIO_DIR, exist_ok=True)
        audio_filename = f"{_sanitize_filename(paper.id)}.mp3"
        audio_path = os.path.join(AUDIO_DIR, audio_filename)

        # 1. Generate script
        print("  [podcast] Generating script with Claude...")
        script = self._generate_script(paper_text, paper.title)
        if not script:
            return AnalysisResult(
                analyzer_name=self.name, paper_id=paper.id,
                success=False, error="Script generation failed",
            )

        # 2. Generate episode title
        print("  [podcast] Generating episode title...")
        episode_title = self._generate_episode_title(script, paper.title)
        if episode_title:
            print(f"  [podcast] Title: {episode_title}")
        else:
            print("  [podcast] Warning: title generation failed, using paper title")

        # 3. Generate audio via ElevenLabs
        print("  [podcast] Converting to audio via ElevenLabs...")
        if not self.tts.generate(script, audio_path):
            return AnalysisResult(
                analyzer_name=self.name, paper_id=paper.id,
                success=False, error="TTS generation failed",
            )

        audio_size = os.path.getsize(audio_path)
        audio_duration = self.tts.get_audio_duration(audio_path)
        print(f"  [podcast] Audio: {audio_filename} ({audio_size / 1024 / 1024:.1f} MB, "
              f"{audio_duration // 60}:{audio_duration % 60:02d})")

        # 4. Upload to GitHub Release
        print("  [podcast] Uploading to GitHub Release...")
        if not upload_audio_to_release(audio_path):
            return AnalysisResult(
                analyzer_name=self.name, paper_id=paper.id,
                success=False, error="GitHub upload failed",
            )

        # 5. Create and persist episode
        pub_date = datetime.now(timezone.utc)
        paper_year = None
        if paper.date_published:
            m = re.search(r"(\d{4})", paper.date_published)
            if m:
                paper_year = m.group(1)

        episode = create_episode_from_paper(
            paper_id=paper.id,
            paper_title=paper.title,
            paper_authors=paper.authors,
            audio_filename=audio_filename,
            audio_size=audio_size,
            duration=audio_duration,
            pub_date=pub_date,
            paper_url=paper.external_url or paper.url,
            paper_year=paper_year,
            episode_title=episode_title,
        )
        add_episode(episode)

        print(f"  [podcast] Done: {paper.title}")
        return AnalysisResult(
            analyzer_name=self.name,
            paper_id=paper.id,
            success=True,
            artifacts=[audio_path],
            data={
                "episode_title": episode_title or paper.title,
                "audio_filename": audio_filename,
                "audio_url": episode.audio_url,
                "duration": audio_duration,
            },
        )
