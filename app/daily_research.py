"""Daily YouTube Research Pipeline — keyword-based search, filtering, scoring, Notion DB upload."""

from __future__ import annotations

import sys
import io
import json
import os
import re
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

import requests
import yt_dlp

from app.models import VideoMetadata, TranscriptResult, TranscriptSegment, TranscriptStatus
from app.transcript import extract_transcript
from app.notion_export import (
    _build_rich_text, _build_paragraph_block, _build_heading_block,
    _build_toggle_block, _build_full_transcript_toggles,
    _build_content_summary, _build_key_insights, _build_practical_applications,
    _build_transcript_excerpt, _notion_request, _get_full_transcript_text,
    NOTION_API_VERSION, format_timestamp,
)
from app.utils import setup_logging, RAW_DIR

logger = setup_logging()

# ---------------------------------------------------------------------------
# Keyword config
# ---------------------------------------------------------------------------

CORE_KEYWORDS = [
    "Claude Code", "클로드 코드", "Claude Code 사용법",
    "클로드 코드 활용", "Claude Code update", "클로드 코드 업데이트",
]
EXTENDED_KEYWORDS = [
    "Claude Code MCP", "MCP 활용", "클로드 코드 워크플로우", "바이브 코딩",
]
AUX_KEYWORDS = [
    "AI 업무 자동화", "NotebookLM 활용", "AI 실무 활용", "AI 리서치 자동화",
]

ALL_SEARCH_QUERIES = CORE_KEYWORDS + EXTENDED_KEYWORDS + AUX_KEYWORDS

KEYWORD_GROUPS = {
    "핵심": CORE_KEYWORDS,
    "확장": EXTENDED_KEYWORDS,
    "보조": AUX_KEYWORDS,
}

GROUP_WEIGHT = {"핵심": 100, "확장": 50, "보조": 20}


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


def classify_language(info: dict[str, Any]) -> str:
    """Classify language: ko_confirmed / ko_likely / unknown / non_ko."""
    title = info.get("title", "")
    desc = info.get("description", "")[:500]
    channel = info.get("channel", "")
    tags = " ".join(info.get("tags") or [])
    combined = f"{title} {desc} {channel} {tags}"

    kr_ratio = _korean_ratio(combined)

    # Check subtitle languages
    subs = info.get("subtitles") or {}
    auto_subs = info.get("automatic_captions") or {}
    has_ko_sub = "ko" in subs
    has_ko_auto = "ko" in auto_subs

    if has_ko_sub and kr_ratio > 0.3:
        return "ko_confirmed"
    if has_ko_auto and kr_ratio > 0.2:
        return "ko_confirmed"
    if kr_ratio > 0.4:
        return "ko_confirmed"
    if kr_ratio > 0.15 or _has_korean(title):
        return "ko_likely"
    if kr_ratio > 0.05:
        return "unknown"
    return "non_ko"


# ---------------------------------------------------------------------------
# Keyword matching
# ---------------------------------------------------------------------------

def match_keywords(info: dict[str, Any]) -> tuple[list[str], str, int]:
    """Match video against keyword groups. Returns (matched_keywords, keyword_group, group_score)."""
    title = info.get("title", "").lower()
    desc = (info.get("description", "") or "")[:1000].lower()
    tags = " ".join(info.get("tags") or []).lower()
    combined = f"{title} {desc} {tags}"

    matched: list[str] = []
    best_group = ""
    best_score = 0

    for group_name, keywords in KEYWORD_GROUPS.items():
        for kw in keywords:
            if kw.lower() in combined and kw not in matched:
                matched.append(kw)
                score = GROUP_WEIGHT[group_name]
                if score > best_score:
                    best_score = score
                    best_group = group_name

    return matched, best_group, best_score


