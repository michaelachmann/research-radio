#!/usr/bin/env python3
"""
Research Radio - Main Orchestrator

Converts academic papers into podcast episodes and structured analyses using:
- Google Drive (Paperpile PDFs)
- Claude (script generation, structured extraction, critical review)
- ElevenLabs (multi-speaker TTS)
"""

import os
import sys
from datetime import datetime, timedelta, timezone

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    ANTHROPIC_API_KEY,
    ELEVENLABS_API_KEY,
    FEED_URL,
    PROCESSED_FILE,
    AUDIO_DIR,
    ANALYSES_DIR,
    GOOGLE_APPLICATION_CREDENTIALS,
    GOOGLE_DRIVE_FOLDER_ID,
    ENABLED_ANALYZERS,
)
from src.feed_parser import (
    Paper,
    parse_papers,
    fetch_feed,
    load_processed_ids,
    save_processed_id,
)
from src.drive_client import DriveClient
from src.feed_generator import generate_podcast_feed, load_episodes
from src.analyzers import load_analyzers, AnalysisResult

# Rate limiting: minimum hours between podcast episode publications
MIN_HOURS_BETWEEN_EPISODES = 24


def sanitize_filename(paper_id: str) -> str:
    """Convert paper ID to a safe filename."""
    name = paper_id.replace("bibtex:", "").replace("/", "_").replace("\\", "_")
    return name[:100]


def can_publish_new_episode() -> tuple[bool, str]:
    """
    Check if enough time has passed since the last podcast episode.

    Returns:
        (can_publish, reason_string)
    """
    episodes = load_episodes()
    if not episodes:
        return True, "No existing episodes"

    latest = max(episodes, key=lambda e: e.pub_date)
    hours_since = (datetime.now(timezone.utc) - latest.pub_date).total_seconds() / 3600

    if hours_since >= MIN_HOURS_BETWEEN_EPISODES:
        return True, f"{hours_since:.1f}h since last episode"
    else:
        remaining = MIN_HOURS_BETWEEN_EPISODES - hours_since
        return False, f"Only {hours_since:.1f}h since last episode — wait {remaining:.1f}h more"


def process_paper(
    paper: Paper,
    drive_client: DriveClient,
    analyzers,
) -> dict[str, AnalysisResult]:
    """
    Run all configured analyzers on a single paper.

    The podcast analyzer is subject to rate limiting; others always run.
    Returns a dict mapping analyzer_name → AnalysisResult.
    """
    print(f"\n{'='*60}")
    print(f"Processing: {paper.title}")
    print(f"ID: {paper.id}")
    print(f"Authors: {', '.join(paper.authors) if paper.authors else 'Unknown'}")
    print("=" * 60)

    # Extract PDF text once, share across all analyzers
    print("\n[PDF] Fetching text from Google Drive...")
    paper_text = drive_client.get_pdf_text(paper)
    if not paper_text:
        print("  Failed to extract PDF text. Skipping paper.")
        return {}
    print(f"  Extracted {len(paper_text):,} characters")

    results: dict[str, AnalysisResult] = {}

    for analyzer in analyzers:
        print(f"\n[{analyzer.name.upper()}]")

        # Rate-limit check applies only to podcast episodes
        if analyzer.name == "podcast":
            can_pub, reason = can_publish_new_episode()
            print(f"  Rate limit check: {reason}")
            if not can_pub:
                print(f"  Skipping podcast — will queue for next eligible run.")
                continue

        try:
            result = analyzer.analyze(paper, paper_text)
            results[analyzer.name] = result
            if not result.success:
                print(f"  [{analyzer.name}] Failed: {result.error}")
        except Exception as e:
            import traceback
            print(f"  [{analyzer.name}] Unexpected error: {e}")
            traceback.print_exc()
            results[analyzer.name] = AnalysisResult(
                analyzer_name=analyzer.name,
                paper_id=paper.id,
                success=False,
                error=str(e),
            )

    return results


