"""Utility functions for the YouTube research pipeline."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
INBOX_DIR = DATA_DIR / "inbox"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
NOTEBOOKLM_DIR = DATA_DIR / "notebooklm"
NOTION_DIR = DATA_DIR / "notion"
OBSIDIAN_DIR = DATA_DIR / "obsidian"
LOGS_DIR = BASE_DIR / "logs"

LANGUAGE_PRIORITY = ["ko", "en"]

# Ensure directories exist
for d in [RAW_DIR, PROCESSED_DIR, NOTEBOOKLM_DIR, NOTION_DIR, OBSIDIAN_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


def setup_logging() -> logging.Logger:
    """Configure and return the pipeline logger."""
    logger = logging.getLogger("yt_pipeline")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    fh = logging.FileHandler(LOGS_DIR / "pipeline.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from various URL formats."""
    url = url.strip()
    if not url:
        return None

    # youtu.be/VIDEO_ID
    if "youtu.be/" in url:
        parsed = urlparse(url)
        vid = parsed.path.lstrip("/").split("/")[0]
        if vid:
            return vid

    # youtube.com/watch?v=VIDEO_ID
    if "youtube.com" in url:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "v" in qs:
            return qs["v"][0]
        # youtube.com/shorts/VIDEO_ID or youtube.com/embed/VIDEO_ID
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in ("shorts", "embed", "v"):
            return parts[1]

    # Bare video ID (11 chars, alphanumeric + _-)
    if re.match(r"^[A-Za-z0-9_-]{11}$", url):
        return url

    return None


def format_timestamp(seconds: float) -> str:
    """Convert seconds to HH:MM:SS or MM:SS timestamp."""
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_duration(seconds: int) -> str:
    """Format duration in human-readable form."""
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def load_urls(path: Path | None = None) -> list[str]:
    """Load YouTube URLs from the inbox file."""
    if path is None:
        path = INBOX_DIR / "youtube_urls.txt"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    urls = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls
