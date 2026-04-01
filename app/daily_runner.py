"""Daily automated runner: search YouTube → scrape → upload to Notion as nested pages."""

from __future__ import annotations

import sys
import io
import json
from datetime import datetime
from pathlib import Path

# Fix Windows encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Ensure project root is on sys.path for direct execution
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from dotenv import load_dotenv
load_dotenv()

from app.utils import (
    setup_logging, INBOX_DIR, DATA_DIR, RAW_DIR, PROCESSED_DIR,
)

logger = setup_logging()

DEFAULT_SEARCH_QUERIES = [
    "Claude Code 2026",
    "Claude Code tutorial",
    "Claude Code tips",
]
DEFAULT_MAX_RESULTS = 30


def search_youtube(
    queries: list[str] | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> list[dict]:
    """Search YouTube for videos matching queries, return metadata list sorted by views."""
    import yt_dlp

    if queries is None:
        queries = DEFAULT_SEARCH_QUERIES

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "playlistend": max_results,
    }

    seen_ids: set[str] = set()
    all_urls: list[str] = []

    for query in queries:
        logger.info("Searching YouTube: %s", query)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
                if info and "entries" in info:
                    for e in info["entries"]:
                        if e and e.get("id") and e["id"] not in seen_ids:
                            seen_ids.add(e["id"])
                            all_urls.append(f"https://www.youtube.com/watch?v={e['id']}")
        except Exception as e:
            logger.error("Search failed for '%s': %s", query, e)

    logger.info("Found %d unique URLs from %d queries", len(all_urls), len(queries))

    # Get full metadata for sorting
    opts2 = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
    }

    videos = []
    with yt_dlp.YoutubeDL(opts2) as ydl:
        for url in all_urls:
            try:
                info = ydl.extract_info(url, download=False)
                if info:
                    videos.append({
                        "id": info.get("id", ""),
                        "title": info.get("title", ""),
                        "channel": info.get("channel", ""),
                        "upload_date": info.get("upload_date", ""),
                        "view_count": info.get("view_count", 0) or 0,
                        "url": info.get("webpage_url", url),
                    })
            except Exception:
                pass

    # Sort by view count (highest first)
    videos.sort(key=lambda x: x["view_count"], reverse=True)
    return videos


def write_url_file(videos: list[dict], max_urls: int = 20) -> Path:
    """Write top URLs to the inbox file. Returns the file path."""
    today = datetime.now().strftime("%Y-%m-%d")
    url_file = INBOX_DIR / "youtube_urls.txt"
    lines = [f"# Auto-generated: {today} Claude Code daily search\n"]
    for v in videos[:max_urls]:
        lines.append(v["url"])
    url_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote %d URLs to %s", min(len(videos), max_urls), url_file)
    return url_file


def run_daily(
    queries: list[str] | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_urls: int = 20,
    notion_nested: bool = True,
) -> None:
    """Full daily pipeline: search → scrape → upload nested to Notion."""
    from app.main import run_pipeline
    from app.notion_export import upload_nested_to_notion

    today = datetime.now().strftime("%Y-%m-%d")
    search_label = "Claude Code"

    print(f"\n{'='*60}")
    print(f" Daily Runner: {today}")
    print(f" Search: {search_label}")
    print(f"{'='*60}\n")

    # Step 1: Search YouTube
    print("Step 1: Searching YouTube...")
    videos = search_youtube(queries, max_results)
    if not videos:
        print("No videos found. Exiting.")
        return

    print(f"  Found {len(videos)} videos")
    for i, v in enumerate(videos[:5], 1):
        print(f"  {i}. [{v['view_count']:,} views] {v['title'][:60]}")

    # Step 2: Write URL file
    print(f"\nStep 2: Writing top {max_urls} URLs...")
    url_file = write_url_file(videos, max_urls)

    # Step 3: Run pipeline
    print("\nStep 3: Running pipeline...")
    manifest = run_pipeline(
        urls_path=url_file,
        theme=f"Claude Code Daily - {today}",
        skip_existing=True,
        parallel=4,
    )

    # Step 4: Upload to Notion as nested pages
    if notion_nested and manifest.success_count > 0:
        print("\nStep 4: Uploading to Notion (nested pages)...")
        # Rebuild video list from processed data
        from app.scraper import scrape_video
        from app.transcript import extract_transcript
        from app.models import VideoMetadata, TranscriptResult
        import json as _json

        notion_videos: list[tuple[VideoMetadata, TranscriptResult]] = []
        for result in manifest.results:
            if result.success:
                # Load from raw files
                info_path = RAW_DIR / f"{result.video_id}.info.json"
                tr_path = RAW_DIR / f"{result.video_id}.transcript.json"
                if info_path.exists() and tr_path.exists():
                    info_data = _json.loads(info_path.read_text(encoding="utf-8"))
                    tr_data = _json.loads(tr_path.read_text(encoding="utf-8"))

                    from app.models import ChapterInfo, TranscriptSegment, TranscriptStatus
                    meta = VideoMetadata(
                        id=info_data.get("id", ""),
                        title=info_data.get("title", ""),
                        uploader=info_data.get("uploader", ""),
                        channel=info_data.get("channel", ""),
                        channel_id=info_data.get("channel_id", ""),
                        upload_date=info_data.get("upload_date", ""),
                        duration=int(info_data.get("duration") or 0),
                        description=info_data.get("description", ""),
                        tags=info_data.get("tags") or [],
                        categories=info_data.get("categories") or [],
                        thumbnail=info_data.get("thumbnail", ""),
                        webpage_url=info_data.get("webpage_url", ""),
                        view_count=info_data.get("view_count"),
                        like_count=info_data.get("like_count"),
                        collected_at=info_data.get("collected_at", ""),
                    )
                    tr = TranscriptResult(
                        video_id=tr_data.get("video_id", ""),
                        status=TranscriptStatus(tr_data.get("status", "extraction_failed")),
                        language=tr_data.get("language", ""),
                        is_generated=tr_data.get("is_generated", False),
                        segments=[
                            TranscriptSegment(
                                text=s.get("text", ""),
                                start=s.get("start", 0),
                                duration=s.get("duration", 0),
                                language=s.get("language", ""),
                                is_generated=s.get("is_generated", False),
                            )
                            for s in tr_data.get("segments", [])
                        ],
                    )
                    notion_videos.append((meta, tr))

        if notion_videos:
            result = upload_nested_to_notion(notion_videos, search_query=search_label)
            print(f"  Notion result: {result['uploaded']} uploaded, {result['failed']} failed")
            if result.get("parent_page_id"):
                print(f"  Parent page ID: {result['parent_page_id']}")
        else:
            print("  No videos to upload to Notion.")
    else:
        print("\nStep 4: Skipped (no successful videos or Notion not enabled)")

    print(f"\n{'='*60}")
    print(f" Daily Runner Complete: {today}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Daily YouTube research runner")
    parser.add_argument(
        "--query", "-q",
        type=str,
        nargs="+",
        default=None,
        help="Search queries (default: Claude Code related)",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        help=f"Max results per query (default: {DEFAULT_MAX_RESULTS})",
    )
    parser.add_argument(
        "--max-urls",
        type=int,
        default=20,
        help="Max URLs to process (default: 20)",
    )
    parser.add_argument(
        "--no-notion",
        action="store_true",
        default=False,
        help="Skip Notion upload",
    )
    args = parser.parse_args()

    run_daily(
        queries=args.query,
        max_results=args.max_results,
        max_urls=args.max_urls,
        notion_nested=not args.no_notion,
    )