def compute_priority_score(
    keyword_score: int,
    view_count: int,
    like_count: int,
    upload_date: str,
    today: str,
) -> int:
    """Compute priority score combining keyword relevance, views, likes, recency."""
    import math
    # View score (log scale, max ~30 points)
    view_score = min(30, int(math.log10(max(view_count, 1)) * 6))

    # Like score (log scale, max ~15 points)
    like_score = min(15, int(math.log10(max(like_count, 1)) * 4))

    # Recency score (0-20, newer = higher)
    recency_score = 0
    if len(upload_date) == 8 and len(today) == 8:
        try:
            ud = datetime.strptime(upload_date, "%Y%m%d")
            td = datetime.strptime(today, "%Y%m%d")
            days_ago = (td - ud).days
            recency_score = max(0, 20 - days_ago * 3)
        except ValueError:
            pass

    return keyword_score + view_score + like_score + recency_score


# ---------------------------------------------------------------------------
# YouTube search
# ---------------------------------------------------------------------------

def search_and_filter(
    days: int = 7,
    max_results_per_query: int = 15,
    final_count: int = 10,
) -> list[dict[str, Any]]:
    """Search YouTube, filter by date/language/keywords, return top candidates."""
    today_str = datetime.now().strftime("%Y%m%d")
    cutoff = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y%m%d")

    # Phase 1: Collect candidate URLs
    logger.info("Phase 1: Searching YouTube with %d queries...", len(ALL_SEARCH_QUERIES))
    seen_ids: set[str] = set()
    candidate_urls: list[str] = []

    flat_opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": True, "playlistend": max_results_per_query,
    }

    for query in ALL_SEARCH_QUERIES:
        try:
            with yt_dlp.YoutubeDL(flat_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{max_results_per_query}:{query}", download=False)
                if info and "entries" in info:
                    for e in info["entries"]:
                        if e and e.get("id") and e["id"] not in seen_ids:
                            seen_ids.add(e["id"])
                            candidate_urls.append(f"https://www.youtube.com/watch?v={e['id']}")
        except Exception as ex:
            logger.warning("Search failed for '%s': %s", query, ex)

    logger.info("Found %d unique candidate URLs", len(candidate_urls))

    # Phase 2: Get full metadata and filter
    logger.info("Phase 2: Fetching metadata and filtering...")
    detail_opts = {
        "quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True,
    }

    candidates: list[dict[str, Any]] = []
    with yt_dlp.YoutubeDL(detail_opts) as ydl:
        for url in candidate_urls:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    continue

                upload_date = info.get("upload_date", "")

                # Date filter: within last N days
                if upload_date < cutoff_str:
                    continue

                # Language classification (keep all languages)
                lang_class = classify_language(info)

                # Keyword matching
                matched_kw, kw_group, kw_score = match_keywords(info)
                if not matched_kw:
                    continue

                view_count = info.get("view_count", 0) or 0
                like_count = info.get("like_count", 0) or 0
                priority = compute_priority_score(kw_score, view_count, like_count, upload_date, today_str)

                candidates.append({
                    "id": info.get("id", ""),
                    "title": info.get("title", ""),
                    "channel": info.get("channel", ""),
                    "uploader": info.get("uploader", ""),
                    "upload_date": upload_date,
                    "view_count": view_count,
                    "like_count": like_count,
                    "duration": info.get("duration", 0) or 0,
                    "description": info.get("description", ""),
                    "tags": info.get("tags") or [],
                    "webpage_url": info.get("webpage_url", url),
                    "thumbnail": info.get("thumbnail", ""),
                    "lang_class": lang_class,
                    "matched_keywords": matched_kw,
                    "keyword_group": kw_group,
                    "keyword_score": kw_score,
                    "priority_score": priority,
                    "chapters": info.get("chapters") or [],
                })
            except Exception:
                continue

    # Phase 3: Sort all candidates by priority score and select top N
    candidates.sort(key=lambda x: x["priority_score"], reverse=True)
    selected = candidates[:final_count]

    logger.info("Phase 3: Selected %d videos (from %d candidates)", len(selected), len(candidates))
    return selected


# ---------------------------------------------------------------------------
# Notion Database creation + upload
# ---------------------------------------------------------------------------

def create_daily_page_with_db(api_key: str, parent_page_id: str, selected: list[dict], days: int) -> tuple[str | None, str | None]:
    """Create a daily page with search criteria summary, then a DB inside it.

    Returns (page_id, db_id) or (None, None) on failure.
    """
    today = datetime.now().strftime("%m%d")
    today_full = datetime.now().strftime("%Y-%m-%d")
    page_title = f"Youtube Daily Research_{today}"

    # Build search criteria summary
    core_kw = ", ".join(CORE_KEYWORDS[:4])
    ext_kw = ", ".join(EXTENDED_KEYWORDS[:3])
    aux_kw = ", ".join(AUX_KEYWORDS[:3])
    criteria_text = (
        f"수집일: {today_full} | 최근 {days}일 이내 | 한국어 영상 | 상위 {len(selected)}개\n\n"
        f"[핵심 키워드] {core_kw}\n"
        f"[확장 키워드] {ext_kw}\n"
        f"[보조 키워드] {aux_kw}\n\n"
        f"선정 기준: 키워드 관련성 + 조회수 + 최신성 종합 점수"
    )

    # Create daily page
    page_endpoint = "https://api.notion.com/v1/pages"
    page_body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": "📋"},
        "properties": {
            "title": {"title": _build_rich_text(page_title)},
        },
        "children": [
            _build_heading_block("검색 기준", level=3),
            _build_paragraph_block(criteria_text),
        ],
    }

    try:
        resp = _notion_request("POST", page_endpoint, api_key, json_body=page_body)
        if resp.status_code != 200:
            logger.error("Failed to create daily page: %d — %s", resp.status_code, resp.text[:300])
            return None, None
        page_id = resp.json()["id"]
        logger.info("Created daily page: %s (id=%s)", page_title, page_id)
    except Exception as e:
        logger.error("Daily page creation error: %s", e)
        return None, None

    # Create DB inside the daily page
    db_id = _create_database_in_page(api_key, page_id)
    return page_id, db_id


