import os
from dotenv import load_dotenv

load_dotenv()

# Google Cloud / Drive
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
GOOGLE_DRIVE_FOLDER_ID = os.getenv(
    "GOOGLE_DRIVE_FOLDER_ID",
    "1gluNDqRQkyqxa_WIASaaoNEItrDlETkn"  # PaperPile PDFs folder
)

# Anthropic / Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# ElevenLabs TTS
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_HOST_VOICE_ID = os.getenv("ELEVENLABS_HOST_VOICE_ID", "")
ELEVENLABS_COHOST_VOICE_ID = os.getenv("ELEVENLABS_COHOST_VOICE_ID", "")
# Names used in the podcast script (displayed in dialogue, not voice IDs)
TTS_HOST_NAME = os.getenv("TTS_HOST_NAME", "Alex")
TTS_COHOST_NAME = os.getenv("TTS_COHOST_NAME", "Sam")

# GitHub
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")

# Feed
FEED_URL = os.getenv(
    "FEED_URL",
    "https://raw.githubusercontent.com/fabiogiglietto/toread/main/output/feed.json"
)

# Podcast metadata
PODCAST_TITLE = os.getenv("PODCAST_TITLE", "Research Radio")
PODCAST_DESCRIPTION = os.getenv(
    "PODCAST_DESCRIPTION",
    "AI-generated podcast discussions of academic papers"
)
PODCAST_AUTHOR = os.getenv("PODCAST_AUTHOR", "Research Radio")
PODCAST_EMAIL = os.getenv("PODCAST_EMAIL", "")
PODCAST_WEBSITE = os.getenv("PODCAST_WEBSITE", "")

# Paths
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
AUDIO_DIR = os.path.join(PROJECT_ROOT, "audio")
DOCS_DIR = os.path.join(PROJECT_ROOT, "docs")
CREDENTIALS_DIR = os.path.join(PROJECT_ROOT, "credentials")
PROCESSED_FILE = os.path.join(DATA_DIR, "processed.json")
EPISODES_FILE = os.path.join(DOCS_DIR, "episodes.json")
FEED_FILE = os.path.join(DOCS_DIR, "feed.xml")
ANALYSES_DIR = os.path.join(DOCS_DIR, "analyses")

# Analyzer framework — comma-separated list of analyzers to run per paper
ENABLED_ANALYZERS = [
    a.strip()
    for a in os.getenv("ENABLED_ANALYZERS", "podcast,extraction,critical").split(",")
    if a.strip()
]
