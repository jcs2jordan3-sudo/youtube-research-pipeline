"""Notion API payload generator and uploader for YouTube research data."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

import requests
from dotenv import load_dotenv

from app.models import VideoMetadata, TranscriptResult, TranscriptStatus
from app.utils import NOTION_DIR, format_timestamp, setup_logging

load_dotenv()
logger = setup_logging()

NOTION_API_VERSION = "2022-06-28"
_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.0  # seconds; doubled on each 429


def _format_date_iso(raw: str) -> str:
    """Convert YYYYMMDD to ISO date string."""
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _build_rich_text(content: str) -> list[dict]:
    """Build Notion rich_text block."""
    # Notion API limits rich_text to 2000 chars per block
    chunks = []
    while content:
        chunk = content[:2000]
        content = content[2000:]
        chunks.append({
            "type": "text",
            "text": {"content": chunk},
        })
    return chunks


def _build_paragraph_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": _build_rich_text(text),
        },
    }


def _build_heading_block(text: str, level: int = 2) -> dict:
    key = f"heading_{level}"
    return {
        "object": "block",
        "type": key,
        key: {
            "rich_text": _build_rich_text(text),
        },
    }


def _build_transcript_excerpt(transcript: TranscriptResult, max_segments: int = 20) -> str:
    if transcript.status != TranscriptStatus.SUCCESS or not transcript.segments:
        return "(자막 없음)"
    lines = []
    for seg in transcript.segments[:max_segments]:
        ts = format_timestamp(seg.start)
        lines.append(f"[{ts}] {seg.text.strip()}")
    if len(transcript.segments) > max_segments:
        lines.append(f"... (외 {len(transcript.segments) - max_segments}개 세그먼트)")
    return "\n".join(lines)


def _build_toggle_block(title: str, children: list[dict]) -> dict:
    """Build a Notion toggle block (collapsible)."""
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": _build_rich_text(title),
            "children": children,
        },
    }


def _build_full_transcript_toggles(transcript: TranscriptResult) -> list[dict]:
    """Build toggle blocks containing the full transcript, chunked to fit Notion limits."""
    if transcript.status != TranscriptStatus.SUCCESS or not transcript.segments:
        return [_build_paragraph_block("(자막/음성 데이터 없음)")]

    source_label = "Whisper STT" if transcript.is_generated else "YouTube 자막"
    total = len(transcript.segments)

    # Build full transcript text in chunks (each paragraph max ~1900 chars)
    all_lines: list[str] = []
    for seg in transcript.segments:
        ts = format_timestamp(seg.start)
        text = seg.text.strip()
        if text:
            all_lines.append(f"[{ts}] {text}")

    # Split into paragraph blocks of ~1900 chars each
    child_blocks: list[dict] = []
    chunk: list[str] = []
    chunk_len = 0
    for line in all_lines:
        if chunk_len + len(line) + 1 > 1900 and chunk:
            child_blocks.append(_build_paragraph_block("\n".join(chunk)))
            chunk = []
            chunk_len = 0
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        child_blocks.append(_build_paragraph_block("\n".join(chunk)))

    # Notion toggle children limit: max 100 blocks
    # If more, nest into multiple toggles
    if len(child_blocks) <= 100:
        return [_build_toggle_block(
            f"📜 전체 자막 보기 ({source_label} | {total}개 세그먼트)",
            child_blocks[:100],
        )]
    else:
        toggles = []
        for i in range(0, len(child_blocks), 100):
            batch = child_blocks[i:i + 100]
            part_num = i // 100 + 1
            toggles.append(_build_toggle_block(
                f"📜 전체 자막 Part {part_num} ({source_label})",
                batch,
            ))
        return toggles


def _get_llm_sections(transcript: TranscriptResult, meta: VideoMetadata) -> dict[str, str]:
    """Get LLM sections, with caching to avoid duplicate API calls."""
    vid = meta.id
    if vid in _llm_cache:
        return _llm_cache[vid]
    result = _llm_summarize(transcript, meta)
    if result:
        _llm_cache[vid] = result
        return result
    return {}


def _build_key_insights(transcript: TranscriptResult, meta: VideoMetadata) -> str:
    """Extract key insights — LLM if available, extractive fallback."""
    if transcript.status != TranscriptStatus.SUCCESS or not transcript.segments:
        return "(자막/음성 데이터 없음 — 인사이트 추출 불가)"

    llm = _get_llm_sections(transcript, meta)
    if "핵심 인사이트" in llm:
        return llm["핵심 인사이트"]

    # Fallback: extractive
    segments = transcript.segments
    step = max(1, len(segments) // 10)
    sampled = [segments[i] for i in range(0, len(segments), step)]
    key_sentences = [f"• {s.text.strip()}" for s in sampled if len(s.text.strip()) >= 15][:8]
    if not key_sentences:
        return "(핵심 인사이트 추출 실패)"
    source_label = "Whisper STT" if transcript.is_generated else "YouTube 자막"
    return f"[{source_label} 기반 추출 — LLM 요약 미사용]\n\n" + "\n".join(key_sentences)


def _build_practical_applications(transcript: TranscriptResult, meta: VideoMetadata) -> str:
    """Extract practical applications — LLM if available, keyword fallback."""
    if transcript.status != TranscriptStatus.SUCCESS or not transcript.segments:
        return "(자막/음성 데이터 없음 — 실무 적용 포인트 추출 불가)"

    llm = _get_llm_sections(transcript, meta)
    if "실무 적용 포인트" in llm:
        return llm["실무 적용 포인트"]

    # Fallback: keyword-based
    action_keywords = ["방법", "하세요", "해보세요", "추천", "설정", "활용", "적용", "설치", "팁", "자동화", "실전"]
    actionable = []
    for seg in transcript.segments:
        text = seg.text.strip()
        if len(text) >= 15 and any(kw in text for kw in action_keywords) and len(actionable) < 8:
            ts = format_timestamp(seg.start)
            actionable.append(f"• [{ts}] {text}")
    if not actionable:
        return "(실무 적용 포인트를 자동 추출하지 못했습니다)"
    source_label = "Whisper STT" if transcript.is_generated else "YouTube 자막"
    return f"[{source_label} 기반 추출 — LLM 요약 미사용]\n\n" + "\n".join(actionable)


def _get_full_transcript_text(transcript: TranscriptResult, max_chars: int = 12000) -> str:
    """Collect transcript text, truncated to max_chars."""
    if transcript.status != TranscriptStatus.SUCCESS or not transcript.segments:
        return ""
    lines = []
    total = 0
    for s in transcript.segments:
        text = s.text.strip()
        if not text:
            continue
        if total + len(text) > max_chars:
            break
        lines.append(text)
        total += len(text)
    return "\n".join(lines)


def _llm_summarize(transcript: TranscriptResult, meta: VideoMetadata) -> dict[str, str] | None:
    """Use OpenAI API to generate structured summary. Returns dict or None."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError as e:
        logger.warning("openai import failed (%s): %s", type(e).__name__, e)
        return None

    transcript_text = _get_full_transcript_text(transcript, max_chars=12000)
    if not transcript_text:
        return None

    prompt = f"""다음 YouTube 영상의 자막/음성 텍스트를 분석하여 한국어로 구조화된 요약을 작성하세요.

영상 제목: {meta.title}
채널: {meta.channel}

자막 텍스트:
{transcript_text}

다음 형식으로 정확히 작성하세요. 각 섹션을 ###로 구분하세요.

### 주제별 요약
영상에서 다루는 주제별로 나눠서 정리하세요. 각 주제를 "▶ 주제명:" 형식으로 시작하세요.
3~6개의 주제로 나눠주세요.

### 핵심 인사이트
이 영상에서 가장 중요하고 가치 있는 인사이트를 5개 추출하세요.
단순한 문장 나열이 아니라, 영상이 전달하는 핵심 메시지와 배울 점을 요약하세요.
"•" 로 시작하세요.

### 실무 적용 포인트
이 영상의 내용을 실무에 어떻게 적용할 수 있는지 구체적으로 작성하세요.
실제로 따라할 수 있는 액션 아이템 위주로 3~5개 작성하세요.
"•" 로 시작하세요.

중요: 영상 내용을 실제로 이해하고 분석한 요약을 작성하세요. 자막 문장을 그대로 복사하지 마세요."""

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content or ""

        # Parse sections
        sections: dict[str, str] = {}
        current_key = ""
        current_lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("### "):
                if current_key:
                    sections[current_key] = "\n".join(current_lines).strip()
                current_key = line[4:].strip()
                current_lines = []
            else:
                current_lines.append(line)
        if current_key:
            sections[current_key] = "\n".join(current_lines).strip()

        logger.info("LLM summary generated for %s", meta.id)
        return sections

    except Exception as e:
        logger.error("LLM summarization failed for %s: %s", meta.id, e)
        return None


