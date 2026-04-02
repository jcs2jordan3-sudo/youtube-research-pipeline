"""Role-based Daily YouTube Research — per-role keywords, Korean filter, Notion upload."""

from __future__ import annotations

import sys
import io
import json
import os
import re
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from dotenv import load_dotenv
load_dotenv(override=True)

import yt_dlp

from app.models import VideoMetadata, TranscriptResult, TranscriptSegment, TranscriptStatus
from app.transcript import extract_transcript
from app.notion_export import (
    _build_rich_text, _build_paragraph_block, _build_heading_block,
    _build_transcript_excerpt, _build_content_summary,
    _build_key_insights, _build_practical_applications,
    _notion_request, _get_full_transcript_text, format_timestamp,
)
from app.utils import setup_logging, RAW_DIR

logger = setup_logging()

# ---------------------------------------------------------------------------
# Role definitions
# ---------------------------------------------------------------------------

ROLES: dict[str, dict[str, Any]] = {
    "아트": {
        "emoji": "🎨",
        "keywords": [
            "아트 AI 활용", "디지털 아트 AI 활용", "디자인 워크플로우 AI",
            "비주얼 작업 AI", "creative AI workflow",
        ],
    },
    "프로그래머": {
        "emoji": "💻",
        "keywords": [
            "Claude Code 활용", "클로드 코드 활용", "AI 코딩 트렌드",
            "에이전트 코딩", "AI 개발 워크플로우",
        ],
    },
    "애니메이터": {
        "emoji": "🎬",
        "keywords": [
            "애니메이터 AI 활용", "애니메이션 AI 활용", "모션 작업 AI",
            "animation AI workflow", "motion workflow AI",
        ],
    },
    "기획자": {
        "emoji": "📋",
        "keywords": [
            "기획자 AI 활용", "기획 업무 AI 활용", "문서화 AI 활용",
            "아이데이션 AI", "planning workflow AI",
        ],
    },
    "PM": {
        "emoji": "📊",
        "keywords": [
            "PM AI 활용", "업무 관리 AI 활용", "회의록 AI",
            "태스크 관리 AI", "project workflow AI",
        ],
    },
    "QA": {
        "emoji": "🔍",
        "keywords": [
            "QA AI 활용", "테스트 자동화 AI", "버그 분석 AI",
            "품질관리 AI", "QA workflow AI",
        ],
    },
    "인사팀": {
        "emoji": "👥",
        "keywords": [
            "인사팀 AI 활용", "채용 AI 활용", "면접 AI 활용",
            "온보딩 AI", "HR workflow AI",
        ],
    },
    "경영지원": {
        "emoji": "🏢",
        "keywords": [
            "경영지원 AI 활용", "문서 자동화 AI", "업무 지원 AI",
            "운영 자동화 AI", "back office workflow AI",
        ],
    },
}


# ---------------------------------------------------------------------------
# Korean detection
# ---------------------------------------------------------------------------

def _has_korean(text: str) -> bool:
    return bool(re.search(r"[\uac00-\ud7a3]", text or ""))


def _korean_ratio(text: str) -> float:
    if not text:
        return 0.0
    korean_chars = len(re.findall(r"[\uac00-\ud7a3]", text))
    total = len(text.replace(" ", ""))
    return korean_chars / total if total > 0 else 0.0


def is_korean(info: dict[str, Any]) -> bool:
    """Check if video is Korean content."""
    title = info.get("title", "")
    desc = info.get("description", "")[:500]
    channel = info.get("channel", "")
    tags = " ".join(info.get("tags") or [])
    combined = f"{title} {desc} {channel} {tags}"
    kr_ratio = _korean_ratio(combined)
    return kr_ratio > 0.15 or _has_korean(title) or _has_korean(channel)


# ---------------------------------------------------------------------------
# Search + filter per role
# ---------------------------------------------------------------------------

