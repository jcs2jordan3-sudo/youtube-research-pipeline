"""Obsidian vault exporter for YouTube research pipeline."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from app.models import VideoMetadata, TranscriptResult, TranscriptStatus
from app.utils import OBSIDIAN_DIR, format_timestamp, format_duration, setup_logging
from app.formatter import _format_upload_date

logger = setup_logging()


def _sanitize_filename(title: str) -> str:
    """Remove characters that are invalid in file names."""
    sanitized = re.sub(r'[\\/:*?"<>|]', "", title)
    sanitized = sanitized.strip()
    # Limit length to avoid filesystem issues
    if len(sanitized) > 120:
        sanitized = sanitized[:120].rstrip()
    return sanitized


def _build_frontmatter(meta: VideoMetadata) -> str:
    """Build YAML frontmatter block for Obsidian."""
    upload_date = _format_upload_date(meta.upload_date)
    tags = meta.tags[:20] if meta.tags else []
    # Sanitize tags for YAML: remove commas and quotes
    safe_tags = [re.sub(r'[,"\']', "", t).strip() for t in tags if t.strip()]
    tags_str = ", ".join(f'"{t}"' for t in safe_tags) if safe_tags else ""

    view_str = str(meta.view_count) if meta.view_count is not None else ""
    like_str = str(meta.like_count) if meta.like_count is not None else ""
    duration_str = format_duration(meta.duration) if meta.duration else ""

    lines = [
        "---",
        f'title: "{meta.title.replace(chr(34), chr(39))}"',
        f"video_id: \"{meta.id}\"",
        f"url: \"{meta.webpage_url}\"",
        f"channel: \"{meta.channel}\"",
        f"uploader: \"{meta.uploader}\"",
        f"upload_date: {upload_date}",
        f"duration: \"{duration_str}\"",
        f"view_count: {view_str}",
        f"like_count: {like_str}",
        f"tags: [{tags_str}]",
        f"collected_at: \"{meta.collected_at}\"",
        "type: youtube-research",
        "---",
    ]
    return "\n".join(lines)


def _build_obsidian_header(meta: VideoMetadata) -> str:
    """Build the note header with wikilinks for tags."""
    tag_links = " ".join(f"#{t.replace(' ', '_')}" for t in meta.tags[:20]) if meta.tags else ""
    upload_date = _format_upload_date(meta.upload_date)

    lines = [
        f"# {meta.title}",
        "",
        f"**Channel:** [[{meta.channel}]]" if meta.channel else "",
        f"**URL:** {meta.webpage_url}",
        f"**Upload Date:** {upload_date}",
        f"**Duration:** {format_duration(meta.duration)}" if meta.duration else "",
        f"**Views:** {meta.view_count:,}" if meta.view_count is not None else "",
        "",
    ]
    if tag_links:
        lines.append(f"**Tags:** {tag_links}")
        lines.append("")

    return "\n".join(line for line in lines if line is not None)


def _build_obsidian_transcript_summary(transcript: TranscriptResult) -> str:
    """Build a condensed transcript summary for Obsidian notes."""
    if transcript.status != TranscriptStatus.SUCCESS or not transcript.segments:
        return "\n## Transcript Summary\n\n(No transcript available)\n"

    lang_note = f"Language: {transcript.language}"
    if transcript.is_generated:
        lang_note += " (auto-generated)"

    lines = [f"\n## Transcript Summary\n\n> {lang_note}\n"]
    last_ts = -60.0
    count = 0
    for seg in transcript.segments:
        if seg.start - last_ts >= 60.0 and count < 30:
            ts = format_timestamp(seg.start)
            text = seg.text.strip().replace("\n", " ")
            if text:
                lines.append(f"- `{ts}` {text}")
                last_ts = seg.start
                count += 1
    if count == 0:
        lines.append("(No transcript segments extracted)")
    return "\n".join(lines) + "\n"


def _build_obsidian_chapters(meta: VideoMetadata) -> str:
    """Build chapters section with Obsidian formatting."""
    if not meta.chapters:
        return ""
    lines = ["\n## Chapters\n"]
    for ch in meta.chapters:
        ts = format_timestamp(ch.start_time)
        lines.append(f"- `{ts}` {ch.title}")
    return "\n".join(lines) + "\n"


def _build_obsidian_description(meta: VideoMetadata) -> str:
    """Build description section."""
    if not meta.description:
        return ""
    desc = meta.description.strip()
    if len(desc) > 2000:
        desc = desc[:2000] + "\n\n... (truncated)"
    return f"\n## Description\n\n{desc}\n"


def generate_obsidian_note(meta: VideoMetadata, transcript: TranscriptResult) -> str:
    """Generate a full Obsidian-compatible Markdown note for a video."""
    parts = [
        _build_frontmatter(meta),
        "",
        _build_obsidian_header(meta),
        _build_obsidian_chapters(meta),
        _build_obsidian_transcript_summary(transcript),
        _build_obsidian_description(meta),
    ]
    return "\n".join(parts)


def save_obsidian_note(
    meta: VideoMetadata,
    transcript: TranscriptResult,
    vault_path: Path | None = None,
) -> str:
    """Generate and save an Obsidian note, returning the file path."""
    if vault_path is None:
        vault_path = OBSIDIAN_DIR
    vault_path.mkdir(parents=True, exist_ok=True)

    content = generate_obsidian_note(meta, transcript)
    filename = _sanitize_filename(meta.title) or meta.id
    out_path = vault_path / f"{filename}.md"

    # Avoid collisions by appending video ID if file exists
    if out_path.exists():
        out_path = vault_path / f"{filename} ({meta.id}).md"

    out_path.write_text(content, encoding="utf-8")
    logger.info("Saved Obsidian note: %s", out_path)
    return str(out_path)


def generate_obsidian_index(
    videos: list[tuple[VideoMetadata, TranscriptResult]],
) -> str:
    """Generate the content of the Obsidian index note."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "---",
        "type: youtube-research-index",
        f"updated: \"{now}\"",
        f"total_videos: {len(videos)}",
        "---",
        "",
        "# YouTube Research Index",
        "",
        f"*Last updated: {now} | {len(videos)} videos*",
        "",
        "## Videos",
        "",
    ]

    # Group by channel
    by_channel: dict[str, list[VideoMetadata]] = {}
    for meta, _ in videos:
        channel = meta.channel or "Unknown Channel"
        by_channel.setdefault(channel, []).append(meta)

    for channel in sorted(by_channel.keys()):
        lines.append(f"### [[{channel}]]")
        lines.append("")
        for meta in by_channel[channel]:
            note_name = _sanitize_filename(meta.title) or meta.id
            upload_date = _format_upload_date(meta.upload_date)
            lines.append(f"- [[{note_name}]] ({upload_date})")
        lines.append("")

    return "\n".join(lines)


def save_obsidian_index(
    videos: list[tuple[VideoMetadata, TranscriptResult]],
    vault_path: Path | None = None,
) -> str:
    """Generate and save the Obsidian index note, returning the file path."""
    if vault_path is None:
        vault_path = OBSIDIAN_DIR
    vault_path.mkdir(parents=True, exist_ok=True)

    content = generate_obsidian_index(videos)
    out_path = vault_path / "_YouTube Research Index.md"
    out_path.write_text(content, encoding="utf-8")
    logger.info("Saved Obsidian index: %s", out_path)
    return str(out_path)
