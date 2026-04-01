"""NotebookLM research brief generator — aggregates multiple video documents."""

from __future__ import annotations

from datetime import datetime

from app.models import VideoMetadata, TranscriptResult, TranscriptStatus
from app.utils import NOTEBOOKLM_DIR, format_timestamp, setup_logging

logger = setup_logging()


def _build_source_list(videos: list[tuple[VideoMetadata, TranscriptResult]]) -> str:
    lines = ["## Source List\n"]
    for i, (meta, _) in enumerate(videos, 1):
        lines.append(f"{i}. **{meta.title}** — {meta.webpage_url}")
    return "\n".join(lines) + "\n"


def _build_cross_source_summary(videos: list[tuple[VideoMetadata, TranscriptResult]]) -> str:
    lines = ["## Cross-Source Summary\n"]
    for meta, tr in videos:
        status = "자막 있음" if tr.status == TranscriptStatus.SUCCESS else "자막 없음"
        duration_min = meta.duration // 60 if meta.duration else 0
        lines.append(f"- **{meta.title}** ({meta.channel}, {duration_min}분, {status})")
    lines.append("")
    lines.append("> 위 영상들의 공통 주제와 교차 분석은 NotebookLM에서 수행하세요.")
    return "\n".join(lines) + "\n"


def _collect_all_tags(videos: list[tuple[VideoMetadata, TranscriptResult]]) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for meta, _ in videos:
        for tag in meta.tags:
            lower = tag.lower().strip()
            if lower and lower not in seen:
                seen.add(lower)
                tags.append(tag.strip())
    return tags[:20]


def _build_transcript_excerpts(videos: list[tuple[VideoMetadata, TranscriptResult]]) -> str:
    lines = ["## Notable Quotes\n"]
    for meta, tr in videos:
        if tr.status != TranscriptStatus.SUCCESS or not tr.segments:
            continue
        # Pick first 3 substantial segments as notable quotes
        excerpts = [
            s for s in tr.segments
            if len(s.text.strip()) > 20
        ][:3]
        if excerpts:
            lines.append(f"### {meta.title}\n")
            for seg in excerpts:
                ts = format_timestamp(seg.start)
                lines.append(f"> [{ts}] {seg.text.strip()}\n")
    if len(lines) == 1:
        lines.append("(자막이 있는 영상에서 주요 발언을 추출하세요)\n")
    return "\n".join(lines) + "\n"


def generate_brief(
    videos: list[tuple[VideoMetadata, TranscriptResult]],
    theme: str = "YouTube Research",
) -> str:
    """Generate a NotebookLM-ready research brief from multiple videos."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    tags = _collect_all_tags(videos)
    tags_str = "\n".join(f"- {t}" for t in tags) if tags else "- (태그 없음)"

    parts = [
        f"# YouTube Research Brief\n",
        f"> Generated: {now} | Videos: {len(videos)}\n",
        f"## Research Theme\n\n{theme}\n",
        _build_source_list(videos),
        _build_cross_source_summary(videos),
        "## Repeated Claims\n\n> (NotebookLM에서 교차 분석하여 반복 등장하는 주장을 정리하세요)\n",
        "## Agreements\n\n> (영상 간 일치하는 관점을 정리하세요)\n",
        "## Disagreements\n\n> (영상 간 상충하는 주장을 정리하세요)\n",
        _build_transcript_excerpts(videos),
        "## Actionable Insights\n\n> (실무 적용 가능한 인사이트를 정리하세요)\n",
        """## Recommended Follow-up Questions for NotebookLM

- 이 영상들에서 반복적으로 등장하는 핵심 패턴은?
- 서로 상충하는 주장들은 무엇인가?
- 실무 적용 시 가장 우선순위가 높은 액션은 무엇인가?
- 내 워크플로우에 붙일 수 있는 자동화 포인트는 무엇인가?
- 각 영상의 신뢰도를 어떻게 평가할 수 있는가?
""",
        f"## Suggested Tags\n\n{tags_str}\n",
    ]

    return "\n".join(parts)


def save_brief(
    videos: list[tuple[VideoMetadata, TranscriptResult]],
    theme: str = "YouTube Research",
) -> str:
    """Generate and save the research brief. Returns file path."""
    content = generate_brief(videos, theme)
    out_path = NOTEBOOKLM_DIR / "research_brief.md"
    out_path.write_text(content, encoding="utf-8")
    logger.info("Saved research brief: %s", out_path)
    return str(out_path)