def search_role_videos(
    role_name: str,
    keywords: list[str],
    days: int = 7,
    max_per_query: int = 15,
    final_count: int = 5,
) -> list[dict[str, Any]]:
    """Search YouTube for a role's keywords as a cluster, filter date+Korean, select top N.

    Strategy:
    - Keywords are semantic search hints, not strict filters
    - YouTube search results are trusted as relevant
    - Only filter by: upload date + Korean content
    - If < final_count after 7 days, expand to 30 days
    """
    today_str = datetime.now().strftime("%Y%m%d")
    cutoff_str = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    cutoff_expanded = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    # Phase 1: Broad collection — ALL keywords as one cluster
    seen_ids: set[str] = set()
    id_to_keywords: dict[str, list[str]] = {}
    all_urls: list[str] = []

    flat_opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": True, "playlistend": max_per_query,
    }

    for kw in keywords:
        try:
            with yt_dlp.YoutubeDL(flat_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{max_per_query}:{kw}", download=False)
                if info and "entries" in info:
                    for e in info["entries"]:
                        if e and e.get("id"):
                            vid = e["id"]
                            id_to_keywords.setdefault(vid, [])
                            if kw not in id_to_keywords[vid]:
                                id_to_keywords[vid].append(kw)
                            if vid not in seen_ids:
                                seen_ids.add(vid)
                                all_urls.append(f"https://www.youtube.com/watch?v={vid}")
        except Exception:
            pass

    logger.info("[%s] Found %d unique candidates from %d keywords", role_name, len(all_urls), len(keywords))

    # Phase 2: Get metadata — filter only by date and Korean
    detail_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True}
    pool: list[dict[str, Any]] = []
    pool_expanded: list[dict[str, Any]] = []  # 30-day fallback

    processed_ids: set[str] = set()
    with yt_dlp.YoutubeDL(detail_opts) as ydl:
        for url in all_urls:
            vid_id = url.split("v=")[-1]
            if vid_id in processed_ids:
                continue
            processed_ids.add(vid_id)

            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    continue

                upload_date = info.get("upload_date", "")
                if not upload_date:
                    continue

                # Korean check (relaxed — title OR channel having Korean is enough)
                if not is_korean(info):
                    continue

                view_count = info.get("view_count", 0) or 0
                like_count = info.get("like_count", 0) or 0
                matched = id_to_keywords.get(info.get("id", vid_id), [])

                # Priority score: views + likes + recency
                view_score = min(30, int(math.log10(max(view_count, 1)) * 6))
                like_score = min(15, int(math.log10(max(like_count, 1)) * 4))
                try:
                    ud = datetime.strptime(upload_date, "%Y%m%d")
                    td = datetime.now()
                    days_ago = (td - ud).days
                    recency_score = max(0, 20 - days_ago * 2)
                except ValueError:
                    recency_score = 0
                priority = view_score + like_score + recency_score

                entry = {
                    "id": info.get("id", ""),
                    "title": info.get("title", ""),
                    "channel": info.get("channel", ""),
                    "upload_date": upload_date,
                    "view_count": view_count,
                    "like_count": like_count,
                    "duration": info.get("duration", 0) or 0,
                    "description": info.get("description", ""),
                    "tags": info.get("tags") or [],
                    "webpage_url": info.get("webpage_url", url),
                    "matched_keywords": matched,
                    "priority_score": priority,
                    "chapters": info.get("chapters") or [],
                }

                # Date bucket
                if upload_date >= cutoff_str:
                    pool.append(entry)
                elif upload_date >= cutoff_expanded:
                    pool_expanded.append(entry)

            except Exception:
                continue

    # Phase 3: Select top N — prefer 7-day pool, expand to 30 if needed
    pool.sort(key=lambda x: x["priority_score"], reverse=True)
    selected = pool[:final_count]

    if len(selected) < final_count:
        pool_expanded.sort(key=lambda x: x["priority_score"], reverse=True)
        existing_ids = {s["id"] for s in selected}
        for entry in pool_expanded:
            if entry["id"] not in existing_ids and len(selected) < final_count:
                selected.append(entry)
                existing_ids.add(entry["id"])

    logger.info("[%s] Selected %d (7d=%d, 30d=%d)", role_name, len(selected), len(pool), len(pool_expanded))
    return selected