def _create_database_in_page(api_key: str, page_id: str) -> str | None:
    """Create the research database inside a page."""
    endpoint = "https://api.notion.com/v1/databases"
    body = {
        "parent": {"type": "page_id", "page_id": page_id},
        "icon": {"type": "emoji", "emoji": "📊"},
        "title": [{"type": "text", "text": {"content": "영상 목록"}}],
        "properties": {
            "제목": {"title": {}},
            "채널": {"rich_text": {}},
            "업로드일": {"date": {}},
            "조회수": {"number": {"format": "number_with_commas"}},
            "원본": {"url": {}},
            "수집일": {"date": {}},
            "태그": {"multi_select": {}},
            "상태": {
                "select": {
                    "options": [
                        {"name": "수집 완료", "color": "gray"},
                        {"name": "요약 완료", "color": "blue"},
                        {"name": "검토 완료", "color": "green"},
                    ]
                }
            },
            "언어판별": {
                "select": {
                    "options": [
                        {"name": "ko_confirmed", "color": "green"},
                        {"name": "ko_likely", "color": "yellow"},
                    ]
                }
            },
            "키워드그룹": {
                "select": {
                    "options": [
                        {"name": "핵심", "color": "red"},
                        {"name": "확장", "color": "orange"},
                        {"name": "보조", "color": "gray"},
                    ]
                }
            },
            "매칭키워드": {"rich_text": {}},
            "우선순위점수": {"number": {"format": "number"}},
        },
    }

    try:
        resp = _notion_request("POST", endpoint, api_key, json_body=body)
        if resp.status_code == 200:
            db_id = resp.json()["id"]
            logger.info("Created Notion DB: %s", db_id)
            return db_id
        else:
            logger.error("Failed to create DB: %d — %s", resp.status_code, resp.text[:300])
            return None
    except Exception as e:
        logger.error("DB creation error: %s", e)
        return None


