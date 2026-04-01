"""LLM-based summarization for video content using Claude API."""

from __future__ import annotations

import os
from typing import Any

from app.models import VideoMetadata, TranscriptResult, TranscriptStatus
from app.utils import format_timestamp, setup_logging

logger = setup_logging()

# Summary structure returned by the LLM
SUMMARY_KEYS = [
    "one_line_summary",
    "key_claims",
    "key_points",
    "practical_applications",
    "risks_and_caveats",
]


def _get_client() -> Any | None:
    """Get Anthropic client if available."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    except ImportError:
        logger.debug("anthropic package not installed")
        return None


def _build_transcript_text(transcript: TranscriptResult, max_chars: int = 15000) -> str:
    """Build plain text from transcript segments."""
    if transcript.status != TranscriptStatus.SUCCESS or not transcript.segments:
        return ""
    lines = []
    total = 0
    for seg in transcript.segments:
        text = seg.text.strip()
        if not text:
            continue
        ts = format_timestamp(seg.start)
        line = f"[{ts}] {text}"
        total += len(line)
        if total > max_chars:
            lines.append("... (truncated)")
            break
        lines.append(line)
    return "\n".join(lines)


def _build_prompt(meta: VideoMetadata, transcript_text: str) -> str:
    return f"""다음 YouTube 영상의 메타데이터와 자막을 분석하여 구조화된 요약을 생성하세요.

## 영상 정보
- 제목: {meta.title}
- 채널: {meta.channel}
- 설명란: {meta.description[:2000] if meta.description else '(없음)'}

## 자막
{transcript_text if transcript_text else '(자막 없음 — 설명란과 제목 기반으로 분석하세요)'}

## 요청 형식
아래 각 항목을 정확히 작성하세요. 각 섹션을 "### 섹션명" 형식으로 구분하세요.

### 한줄 요약
영상의 핵심 내용을 한 문장으로 요약

### 핵심 주장 5개
1.
2.
3.
4.
5.

### 핵심 포인트
- (3~5개의 핵심 포인트)

### 실무 적용 포인트
- 내 업무/프로젝트에 적용 가능한 점:
- 자동화 관점에서의 인사이트:
- 툴/워크플로우 관점에서의 시사점:

### 리스크 / 한계 / 검증 필요
- 광고성 주장 여부:
- 근거 부족 내용:
- 개인 의견 가능성:
- 검증 필요 내용:

중요: 자막이 없는 경우 제목과 설명란만으로 가능한 범위에서 분석하되, 불확실한 부분은 명확히 표시하세요."""


def summarize_video(
    meta: VideoMetadata,
    transcript: TranscriptResult,
    model: str = "claude-sonnet-4-20250514",
) -> str | None:
    """Generate LLM summary for a video. Returns summary text or None if unavailable."""
    client = _get_client()
    if client is None:
        logger.info("LLM summarization skipped (no ANTHROPIC_API_KEY or anthropic package)")
        return None

    transcript_text = _build_transcript_text(transcript)
    prompt = _build_prompt(meta, transcript_text)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = response.content[0].text
        logger.info("LLM summary generated for %s (%d chars)", meta.id, len(summary))
        return summary
    except Exception as e:
        logger.error("LLM summarization failed for %s: %s", meta.id, e)
        return None


def inject_summary_into_markdown(markdown: str, summary: str) -> str:
    """Replace placeholder sections in markdown with LLM-generated summary."""
    # Parse summary sections
    sections = {}
    current_key = None
    current_lines: list[str] = []

    for line in summary.splitlines():
        if line.startswith("### "):
            if current_key:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = line[4:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_key:
        sections[current_key] = "\n".join(current_lines).strip()

    # Replace placeholder sections
    replacements = {
        "## 한줄 요약\n\n> (수집된 메타데이터와 자막을 바탕으로 직접 작성하거나 LLM에 요약을 요청하세요)":
            f"## 한줄 요약\n\n> {sections.get('한줄 요약', '(요약 생성 실패)')}",
        "## 핵심 주장 5개\n\n1. (자막/설명란 분석 후 작성)\n2.\n3.\n4.\n5.":
            f"## 핵심 주장 5개\n\n{sections.get('핵심 주장 5개', '(생성 실패)')}",
        "## 핵심 포인트\n\n- (자막/설명란에서 도출)\n-\n-":
            f"## 핵심 포인트\n\n{sections.get('핵심 포인트', '(생성 실패)')}",
        "## 실무 적용 포인트\n\n- 내 업무/프로젝트에 적용 가능한 점:\n- 자동화 관점에서의 인사이트:\n- 툴/워크플로우 관점에서의 시사점:":
            f"## 실무 적용 포인트\n\n{sections.get('실무 적용 포인트', '(생성 실패)')}",
        "## 리스크 / 한계 / 검증 필요\n\n- 광고성 주장 여부:\n- 근거 부족 내용:\n- 개인 의견 가능성:\n- 검증 필요 내용:":
            f"## 리스크 / 한계 / 검증 필요\n\n{sections.get('리스크 / 한계 / 검증 필요', '(생성 실패)')}",
    }

    result = markdown
    for old, new in replacements.items():
        result = result.replace(old, new)
    return result