# ---------------------------------------------------------------------------
# LLM summaries
# ---------------------------------------------------------------------------

def _llm_one_line(transcript: TranscriptResult, meta: VideoMetadata) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return meta.title
    try:
        from openai import OpenAI
        text = _get_full_transcript_text(transcript, max_chars=5000)
        if not text:
            return meta.title
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=150,
            messages=[{"role": "user", "content": f"다음 YouTube 영상 자막을 한 문장으로 핵심만 요약해줘.\n제목: {meta.title}\n\n자막:\n{text}"}],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return meta.title


def _llm_detailed(transcript: TranscriptResult, meta: VideoMetadata) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return _build_content_summary(transcript, meta)
    try:
        from openai import OpenAI
        text = _get_full_transcript_text(transcript, max_chars=12000)
        if not text:
            return _build_content_summary(transcript, meta)
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=1500,
            messages=[{"role": "user", "content": f"""다음 YouTube 영상의 자막을 주제별로 상세하게 정리해줘.

제목: {meta.title}
채널: {meta.channel}

자막:
{text}

요구사항:
- 주제를 3~6개로 나눠서 정리
- 각 주제별로 구체적인 내용을 3~5문장으로 설명
- "▶ 주제명:" 형식으로 시작
- 자막 원문을 복사하지 말고 내용을 재구성
- 구체적인 도구명, 방법론, 팁 등 반드시 포함"""}],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return _build_content_summary(transcript, meta)


# ---------------------------------------------------------------------------
# Notion: role folder + daily page + DB + upload rows
# ---------------------------------------------------------------------------

def _format_date_iso(raw: str) -> str:
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _find_or_create_role_folder(
    api_key: str,
    parent_page_id: str,
    role_name: str,
    emoji: str,
) -> str | None:
    """Find existing role folder page under parent, or create one.

    Searches children of parent_page_id for a page titled "{emoji} {role_name}".
    Returns the role folder page ID, or None on failure.
    """
    folder_title = f"{emoji} {role_name}"

    # Search existing children for the role folder
    cursor: str | None = ""
    while cursor is not None:
        url = f"https://api.notion.com/v1/blocks/{parent_page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        try:
            resp = _notion_request("GET", url, api_key)
            if resp.status_code != 200:
                logger.warning("[%s] Failed to list children: %d", role_name, resp.status_code)
                break
            data = resp.json()
            for block in data.get("results", []):
                if block.get("type") == "child_page":
                    block_title = block.get("child_page", {}).get("title", "")
                    if block_title == folder_title:
                        logger.info("[%s] Found existing role folder: %s", role_name, block["id"])
                        return block["id"]
            cursor = data.get("next_cursor")
        except Exception as e:
            logger.warning("[%s] Error searching children: %s", role_name, e)
            break

    # Not found — create new role folder page
    page_body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": emoji},
        "properties": {"title": {"title": _build_rich_text(folder_title)}},
        "children": [
            _build_paragraph_block(f"{role_name} 직군의 일일 YouTube 리서치 결과가 날짜별로 정리됩니다."),
        ],
    }

    try:
        resp = _notion_request("POST", "https://api.notion.com/v1/pages", api_key, json_body=page_body)
        if resp.status_code != 200:
            logger.error("[%s] Role folder creation failed: %d — %s", role_name, resp.status_code, resp.text[:300])
            return None
        folder_id = resp.json()["id"]
        logger.info("[%s] Created role folder: %s (id=%s)", role_name, folder_title, folder_id)
        return folder_id
    except Exception as e:
        logger.error("[%s] Role folder error: %s", role_name, e)
        return None