def _format_date_iso(raw: str) -> str:
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _generate_one_line_summary(transcript: TranscriptResult, meta: VideoMetadata) -> str:
    """Generate a single-line summary via OpenAI."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return f"{meta.title} — {meta.channel}"
    try:
        from openai import OpenAI
        text = _get_full_transcript_text(transcript, max_chars=5000)
        if not text:
            return f"{meta.title} — {meta.channel}"
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=150,
            messages=[{"role": "user", "content": f"다음 YouTube 영상 자막을 한 문장(1~2줄)으로 핵심만 요약해줘. 제목: {meta.title}\n\n자막:\n{text}"}],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("One-line summary failed: %s", e)
        return f"{meta.title} — {meta.channel}"


def _generate_detailed_summary(transcript: TranscriptResult, meta: VideoMetadata) -> str:
    """Generate a detailed topic-by-topic summary from transcript via OpenAI."""
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
            messages=[{"role": "user", "content": f"""다음 YouTube 영상의 자막/음성 텍스트를 주제별로 상세하게 정리해줘.

제목: {meta.title}
채널: {meta.channel}

자막:
{text}

요구사항:
- 영상에서 다루는 주제를 3~6개로 나눠서 정리
- 각 주제별로 구체적인 내용을 3~5문장으로 설명
- "▶ 주제명:" 형식으로 시작
- 자막 원문을 그대로 복사하지 말고, 내용을 이해하고 재구성해서 작성
- 구체적인 도구명, 방법론, 팁 등이 언급되면 반드시 포함"""}],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("Detailed summary failed: %s", e)
        return _build_content_summary(transcript, meta)


def upload_video_to_db(
    api_key: str,
    db_id: str,
    video: dict[str, Any],
    transcript: TranscriptResult,
    meta: VideoMetadata,
) -> bool:
    """Upload one video as a row in the Notion database with body content."""
    today_iso = datetime.now().strftime("%Y-%m-%d")

    # Build body content blocks
    content_summary = _build_content_summary(transcript, meta)
    key_insights = _build_key_insights(transcript, meta)
    practical_apps = _build_practical_applications(transcript, meta)
    transcript_excerpt = _build_transcript_excerpt(transcript)
    transcript_toggles = _build_full_transcript_toggles(transcript)

    # One-line summary via LLM
    one_line = _generate_one_line_summary(transcript, meta)

    # Detailed content summary via LLM (longer, transcript-based)
    detailed = _generate_detailed_summary(transcript, meta)

    # Description summary
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
            "원본": {"url": video["webpage_url"]},
            "수집일": {"date": {"start": today_iso}},
            "태그": {"multi_select": [{"name": t} for t in (video["tags"] or [])[:10]]},
            "상태": {"select": {"name": "요약 완료"}},
            "언어판별": {"select": {"name": video["lang_class"]}},
            "키워드그룹": {"select": {"name": video["keyword_group"]}},
            "매칭키워드": {"rich_text": _build_rich_text(", ".join(video["matched_keywords"]))},
            "우선순위점수": {"number": video["priority_score"]},
        },
        "children": children[:100],
    }

    endpoint = "https://api.notion.com/v1/pages"
    try:
        resp = _notion_request("POST", endpoint, api_key, json_body=body)
        if resp.status_code == 200:
            logger.info("Uploaded: %s (score=%d)", video["title"][:50], video["priority_score"])
            return True
        else:
            logger.error("Upload failed for %s: %d — %s", video["title"][:30], resp.status_code, resp.text[:200])
            return False
    except Exception as e:
        logger.error("Upload error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def run_daily_research(
    parent_page_id: str = "",
    days: int = 7,
    final_count: int = 10,
    whisper: bool = True,
    whisper_model: str = "base",
) -> None:
    """Full daily research pipeline."""
    api_key = os.getenv("NOTION_API_KEY", "")
    parent_page_id = parent_page_id or os.getenv("NOTION_PARENT_PAGE_ID", "")

    if not api_key:
        print("NOTION_API_KEY not set. Exiting.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f" YouTube Daily Research — {today}")
    print(f" 최근 {days}일 / 한국어 / 상위 {final_count}개")
    print(f"{'='*60}\n")

    # Step 1: Search and filter
    print("Step 1: YouTube 검색 및 필터링...")
    selected = search_and_filter(days=days, final_count=final_count)

    if not selected:
        print("기준에 맞는 영상이 없습니다.")
        return

    print(f"\n선정 결과: {len(selected)}개 영상")
    for i, v in enumerate(selected, 1):
        d = _format_date_iso(v["upload_date"])
        print(f"  {i:2d}. [{v['priority_score']:3d}점] [{d}] [{v['view_count']:>8,}] [{v['keyword_group']}] {v['title'][:50]}")
        print(f"      매칭: {', '.join(v['matched_keywords'][:4])}")

    # Step 2: Create daily page + DB
    print(f"\nStep 2: Notion 일별 페이지 + DB 생성...")
    page_id, db_id = create_daily_page_with_db(api_key, parent_page_id, selected, days)
    if not db_id:
        print("Notion 페이지/DB 생성 실패.")
        return
    print(f"  Page ID: {page_id}")
    print(f"  DB ID: {db_id}")

    # Step 3: Scrape metadata + transcript + upload
    print(f"\nStep 3: 메타데이터/자막 수집 + AI 요약 + Notion 업로드...")
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    uploaded = 0
    failed = 0

    for i, video in enumerate(selected, 1):
        vid = video["id"]
        url = video["webpage_url"]
        print(f"\n[{i}/{len(selected)}] {video['title'][:55]}")

        # Save raw info
        info_path = RAW_DIR / f"{vid}.info.json"
        info_path.write_text(json.dumps(video, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        # Extract transcript (with Whisper fallback)
        ffmpeg_path = "C:/Users/USER/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1-full_build/bin"
        if ffmpeg_path not in os.environ.get("PATH", ""):
            os.environ["PATH"] = ffmpeg_path + os.pathsep + os.environ.get("PATH", "")

        transcript = extract_transcript(vid, url, whisper_fallback=whisper, whisper_model=whisper_model)
        print(f"  자막: {transcript.status.value} ({len(transcript.segments)} segments)")

        # Build meta object
        meta = VideoMetadata(
            id=vid, title=video["title"], channel=video["channel"],
            uploader=video.get("uploader", ""), upload_date=video["upload_date"],
            duration=video["duration"], description=video.get("description", "")[:500],
            tags=video.get("tags") or [], webpage_url=url,
            view_count=video["view_count"], collected_at=collected_at,
        )

        # Upload to Notion
        ok = upload_video_to_db(api_key, db_id, video, transcript, meta)
        if ok:
            uploaded += 1
            print(f"  Notion: 업로드 완료")
        else:
            failed += 1
            print(f"  Notion: 업로드 실패")

    # Summary
    print(f"\n{'='*60}")
    print(f" 완료: {uploaded}/{len(selected)} 업로드 성공, {failed} 실패")
    print(f" Notion DB: {db_id}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="YouTube Daily Research Pipeline")
    parser.add_argument("--page-id", type=str, default="", help="Notion parent page ID")
    parser.add_argument("--days", type=int, default=3, help="Days to look back")
    parser.add_argument("--count", type=int, default=10, help="Number of videos to select")
    parser.add_argument("--no-whisper", action="store_true", help="Disable Whisper STT")
    parser.add_argument("--whisper-model", type=str, default="base", help="Whisper model size")
    args = parser.parse_args()

    run_daily_research(
        parent_page_id=args.page_id,
        days=args.days,
        final_count=args.count,
        whisper=not args.no_whisper,
        whisper_model=args.whisper_model,
    )
