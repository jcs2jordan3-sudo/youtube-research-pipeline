"""Data models for the YouTube research pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class TranscriptStatus(str, Enum):
    SUCCESS = "success"
    NOT_AVAILABLE = "not_available"
    EXTRACTION_FAILED = "extraction_failed"
    LANGUAGE_NOT_SUPPORTED = "language_not_supported"
    DISABLED = "disabled"


@dataclass
class TranscriptSegment:
    text: str
    start: float
    duration: float
    language: str = ""
    is_generated: bool = False


@dataclass
class TranscriptResult:
    video_id: str
    status: TranscriptStatus
    language: str = ""
    is_generated: bool = False
    segments: list[TranscriptSegment] = field(default_factory=list)
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "status": self.status.value,
            "language": self.language,
            "is_generated": self.is_generated,
            "segments": [
                {
                    "text": s.text,
                    "start": s.start,
                    "duration": s.duration,
                    "language": s.language,
                    "is_generated": s.is_generated,
                }
                for s in self.segments
            ],
            "error_message": self.error_message,
        }


@dataclass
class ChapterInfo:
    title: str
    start_time: float
    end_time: float | None = None


@dataclass
class VideoMetadata:
    id: str
    title: str
    uploader: str = ""
    channel: str = ""
    channel_id: str = ""
    upload_date: str = ""
    duration: int = 0
    description: str = ""
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    thumbnail: str = ""
    webpage_url: str = ""
    chapters: list[ChapterInfo] = field(default_factory=list)
    view_count: int | None = None
    like_count: int | None = None
    availability: str = ""
    collected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class ProcessingResult:
    url: str
    video_id: str
    success: bool
    metadata_ok: bool = False
    transcript_ok: bool = False
    transcript_status: str = ""
    markdown_path: str = ""
    error: str = ""


@dataclass
class PipelineManifest:
    run_at: str = ""
    total_urls: int = 0
    success_count: int = 0
    failure_count: int = 0
    transcript_success_count: int = 0
    results: list[ProcessingResult] = field(default_factory=list)
    failed_urls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_at": self.run_at,
            "total_urls": self.total_urls,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "transcript_success_count": self.transcript_success_count,
            "results": [asdict(r) for r in self.results],
            "failed_urls": self.failed_urls,
        }

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
