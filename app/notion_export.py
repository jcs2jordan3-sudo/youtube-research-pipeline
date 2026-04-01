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
    transcript_excerpt = _build_transcript_excerpt(transcript)
    description_summary = meta.description[:1500] if meta.description else "(설명란 없음)"

    children = [
        _build_heading_block("영상 요약"),
        _build_paragraph_block(description_summary),
        _build_heading_block("핵심 인사이트"),
        _build_paragraph_block("(자막 및 메타데이터 분석 후 작성)"),
        _build_heading_block("타임스탬프 하이라이트"),
        _build_paragraph_block(transcript_excerpt),
        _build_heading_block("실무 적용 포인트"),
        _build_paragraph_block("(실무에 적용 가능한 포인트 작성)"),
        _build_heading_block("리스크 / 검증 필요"),
        _build_paragraph_block("(검증이 필요한 사항 작성)"),
        _build_heading_block("자막 발췌"),
        _build_paragraph_block(transcript_excerpt),
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
