"""YouTube metadata scraper using yt-dlp Python API."""

from __future__ import annotations

import json
from typing import Any

import yt_dlp

from app.models import VideoMetadata, ChapterInfo
from app.utils import RAW_DIR, setup_logging, extract_video_id

logger = setup_logging()


def _build_ydl_opts() -> dict[str, Any]:
    """Build yt-dlp options for metadata-only extraction."""
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "ignoreerrors": False,
    }


def _parse_chapters(info: dict[str, Any]) -> list[ChapterInfo]:
    """Parse chapter information from yt-dlp info dict."""
    chapters_raw = info.get("chapters") or []
    chapters = []
    for ch in chapters_raw:
        chapters.append(
            ChapterInfo(
                title=ch.get("title", ""),
                start_time=ch.get("start_time", 0),
                end_time=ch.get("end_time"),
            )
        )
    return chapters


def _info_to_metadata(info: dict[str, Any], collected_at: str) -> VideoMetadata:
    """Convert yt-dlp info dict to VideoMetadata."""
    return VideoMetadata(
        id=info.get("id", ""),
        title=info.get("title", ""),
        uploader=info.get("uploader", ""),
        channel=info.get("channel", ""),
        channel_id=info.get("channel_id", ""),
        upload_date=info.get("upload_date", ""),
        duration=int(info.get("duration") or 0),
        description=info.get("description", ""),
        tags=info.get("tags") or [],
        categories=info.get("categories") or [],
        thumbnail=info.get("thumbnail", ""),
        webpage_url=info.get("webpage_url", ""),
        chapters=_parse_chapters(info),
        view_count=info.get("view_count"),
        like_count=info.get("like_count"),
        availability=info.get("availability", ""),
        collected_at=collected_at,
    )


def _save_raw_info(video_id: str, info: dict[str, Any]) -> None:
    """Save raw info.json, stripping large binary/format data."""
    # Remove heavy keys that aren't needed for research
    skip_keys = {
        "formats", "requested_formats", "requested_downloads",
        "thumbnails", "http_headers", "downloader_options",
    }
    filtered = {k: v for k, v in info.items() if k not in skip_keys}
    out_path = RAW_DIR / f"{video_id}.info.json"
    out_path.write_text(
        json.dumps(filtered, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger.debug("Saved raw info: %s", out_path)


def scrape_video(url: str, collected_at: str) -> VideoMetadata | None:
    """Scrape metadata for a single YouTube video.

    Returns VideoMetadata on success, None on failure.
    """
    video_id = extract_video_id(url)
    logger.info("Scraping: %s (id=%s)", url, video_id)

    opts = _build_ydl_opts()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                logger.error("No info returned for %s", url)
                return None

            actual_id = info.get("id", video_id or "unknown")
            _save_raw_info(actual_id, info)

            metadata = _info_to_metadata(info, collected_at)
            logger.info("Scraped OK: %s — %s", actual_id, metadata.title)
            return metadata

    except yt_dlp.utils.DownloadError as e:
        logger.error("yt-dlp DownloadError for %s: %s", url, e)
        return None
    except Exception as e:
        logger.error("Unexpected error scraping %s: %s", url, e, exc_info=True)
        return None