def create_daily_page_with_db(
    api_key: str,
    role_folder_id: str,
    role_name: str,
    emoji: str,
    keywords: list[str],
    video_count: int,
) -> tuple[str | None, str | None]:
    """Create a daily research page with DB inside the role folder.

    Structure:
        {emoji} {role_name} (role folder — persistent)
            └── Daily Research_MMDD (daily page — created each run)
                    └── 📊 영상 목록 (DB with video rows)
    """
    today = datetime.now().strftime("%m%d")
    today_full = datetime.now().strftime("%Y-%m-%d")
    page_title = f"Daily Research_{today}"

    kw_text = ", ".join(keywords)
    criteria = (
        f"직군: {role_name} | 수집일: {today_full} | 최근 7일 | 한국어 | 상위 {video_count}개\n\n"
        f"[검색 키워드] {kw_text}\n\n"
        f"선정 기준: 키워드 관련성 + 조회수 + 좋아요 수"
    )

    page_body = {
        "parent": {"type": "page_id", "page_id": role_folder_id},
        "icon": {"type": "emoji", "emoji": "📅"},
        "properties": {"title": {"title": _build_rich_text(page_title)}},
        "children": [
            _build_heading_block("검색 기준", level=3),
            _build_paragraph_block(criteria),
        ],
    }

    try:
        resp = _notion_request("POST", "https://api.notion.com/v1/pages", api_key, json_body=page_body)
        if resp.status_code != 200:
            logger.error("[%s] Daily page creation failed: %d", role_name, resp.status_code)
            return None, None
        page_id = resp.json()["id"]
    except Exception as e:
        logger.error("[%s] Daily page error: %s", role_name, e)
        return None, None

    # Create DB inside the daily page
    db_body = {
        "parent": {"type": "page_id", "page_id": page_id},
        "icon": {"type": "emoji", "emoji": "📊"},
        "title": [{"type": "text", "text": {"content": "영상 목록"}}],
        "properties": {
            "제목": {"title": {}},
            "채널": {"rich_text": {}},
            "업로드일": {"date": {}},
            "조회수": {"number": {"format": "number_with_commas"}},
            "좋아요": {"number": {"format": "number_with_commas"}},
            "원본": {"url": {}},
            "매칭키워드": {"rich_text": {}},
            "우선순위": {"number": {"format": "number"}},
        },
    }

    try:
        resp = _notion_request("POST", "https://api.notion.com/v1/databases", api_key, json_body=db_body)
        if resp.status_code != 200:
            logger.error("[%s] DB creation failed: %d — %s", role_name, resp.status_code, resp.text[:200])
            return page_id, None
        db_id = resp.json()["id"]
        logger.info("[%s] Created daily page+DB: %s", role_name, page_title)
        return page_id, db_id
    except Exception as e:
        logger.error("[%s] DB error: %s", role_name, e)
        return page_id, None