def _build_content_summary(transcript: TranscriptResult, meta: VideoMetadata) -> str:
    """Build a content summary — LLM if available, extractive fallback."""
    if transcript.status != TranscriptStatus.SUCCESS or not transcript.segments:
        if meta.description:
            return meta.description[:1500]
        return "(자막/음성 데이터 없음 — 내용 요약 불가)"

    # Try LLM summary first (uses cache to avoid duplicate API calls per video)
    llm = _get_llm_sections(transcript, meta)
    if "주제별 요약" in llm:
        return llm["주제별 요약"]

    # Fallback: extractive
    segment_count = len(transcript.segments)
    duration_min = (meta.duration or 0) // 60
    source_label = "Whisper STT" if transcript.is_generated else "YouTube 자막"

    third = max(1, segment_count // 3)
    def extract(segs: list, max_c: int = 400) -> str:
        out, t = [], 0
        for s in segs:
            txt = s.text.strip()
            if len(txt) < 10: continue
            if t + len(txt) > max_c: break
            out.append(txt)
            t += len(txt)
        return " ".join(out)

    parts = [
        f"[{source_label} 기반 | {duration_min}분 | {segment_count}개 세그먼트]\n",
        f"▶ 도입부: {extract(transcript.segments[:third], 500)}\n",
        f"▶ 본문: {extract(transcript.segments[third:third*2], 500)}\n",
        f"▶ 마무리: {extract(transcript.segments[third*2:], 400)}",
    ]
    return "\n".join(p for p in parts if p).strip()


# Cache for LLM results to avoid duplicate calls per video
_llm_cache: dict[str, dict[str, str]] = {}


def build_notion_entry(
    meta: VideoMetadata,
    transcript: TranscriptResult,
) -> dict:
    """Build a single Notion page payload for one video."""
    # DB properties
    properties = {
        "Title": {
            "title": _build_rich_text(meta.title),
        },
        "소스 URL": {
            "url": meta.webpage_url,
        },
        "채널": {
            "rich_text": _build_rich_text(meta.channel),
        },
        "게시일": {
            "date": {"start": _format_date_iso(meta.upload_date)} if meta.upload_date else None,
        },
        "태그": {
            "multi_select": [{"name": tag} for tag in (meta.tags or [])[:10]],
        },
        "상태": {
            "select": {"name": "수집 완료"},
        },
        "NotebookLM 준비": {
            "checkbox": transcript.status == TranscriptStatus.SUCCESS,
        },
        "신뢰도": {
            "select": {
                "name": "높음" if transcript.status == TranscriptStatus.SUCCESS else "낮음",
            },
        },
        "수집 일시": {
            "rich_text": _build_rich_text(meta.collected_at),
        },
    }

    # Page body blocks
    content_summary = _build_content_summary(transcript, meta)
    key_insights = _build_key_insights(transcript, meta)
    practical_apps = _build_practical_applications(transcript, meta)
    transcript_excerpt = _build_transcript_excerpt(transcript)
    transcript_toggles = _build_full_transcript_toggles(transcript)

    children = [
        _build_heading_block("영상 내용 요약"),
        _build_paragraph_block(content_summary),
        _build_heading_block("핵심 인사이트"),
        _build_paragraph_block(key_insights),
        _build_heading_block("실무 적용 포인트"),
        _build_paragraph_block(practical_apps),
        _build_heading_block("타임스탬프 하이라이트"),
        _build_paragraph_block(transcript_excerpt),
        _build_heading_block("리스크 / 검증 필요"),
        _build_paragraph_block("(검증이 필요한 사항 작성)"),
        _build_heading_block("전체 자막"),
        *transcript_toggles,
    ]

    return {
        "properties": properties,
        "children": children,
    }


def generate_notion_payload(
    videos: list[tuple[VideoMetadata, TranscriptResult]],
) -> list[dict]:
    """Generate Notion payload for all videos."""
    entries = []
    for meta, transcript in videos:
        entries.append(build_notion_entry(meta, transcript))
    return entries


def save_notion_payload(
    videos: list[tuple[VideoMetadata, TranscriptResult]],
) -> str:
    """Generate and save Notion payload JSON. Returns file path."""
    payload = generate_notion_payload(videos)
    wrapper = {
        "generated_at": datetime.now().isoformat(),
        "total_entries": len(payload),
        "notion_api_version": "2022-06-28",
        "note": "Use this payload with Notion Create Page API. Set 'parent.database_id' before calling.",
        "entries": payload,
    }
    out_path = NOTION_DIR / "notion_payload.json"
    out_path.write_text(
        json.dumps(wrapper, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Saved Notion payload: %s (%d entries)", out_path, len(payload))
    return str(out_path)


# ---------------------------------------------------------------------------
# Real Notion API upload
# ---------------------------------------------------------------------------

def _notion_request(
    method: str,
    url: str,
    api_key: str,
    json_body: dict | None = None,
) -> requests.Response:
    """Send a request to Notion API with retry on 429 rate-limit."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION,
    }
    backoff = _RETRY_BACKOFF
    for attempt in range(1, _MAX_RETRIES + 1):
        resp = requests.request(method, url, headers=headers, json=json_body, timeout=30)
        if resp.status_code != 429:
            return resp
        retry_after = float(resp.headers.get("Retry-After", backoff))
        logger.warning(
            "Notion API rate-limited (429). Retry %d/%d after %.1fs",
            attempt, _MAX_RETRIES, retry_after,
        )
        time.sleep(retry_after)
        backoff *= 2
    return resp  # return last response even if still 429


def upload_to_notion(
    videos: list[tuple[VideoMetadata, TranscriptResult]],
) -> dict[str, Any]:
    """Upload video entries to a Notion database via the API.

    Reads NOTION_API_KEY and NOTION_DATABASE_ID from environment variables.
    Returns a summary dict with counts of successful / failed uploads.
    """
    api_key = os.getenv("NOTION_API_KEY", "")
    database_id = os.getenv("NOTION_DATABASE_ID", "")

    if not api_key or not database_id:
        logger.warning(
            "Notion credentials not found (NOTION_API_KEY / NOTION_DATABASE_ID). "
            "Skipping upload."
        )
        return {
            "uploaded": 0,
            "failed": 0,
            "skipped": len(videos),
            "errors": ["Missing NOTION_API_KEY or NOTION_DATABASE_ID"],
        }

    endpoint = "https://api.notion.com/v1/pages"
    entries = generate_notion_payload(videos)

    uploaded = 0
    failed = 0
    errors: list[str] = []

    for i, (entry, (meta, _transcript)) in enumerate(zip(entries, videos), 1):
        body = {
            "parent": {"database_id": database_id},
            **entry,
        }
        try:
            resp = _notion_request("POST", endpoint, api_key, json_body=body)
            if resp.status_code == 200:
                page_id = resp.json().get("id", "unknown")
                logger.info(
                    "[%d/%d] Created Notion page for '%s' (page_id=%s)",
                    i, len(entries), meta.title[:60], page_id,
                )
                uploaded += 1
            else:
                err_msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
                logger.error(
                    "[%d/%d] Failed to create page for '%s': %s",
                    i, len(entries), meta.title[:60], err_msg,
                )
                errors.append(f"{meta.id}: {err_msg}")
                failed += 1
        except requests.RequestException as exc:
            err_msg = str(exc)
            logger.error(
                "[%d/%d] Request error for '%s': %s",
                i, len(entries), meta.title[:60], err_msg,
            )
            errors.append(f"{meta.id}: {err_msg}")
            failed += 1

    summary = {
        "uploaded": uploaded,
        "failed": failed,
        "skipped": 0,
        "errors": errors,
    }
    logger.info(
        "Notion upload complete: %d uploaded, %d failed",
        uploaded, failed,
    )
    return summary


def maybe_upload_to_notion(
    videos: list[tuple[VideoMetadata, TranscriptResult]],
    upload: bool,
) -> dict[str, Any] | None:
    """Conditionally upload to Notion when the --notion-upload flag is set."""
    if not upload:
        logger.debug("Notion upload not requested, skipping.")
        return None
    if not videos:
        logger.info("No videos to upload to Notion.")
        return {"uploaded": 0, "failed": 0, "skipped": 0, "errors": []}
    return upload_to_notion(videos)


# ---------------------------------------------------------------------------
# Nested page structure: Daily parent page → child pages per video
# ---------------------------------------------------------------------------

def _create_page(
    api_key: str,
    parent: dict,
    title: str,
    children: list[dict] | None = None,
    properties: dict | None = None,
    icon: str | None = None,
) -> dict | None:
    """Create a Notion page and return the response JSON, or None on failure."""
    endpoint = "https://api.notion.com/v1/pages"
    body: dict[str, Any] = {"parent": parent}

    if properties:
        body["properties"] = properties
    else:
        body["properties"] = {
            "title": {"title": _build_rich_text(title)},
        }

    if children:
        # Notion API limits children to 100 blocks per request
        body["children"] = children[:100]

    if icon:
        body["icon"] = {"type": "emoji", "emoji": icon}

    try:
        resp = _notion_request("POST", endpoint, api_key, json_body=body)
        if resp.status_code == 200:
            return resp.json()
        else:
            logger.error("Failed to create page '%s': HTTP %d — %s", title[:40], resp.status_code, resp.text[:300])
            return None
    except Exception as e:
        logger.error("Request error creating page '%s': %s", title[:40], e)
        return None


def _append_children(api_key: str, page_id: str, children: list[dict]) -> bool:
    """Append additional blocks to an existing page (for >100 blocks)."""
    endpoint = f"https://api.notion.com/v1/blocks/{page_id}/children"
    body = {"children": children[:100]}
    try:
        resp = _notion_request("PATCH", endpoint, api_key, json_body=body)
        return resp.status_code == 200
    except Exception as e:
        logger.error("Failed to append blocks to %s: %s", page_id, e)
        return False


def upload_nested_to_notion(
    videos: list[tuple[VideoMetadata, TranscriptResult]],
    search_query: str = "",
) -> dict[str, Any]:
    """Upload videos as nested pages: daily parent page → video child pages.

    Structure:
        [Parent Page] "YYYY-MM-DD Claude Code Research (N videos)"
            [Child Page 1] "Video Title 1"
            [Child Page 2] "Video Title 2"
            ...

    The parent page is created under NOTION_PARENT_PAGE_ID (a page)
    or NOTION_DATABASE_ID (a database).
    """
    api_key = os.getenv("NOTION_API_KEY", "")
    parent_page_id = os.getenv("NOTION_PARENT_PAGE_ID", "")
    database_id = os.getenv("NOTION_DATABASE_ID", "")

    if not api_key:
        logger.warning("NOTION_API_KEY not set. Skipping nested upload.")
        return {"uploaded": 0, "failed": 0, "skipped": len(videos), "errors": ["Missing NOTION_API_KEY"]}

    if not parent_page_id and not database_id:
        logger.warning("Neither NOTION_PARENT_PAGE_ID nor NOTION_DATABASE_ID set.")
        return {"uploaded": 0, "failed": 0, "skipped": len(videos), "errors": ["Missing parent ID"]}

    today = datetime.now().strftime("%Y-%m-%d")
    query_label = search_query or "YouTube Research"
    parent_title = f"{today} {query_label} ({len(videos)} videos)"

    # Determine parent type
    if parent_page_id:
        parent_ref = {"type": "page_id", "page_id": parent_page_id}
    else:
        parent_ref = {"type": "database_id", "database_id": database_id}

    # Build parent page summary blocks
    summary_lines = []
    for i, (meta, tr) in enumerate(videos, 1):
        tr_status = "O" if tr.status == TranscriptStatus.SUCCESS else "X"
        views = f"{meta.view_count:,}" if meta.view_count else "N/A"
        summary_lines.append(f"{i}. {meta.title} [{meta.channel}] (조회수: {views}, 자막: {tr_status})")

    parent_children = [
        _build_heading_block("리서치 요약", level=2),
        _build_paragraph_block(f"검색어: {query_label}\n수집일: {today}\n총 영상 수: {len(videos)}"),
        _build_heading_block("영상 목록", level=2),
    ]
    # Split summary lines into chunks that fit within 2000 char limit
    chunk: list[str] = []
    chunk_len = 0
    for line in summary_lines:
        if chunk_len + len(line) + 1 > 1900 and chunk:
            parent_children.append(_build_paragraph_block("\n".join(chunk)))
            chunk = []
            chunk_len = 0
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        parent_children.append(_build_paragraph_block("\n".join(chunk)))

    # Create daily parent page
    logger.info("Creating daily parent page: %s", parent_title)
    parent_result = _create_page(
        api_key=api_key,
        parent=parent_ref,
        title=parent_title,
        children=parent_children,
        icon="📋",
    )

    if not parent_result:
        return {"uploaded": 0, "failed": 0, "skipped": len(videos), "errors": ["Failed to create parent page"]}

    daily_page_id = parent_result["id"]
    logger.info("Created parent page: %s (id=%s)", parent_title, daily_page_id)

    # Create child pages under the daily parent
    uploaded = 0
    failed = 0
    errors: list[str] = []

    for i, (meta, transcript) in enumerate(videos, 1):
        entry = build_notion_entry(meta, transcript)
        child_children = entry["children"]

        # Child page under the daily parent page
        child_result = _create_page(
            api_key=api_key,
            parent={"type": "page_id", "page_id": daily_page_id},
            title=meta.title,
            children=child_children[:100],
            icon="🎬",
        )

        if child_result:
            child_id = child_result["id"]
            logger.info("[%d/%d] Created child page: %s (id=%s)", i, len(videos), meta.title[:50], child_id)
            uploaded += 1

            # Append remaining blocks if >100
            if len(child_children) > 100:
                _append_children(api_key, child_id, child_children[100:])
        else:
            failed += 1
            errors.append(f"{meta.id}: Failed to create child page")

    summary = {
        "uploaded": uploaded,
        "failed": failed,
        "skipped": 0,
        "parent_page_id": daily_page_id,
        "errors": errors,
    }
    logger.info("Nested upload complete: parent=%s, children=%d/%d", daily_page_id, uploaded, len(videos))
    return summary
