"""Markdown document generator for individual videos."""

from __future__ import annotations

from app.models import VideoMetadata, TranscriptResult, TranscriptStatus
from app.utils import PROCESSED_DIR, format_timestamp, format_duration, setup_logging

logger = setup_logging()


def _format_upload_date(raw: str) -> str:
    """Convert YYYYMMDD to YYYY-MM-DD."""
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _build_header(meta: VideoMetadata) -> str:
    tags_str = ", ".join(meta.tags) if meta.tags else "N/A"
    view_str = f"{meta.view_count:,}" if meta.view_count is not None else "N/A"
    duration_str = format_duration(meta.duration) if meta.duration else "N/A"

    return f"""# {meta.title}

- **URL:** {meta.webpage_url}
- **Video ID:** {meta.id}
- **Channel:** {meta.channel}
- **Uploader:** {meta.uploader}
- **Upload Date:** {_format_upload_date(meta.upload_date)}
- **Duration:** {duration_str}
- **View Count:** {view_str}
- **Tags:** {tags_str}
- **Collected At:** {meta.collected_at}
"""


def _build_placeholder_sections() -> str:
    return """
## 한줄 요약

> (수집된 메타데이터와 자막을 바탕으로 직접 작성하거나 LLM에 요약을 요청하세요)

## 핵심 주장 5개

1. (자막/설명란 분석 후 작성)
2.
3.
4.
5.

## 핵심 포인트

- (자막/설명란에서 도출)
-
-

## 실무 적용 포인트

- 내 업무/프로젝트에 적용 가능한 점:
- 자동화 관점에서의 인사이트:
- 툴/워크플로우 관점에서의 시사점:

## 리스크 / 한계 / 검증 필요

- 광고성 주장 여부:
- 근거 부족 내용:
- 개인 의견 가능성:
- 검증 필요 내용:
"""


def _build_description_section(meta: VideoMetadata) -> str:
    if not meta.description:
        return "\n## 설명란 요약\n\n(설명란 없음)\n"
    # Truncate very long descriptions for readability
    desc = meta.description.strip()
    if len(desc) > 3000:
        desc = desc[:3000] + "\n\n... (이하 생략, 원문은 raw/*.info.json 참조)"
    return f"\n## 설명란 요약\n\n{desc}\n"


def _build_chapters_section(meta: VideoMetadata) -> str:
    if not meta.chapters:
        return "\n## 챕터 정보\n\n(챕터 정보 없음)\n"
    lines = ["\n## 챕터 정보\n"]
    for ch in meta.chapters:
        ts = format_timestamp(ch.start_time)
        lines.append(f"- [{ts}] {ch.title}")
    return "\n".join(lines) + "\n"


def _build_timestamp_summary(transcript: TranscriptResult) -> str:
    if transcript.status != TranscriptStatus.SUCCESS or not transcript.segments:
        return "\n## 타임스탬프 요약\n\n(자막 없음 — 타임스탬프 요약 불가)\n"

    lines = ["\n## 타임스탬프 요약\n"]
    # Sample key segments: pick one per ~60 seconds
    last_ts = -60.0
    count = 0
    for seg in transcript.segments:
        if seg.start - last_ts >= 60.0 and count < 30:
            ts = format_timestamp(seg.start)
            text = seg.text.strip().replace("\n", " ")
            if text:
                lines.append(f"- [{ts}] {text}")
                last_ts = seg.start
                count += 1
    if count == 0:
        lines.append("(타임스탬프 추출 결과 없음)")
    return "\n".join(lines) + "\n"


def _build_transcript_section(transcript: TranscriptResult) -> str:
    if transcript.status != TranscriptStatus.SUCCESS or not transcript.segments:
        status_msg = {
            TranscriptStatus.NOT_AVAILABLE: "자막 없음",
            TranscriptStatus.EXTRACTION_FAILED: "자막 추출 실패",
            TranscriptStatus.LANGUAGE_NOT_SUPPORTED: "지원 언어 없음",
            TranscriptStatus.DISABLED: "자막 비활성화됨",
        }.get(transcript.status, transcript.error_message or "자막 없음")
        return f"\n## 원문 자막\n\n({status_msg})\n"

    lang_note = f"언어: {transcript.language}"
    if transcript.is_generated:
        lang_note += " (자동 생성)"

    lines = [f"\n## 원문 자막\n\n> {lang_note}\n"]
    for seg in transcript.segments:
        ts = format_timestamp(seg.start)
        text = seg.text.strip().replace("\n", " ")
        if text:
            lines.append(f"- [{ts}] {text}")
    return "\n".join(lines) + "\n"


def generate_markdown(
    meta: VideoMetadata,
    transcript: TranscriptResult,
) -> str:
    """Generate a full Markdown document for a single video."""
    parts = [
        _build_header(meta),
        _build_placeholder_sections(),
        _build_timestamp_summary(transcript),
        _build_description_section(meta),
        _build_chapters_section(meta),
        _build_transcript_section(transcript),
    ]
    return "\n".join(parts)


def save_markdown(meta: VideoMetadata, transcript: TranscriptResult) -> str:
    """Generate and save Markdown, returning the file path."""
    content = generate_markdown(meta, transcript)
    out_path = PROCESSED_DIR / f"{meta.id}.md"
    out_path.write_text(content, encoding="utf-8")
    logger.info("Saved markdown: %s", out_path)
    return str(out_path)