def upload_video_row(
    api_key: str,
    db_id: str,
    video: dict[str, Any],
    transcript: TranscriptResult,
    meta: VideoMetadata,
) -> bool:
    """Upload one video row to the role's Notion DB."""
    one_line = _llm_one_line(transcript, meta)
    key_insights = _build_key_insights(transcript, meta)
    practical_apps = _build_practical_applications(transcript, meta)
    detailed = _llm_detailed(transcript, meta)
    transcript_excerpt = _build_transcript_excerpt(transcript)
    desc = video.get("description", "")[:1500] or "(설명란 없음)"

    children = [
        _build_heading_block("한줄 요약"),
        _build_paragraph_block(one_line),
        _build_heading_block("핵심 인사이트"),
        _build_paragraph_block(key_insights),
        _build_heading_block("실무 적용 포인트"),
        _build_paragraph_block(practical_apps),
        _build_heading_block("상세 내용 정리"),
        _build_paragraph_block(detailed),
        _build_heading_block("타임스탬프 요약"),
        _build_paragraph_block(transcript_excerpt),
        _build_heading_block("설명란 요약"),
        _build_paragraph_block(desc),
    ]

    body = {
        "parent": {"database_id": db_id},
        "icon": {"type": "emoji", "emoji": "🎬"},
        "properties": {
            "제목": {"title": _build_rich_text(video["title"])},
            "채널": {"rich_text": _build_rich_text(video["channel"])},
            "업로드일": {"date": {"start": _format_date_iso(video["upload_date"])}},
            "조회수": {"number": video["view_count"]},
            "좋아요": {"number": video.get("like_count", 0)},
            "원본": {"url": video["webpage_url"]},
            "매칭키워드": {"rich_text": _build_rich_text(", ".join(video["matched_keywords"][:5]))},
            "우선순위": {"number": video["priority_score"]},
        },
        "children": children[:100],
    }

    try:
        resp = _notion_request("POST", "https://api.notion.com/v1/pages", api_key, json_body=body)
        if resp.status_code == 200:
            return True
        logger.error("Upload failed: %d — %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        logger.error("Upload error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_role_research(
    parent_page_id: str = "",
    days: int = 7,
    videos_per_role: int = 5,
    whisper: bool = True,
    whisper_model: str = "base",
    roles: dict[str, dict] | None = None,
) -> None:
    """Run daily research for all roles."""
    api_key = os.getenv("NOTION_API_KEY", "")
    parent_page_id = parent_page_id or os.getenv("NOTION_PARENT_PAGE_ID", "")
    if not api_key:
        print("NOTION_API_KEY not set.")
        return

    # ffmpeg path
    ffmpeg_path = "C:/Users/USER/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1-full_build/bin"
    if ffmpeg_path not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_path + os.pathsep + os.environ.get("PATH", "")

    roles = roles or ROLES
    today = datetime.now().strftime("%Y-%m-%d")
    total_roles = len(roles)

    print(f"\n{'='*60}")
    print(f" 직군별 Daily Research — {today}")
    print(f" {total_roles}개 직군 × {videos_per_role}개 = 최대 {total_roles * videos_per_role}개")
    print(f"{'='*60}\n")

    total_uploaded = 0
    total_failed = 0
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for role_idx, (role_name, role_config) in enumerate(roles.items(), 1):
        emoji = role_config["emoji"]
        keywords = role_config["keywords"]

        print(f"\n{'─'*50}")
        print(f" [{role_idx}/{total_roles}] {emoji} {role_name}")
        print(f"{'─'*50}")

        # Step 1: Search
        print(f"  검색 중...")
        selected = search_role_videos(role_name, keywords, days=days, final_count=videos_per_role)

        if not selected:
            print(f"  기준에 맞는 영상 없음. 스킵.")
            continue

        print(f"  {len(selected)}개 선정:")
        for i, v in enumerate(selected, 1):
            print(f"    {i}. [{v['view_count']:,}] {v['title'][:50]}")

        # Step 2: Find/create role folder, then create daily page + DB
        print(f"  Notion 직군 폴더 확인...")
        role_folder_id = _find_or_create_role_folder(api_key, parent_page_id, role_name, emoji)
        if not role_folder_id:
            print(f"  직군 폴더 생성 실패. 스킵.")
            continue

        print(f"  Notion 데일리 페이지 생성...")
        page_id, db_id = create_daily_page_with_db(
            api_key, role_folder_id, role_name, emoji, keywords, len(selected)
        )
        if not db_id:
            print(f"  Notion DB 생성 실패. 스킵.")
            continue

        # Step 3: Process each video
        for i, video in enumerate(selected, 1):
            vid = video["id"]
            print(f"  [{i}/{len(selected)}] {video['title'][:45]}...", end=" ")

            # Save raw info
            info_path = RAW_DIR / f"{vid}.info.json"
            info_path.write_text(json.dumps(video, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

            # Transcript
            transcript = extract_transcript(vid, video["webpage_url"], whisper_fallback=whisper, whisper_model=whisper_model)

            meta = VideoMetadata(
                id=vid, title=video["title"], channel=video["channel"],
                upload_date=video["upload_date"], duration=video["duration"],
                description=video.get("description", "")[:500],
                tags=video.get("tags") or [], webpage_url=video["webpage_url"],
                view_count=video["view_count"], collected_at=collected_at,
            )

            # Upload
            ok = upload_video_row(api_key, db_id, video, transcript, meta)
            if ok:
                total_uploaded += 1
                print("✓")
            else:
                total_failed += 1
                print("✗")

    print(f"\n{'='*60}")
    print(f" 완료: {total_uploaded} 업로드 / {total_failed} 실패")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Migration: move existing flat pages into role folders
# ---------------------------------------------------------------------------

def _read_block_children(api_key: str, block_id: str) -> list[dict]:
    """Read all child blocks of a page/block (handles pagination)."""
    children: list[dict] = []
    cursor: str | None = ""
    while cursor is not None:
        url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        try:
            resp = _notion_request("GET", url, api_key)
            if resp.status_code != 200:
                break
            data = resp.json()
            children.extend(data.get("results", []))
            cursor = data.get("next_cursor")
        except Exception:
            break
    return children


def _clone_block(block: dict) -> dict | None:
    """Convert a retrieved block into a create-ready block payload.

    Notion API returns blocks with extra metadata; we strip it down
    to only the fields needed for creating a new block.
    """
    btype = block.get("type")
    if not btype:
        return None

    # Block types that can be cloned directly
    clonable = {
        "paragraph", "heading_1", "heading_2", "heading_3",
        "bulleted_list_item", "numbered_list_item", "to_do",
        "toggle", "quote", "callout", "divider",
        "table_of_contents", "bookmark", "embed", "image",
        "video", "file", "pdf", "code", "equation",
    }

    if btype not in clonable:
        # For unsupported types (child_page, child_database, etc.), skip
        return None

    content = block.get(btype)
    if content is None:
        return None

    # Remove read-only fields that can't be sent in create requests
    clean = {}
    for k, v in content.items():
        if k in ("children",):
            continue  # children are handled separately
        clean[k] = v

    return {
        "object": "block",
        "type": btype,
        btype: clean,
    }


def _clone_page_content(
    api_key: str,
    source_page_id: str,
    target_parent_id: str,
    title: str,
    icon_emoji: str = "📅",
) -> str | None:
    """Clone a page's content (blocks) into a new page under target_parent_id.

    Returns the new page ID, or None on failure.
    """
    # Read source page blocks
    source_blocks = _read_block_children(api_key, source_page_id)

    # Convert blocks to create-ready payloads
    new_blocks: list[dict] = []
    for block in source_blocks:
        btype = block.get("type")

        # child_database: recreate as a fresh DB with same schema
        if btype == "child_database":
            # We can't clone databases block-by-block easily,
            # so we add a placeholder noting the DB existed
            new_blocks.append(_build_paragraph_block(
                f"(원본 데이터베이스: {block.get('child_database', {}).get('title', 'DB')} — "
                f"영상 데이터는 원본 DB 참조)"
            ))
            continue

        cloned = _clone_block(block)
        if cloned:
            new_blocks.append(cloned)

    # Create new page
    page_body = {
        "parent": {"type": "page_id", "page_id": target_parent_id},
        "icon": {"type": "emoji", "emoji": icon_emoji},
        "properties": {"title": {"title": _build_rich_text(title)}},
    }
    if new_blocks:
        page_body["children"] = new_blocks[:100]

    try:
        resp = _notion_request("POST", "https://api.notion.com/v1/pages", api_key, json_body=page_body)
        if resp.status_code != 200:
            logger.error("Failed to clone page '%s': %d — %s", title, resp.status_code, resp.text[:300])
            return None
        new_page_id = resp.json()["id"]

        # Append remaining blocks if > 100
        if len(new_blocks) > 100:
            endpoint = f"https://api.notion.com/v1/blocks/{new_page_id}/children"
            for i in range(100, len(new_blocks), 100):
                batch = new_blocks[i:i + 100]
                _notion_request("PATCH", endpoint, api_key, json_body={"children": batch})

        return new_page_id
    except Exception as e:
        logger.error("Clone error for '%s': %s", title, e)
        return None


def _archive_page(api_key: str, page_id: str) -> bool:
    """Archive (soft-delete) a Notion page."""
    try:
        resp = _notion_request(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            api_key,
            json_body={"archived": True},
        )
        return resp.status_code == 200
    except Exception:
        return False


def migrate_to_role_folders(parent_page_id: str = "") -> None:
    """Migrate existing flat role pages into role folder structure.

    Finds pages like "🎨 아트 Daily Research_0402" directly under
    parent_page_id and moves them into "🎨 아트" folder pages.

    Process for each matched page:
        1. Find/create the role folder
        2. Clone page content into a new page under the role folder
        3. Archive the original page
    """
    api_key = os.getenv("NOTION_API_KEY", "")
    parent_page_id = parent_page_id or os.getenv("NOTION_PARENT_PAGE_ID", "")

    if not api_key or not parent_page_id:
        print("NOTION_API_KEY 또는 NOTION_PARENT_PAGE_ID가 설정되지 않았습니다.")
        return

    print(f"\n{'='*60}")
    print(f" 마이그레이션: 기존 페이지 → 직군별 폴더 구조")
    print(f"{'='*60}\n")

    # Build lookup: emoji+role_name → role_name, emoji
    role_lookup: list[tuple[str, str, str]] = []
    for role_name, config in ROLES.items():
        emoji = config["emoji"]
        prefix = f"{emoji} {role_name} Daily Research_"
        role_lookup.append((prefix, role_name, emoji))

    # List all children of parent page
    print("부모 페이지 하위 블록 조회 중...")
    children = _read_block_children(api_key, parent_page_id)

    # Find pages matching role patterns
    pages_to_migrate: list[tuple[dict, str, str, str]] = []  # (block, title, role_name, emoji)
    for block in children:
        if block.get("type") != "child_page":
            continue
        title = block.get("child_page", {}).get("title", "")
        for prefix, role_name, emoji in role_lookup:
            if title.startswith(prefix):
                pages_to_migrate.append((block, title, role_name, emoji))
                break

    if not pages_to_migrate:
        print("이동할 페이지가 없습니다. (이미 정리되었거나 매칭되는 페이지 없음)")
        return

    print(f"이동 대상: {len(pages_to_migrate)}개 페이지\n")
    for _, title, role_name, _ in pages_to_migrate:
        print(f"  - {title}  →  {role_name} 폴더")

    # Process each page
    print()
    migrated = 0
    failed = 0
    role_folder_cache: dict[str, str] = {}  # role_name → folder_id

    for block, title, role_name, emoji in pages_to_migrate:
        old_page_id = block["id"]
        print(f"[이동] {title}")

        # Find/create role folder (cached)
        if role_name not in role_folder_cache:
            folder_id = _find_or_create_role_folder(api_key, parent_page_id, role_name, emoji)
            if not folder_id:
                print(f"  ✗ 직군 폴더 생성 실패")
                failed += 1
                continue
            role_folder_cache[role_name] = folder_id
        folder_id = role_folder_cache[role_name]

        # New title: strip the emoji+role prefix → just "Daily Research_MMDD"
        prefix = f"{emoji} {role_name} "
        new_title = title[len(prefix):] if title.startswith(prefix) else title

        # Clone page content to role folder
        new_page_id = _clone_page_content(api_key, old_page_id, folder_id, new_title)
        if not new_page_id:
            print(f"  ✗ 복제 실패")
            failed += 1
            continue

        # Archive original
        archived = _archive_page(api_key, old_page_id)
        if archived:
            print(f"  ✓ → {emoji} {role_name}/{new_title} (원본 보관처리)")
            migrated += 1
        else:
            print(f"  △ 복제 완료, 원본 보관처리 실패 (수동 삭제 필요)")
            migrated += 1

    print(f"\n{'='*60}")
    print(f" 마이그레이션 완료: {migrated} 이동 / {failed} 실패")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Role-based YouTube Daily Research")
    parser.add_argument("--page-id", type=str, default="", help="Notion parent page ID")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--count", type=int, default=5, help="Videos per role")
    parser.add_argument("--no-whisper", action="store_true")
    parser.add_argument("--whisper-model", type=str, default="base")
    parser.add_argument("--migrate", action="store_true", help="Migrate existing flat pages into role folders")
    args = parser.parse_args()

    if args.migrate:
        migrate_to_role_folders(parent_page_id=args.page_id)
    else:
        run_role_research(
            parent_page_id=args.page_id,
            days=args.days,
            videos_per_role=args.count,
            whisper=not args.no_whisper,
            whisper_model=args.whisper_model,
        )