def get_papers_from_drive(
    drive_client: DriveClient,
    processed_file: str,
    max_age_days: int = 30,
) -> list[Paper]:
    """
    Return unprocessed papers that have a matching PDF in Drive modified
    within the last max_age_days.
    """
    feed_data = fetch_feed(FEED_URL)
    all_papers = parse_papers(feed_data)
    processed_ids = load_processed_ids(processed_file)

    cutoff_str = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days)
    ).strftime("%Y-%m-%d")

    papers_with_pdfs = []
    for paper in all_papers:
        if paper.id in processed_ids:
            continue
        pdf_info = drive_client.find_pdf(paper)
        if pdf_info:
            modified_time = pdf_info.get("modifiedTime", "")
            if modified_time >= cutoff_str:
                papers_with_pdfs.append(paper)

    return papers_with_pdfs


def main():
    """Main entry point."""
    print("=" * 60)
    print("Research Radio - Paper to Podcast & Analysis Pipeline")
    print("=" * 60)

    # Validate required credentials
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not ELEVENLABS_API_KEY and "podcast" in ENABLED_ANALYZERS:
        missing.append("ELEVENLABS_API_KEY (required for podcast analyzer)")
    if not GOOGLE_APPLICATION_CREDENTIALS:
        missing.append("GOOGLE_APPLICATION_CREDENTIALS")
    if missing:
        for m in missing:
            print(f"Error: {m} not set")
        sys.exit(1)

    # Ensure output directories exist
    os.makedirs(AUDIO_DIR, exist_ok=True)
    os.makedirs(ANALYSES_DIR, exist_ok=True)

    # Load analyzers
    analyzers = load_analyzers(ENABLED_ANALYZERS)
    if not analyzers:
        print("Error: no valid analyzers configured in ENABLED_ANALYZERS")
        sys.exit(1)
    print(f"\nEnabled analyzers: {[a.name for a in analyzers]}")

    # Initialize Drive client
    print("\nInitializing Google Drive client...")
    drive_client = DriveClient(
        credentials_path=GOOGLE_APPLICATION_CREDENTIALS,
        folder_id=GOOGLE_DRIVE_FOLDER_ID,
    )

    # Fetch new papers
    print(f"\nFetching feed: {FEED_URL}")
    print(f"Drive folder: {GOOGLE_DRIVE_FOLDER_ID}")
    print("Considering PDFs modified in the last 30 days")

    papers = get_papers_from_drive(drive_client, PROCESSED_FILE, max_age_days=30)

    if not papers:
        print("\nNo new papers with matching PDFs found.")
        return

    print(f"\nFound {len(papers)} new paper(s) with PDFs in Drive:")
    for i, paper in enumerate(papers, 1):
        print(f"  {i}. {paper.title}")

    # Process one paper per run (rate-limiting for podcast; others always run)
    paper_to_process = papers[0]
    remaining = len(papers) - 1

    print(f"\nProcessing 1 paper this run")
    if remaining > 0:
        print(f"  {remaining} paper(s) queued for future runs")

    # Run analyzers
    results = process_paper(paper_to_process, drive_client, analyzers)

    # Mark as processed if at least one analyzer succeeded
    any_success = any(r.success for r in results.values())
    if any_success:
        save_processed_id(PROCESSED_FILE, paper_to_process.id)

    # Regenerate RSS feed if podcast ran successfully
    if results.get("podcast") and results["podcast"].success:
        print("\n" + "=" * 60)
        print("Generating podcast RSS feed...")
        generate_podcast_feed()

    # Summary
    print("\n" + "=" * 60)
    print("Summary:")
    for name, result in results.items():
        status = "OK" if result.success else f"FAILED ({result.error})"
        print(f"  {name}: {status}")
    if remaining > 0:
        print(f"  Queued for later: {remaining} paper(s)")
    print("=" * 60)


if __name__ == "__main__":
    main()
