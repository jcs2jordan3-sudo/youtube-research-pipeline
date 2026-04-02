"""Category-based YouTube Daily Research Pipeline.

10 AI categories with 36-hour window, smart dedup, Korean translation,
Notion folder structure (category folder → daily page → DB).
"""

from __future__ import annotations

import sys
import io
import json
import math
import os
import re
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher
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

from app.models import VideoMetadata, TranscriptResult, TranscriptStatus
from app.transcript import extract_transcript
from app.notion_export import (
    _build_rich_text, _build_paragraph_block, _build_heading_block,
    _build_toggle_block, _build_transcript_excerpt,
    _build_content_summary, _build_key_insights, _build_practical_applications,
    _notion_request, _get_full_transcript_text, format_timestamp,
)
from app.utils import setup_logging, RAW_DIR

logger = setup_logging()

# ---------------------------------------------------------------------------
# Default Notion parent page for YouTube AI Research
# ---------------------------------------------------------------------------
NOTION_YOUTUBE_AI_PAGE_ID = "33609d25c5a180bcab08f8662b65f073"

# ---------------------------------------------------------------------------
# 10 Categories
# ---------------------------------------------------------------------------

CATEGORIES: dict[str, dict[str, Any]] = {
    "AI 뉴스": {
        "emoji": "📰",
        "keywords": [
            "AI news", "generative AI news", "LLM news", "AI update",
            "AI breaking news", "AI weekly roundup", "OpenAI news",
            "Anthropic news", "Google AI news", "Microsoft AI news",
            "AI startup news", "AI funding news", "AI product launch",
            "AI feature update", "new AI model release", "latest AI tools",
            "AI regulation news", "AI policy news", "frontier AI news",
            "artificial intelligence trends",
        ],
    },
    "모델 비교분석": {
        "emoji": "⚖️",
        "keywords": [
            "LLM comparison", "AI model comparison", "Claude vs GPT",
            "GPT vs Gemini", "Claude vs Gemini", "best AI model",
            "best LLM for coding", "reasoning model benchmark",
            "open source LLM comparison", "frontier model comparison",
            "long context model comparison", "multimodal model comparison",
            "AI benchmark analysis", "top AI models 2026",
            "cheapest AI model comparison", "fastest AI model",
            "best AI chatbot comparison", "local LLM comparison",
            "small language model comparison", "AI model leaderboard",
        ],
    },
    "AI 에이전트": {
        "emoji": "🤖",
        "keywords": [
            "AI agents", "agentic AI", "autonomous AI workflow",
            "AI automation", "AI agent tutorial", "AI workflow automation",
            "enterprise AI agent", "multi agent system",
            "agent orchestration", "AI task automation",
            "agentic workflow", "AI assistant automation",
            "no code AI agent", "n8n AI agent", "LangGraph agent",
            "AutoGen multi agent", "AI agent use cases",
            "AI agent demo", "agentic systems", "AI agents for business",
        ],
    },
    "AI 코딩": {
        "emoji": "💻",
        "keywords": [
            "AI coding", "AI coding workflow", "coding agent",
            "Claude Code", "Cursor AI", "GitHub Copilot workflow",
            "AI software engineering", "AI code review", "vibe coding",
            "developer productivity AI", "agentic coding",
            "AI pair programming", "AI debugging workflow",
            "AI code generation", "AI refactoring", "AI coding tutorial",
            "best AI coding tools", "terminal AI coding",
            "AI dev workflow", "software engineering agents",
        ],
    },
    "멀티모달 AI": {
        "emoji": "🎭",
        "keywords": [
            "multimodal AI", "vision language model", "video generation AI",
            "voice AI", "speech to speech AI", "real time multimodal AI",
            "image understanding AI", "AI video model",
            "multimodal model demo", "voice assistant AI",
            "text to video AI", "image to video AI",
            "visual reasoning AI", "AI voice agent",
            "audio understanding AI", "AI screen understanding",
            "live multimodal assistant", "video AI workflow",
            "multimodal LLM", "best multimodal AI tools",
        ],
    },
    "업무 생산성": {
        "emoji": "📋",
        "keywords": [
            "AI for productivity", "AI workflow", "AI use cases",
            "practical AI use case", "AI for business",
            "AI for marketers", "AI for product managers",
            "AI for designers", "AI for analysts",
            "AI operations workflow", "AI for content creation",
            "AI for research", "AI for meetings",
            "AI for presentations", "AI for documents",
            "AI for customer support", "AI for email workflow",
            "AI for team productivity", "how to use AI at work",
            "real world AI workflow",
        ],
    },
    "엔터프라이즈 AI": {
        "emoji": "🏢",
        "keywords": [
            "enterprise AI", "generative AI enterprise",
            "AI adoption case study", "AI transformation",
            "enterprise AI workflow", "AI rollout strategy",
            "business AI implementation", "AI ROI case study",
            "enterprise agentic AI", "AI at work",
            "enterprise LLM strategy", "AI platform strategy",
            "enterprise AI stack", "AI implementation roadmap",
            "AI governance enterprise", "AI in large organizations",
            "enterprise AI use cases", "AI data stack",
            "enterprise AI architecture", "AI operating model",
        ],
    },
    "보안 거버넌스": {
        "emoji": "🛡️",
        "keywords": [
            "AI security", "AI governance", "prompt injection",
            "LLM security", "AI risk", "enterprise AI safety",
            "AI compliance", "responsible AI", "secure AI workflow",
            "AI policy", "AI guardrails", "red teaming AI",
            "AI privacy risk", "secure LLM deployment",
            "AI model safety", "AI regulation",
            "jailbreak prompt attack", "AI observability",
            "AI evaluation framework", "AI governance framework",
        ],
    },
    "오픈소스 로컬AI": {
        "emoji": "🔓",
        "keywords": [
            "open source AI", "open source LLM", "local LLM",
            "self hosted AI", "Ollama", "local AI workflow",
            "best open source AI models", "small language model",
            "local AI coding assistant", "llama model tutorial",
            "mistral model demo", "qwen model demo",
            "self hosted AI agent", "offline AI assistant",
            "run LLM locally", "local multimodal model",
            "private AI stack", "on premise AI",
            "open weight model", "best local AI tools",
        ],
    },
    "산업별 AI": {
        "emoji": "🏭",
        "keywords": [
            "AI in gaming", "AI for game development",
            "AI in education", "AI in healthcare", "AI in finance",
            "AI in design", "AI in marketing", "AI in legal",
            "AI in customer support", "AI in sales", "AI in HR",
            "AI in operations", "AI in manufacturing", "AI in media",
            "AI in content creation", "AI in startups",
            "AI in product management", "AI in mobile app development",
            "AI in software teams", "industry AI use cases",
        ],
    },
}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _normalize_title(title: str) -> str:
    """Normalize title for similarity comparison."""
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", "", t)  # remove punctuation
    t = re.sub(r"\s+", " ", t)     # collapse whitespace
    return t


def _title_similarity(t1: str, t2: str) -> float:
    """Compute normalized title similarity (0.0–1.0)."""
    n1, n2 = _normalize_title(t1), _normalize_title(t2)
    if not n1 or not n2:
        return 0.0
    return SequenceMatcher(None, n1, n2).ratio()


def _is_normal_video(info: dict[str, Any]) -> bool:
    """Check if video is a normal upload (not Shorts, live, premiere)."""
    duration = info.get("duration") or 0
    if duration < 61:
        return False  # Shorts or very short clips

    url = info.get("webpage_url", "")
    if "/shorts/" in url:
        return False

    live_status = info.get("live_status", "")
    if live_status in ("is_live", "is_upcoming", "was_live"):
        return False

    return True


# ---------------------------------------------------------------------------
# Korean translation via OpenAI
# ---------------------------------------------------------------------------

def _translate_to_korean(text: str, max_chars: int = 500) -> str:
    """Translate text to Korean via OpenAI. Returns original if fails or already Korean."""
    if not text or not text.strip():
        return text

    # Check if already mostly Korean
    korean_chars = len(re.findall(r"[\uac00-\ud7a3]", text))
    total = len(text.replace(" ", ""))
    if total > 0 and korean_chars / total > 0.3:
        return text  # already Korean enough

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return text

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"다음 텍스트를 자연스러운 한국어로 번역해줘. 고유명사(Claude, GPT, OpenAI 등)는 원문 유지. 번역문만 출력해.\n\n{text[:max_chars]}"
            }],
        )
        result = (resp.choices[0].message.content or "").strip()
        return result if result else text
    except Exception:
        return text


# ---------------------------------------------------------------------------
# Priority scoring
# ---------------------------------------------------------------------------

# Bonus keywords for practical / official / announcement content
_PRACTICAL_KW = [
    "tutorial", "how to", "guide", "workflow", "demo", "실전", "활용",
    "사용법", "방법", "팁", "tip", "walkthrough", "hands on",
]
_OFFICIAL_CHANNELS = [
    "openai", "anthropic", "google", "microsoft", "nvidia", "meta ai",
    "hugging face", "langchain", "google deepmind", "google cloud",
]


def _compute_priority(
    info: dict[str, Any],
    matched_keywords: list[str],
    cutoff_dt: datetime,
) -> int:
    """Compute priority score for a video."""
    view_count = info.get("view_count", 0) or 0
    like_count = info.get("like_count", 0) or 0
    upload_date = info.get("upload_date", "")

    # View score (log scale, max 25)
    view_score = min(25, int(math.log10(max(view_count, 1)) * 5))

    # Like score (log scale, max 10)
    like_score = min(10, int(math.log10(max(like_count, 1)) * 3))

    # Recency score (0-20, newer = higher)
    recency_score = 0
    if len(upload_date) == 8:
        try:
            ud = datetime.strptime(upload_date, "%Y%m%d")
            hours_ago = (datetime.now() - ud).total_seconds() / 3600
            recency_score = max(0, int(20 - hours_ago * 0.5))
        except ValueError:
            pass

    # Keyword match count bonus (max 15)
    kw_score = min(15, len(matched_keywords) * 3)

    # Official channel bonus (+10)
    channel = (info.get("channel", "") or "").lower()
    official_bonus = 10 if any(oc in channel for oc in _OFFICIAL_CHANNELS) else 0

    # Practical content bonus (+8)
    title = (info.get("title", "") or "").lower()
    desc = (info.get("description", "") or "")[:500].lower()
    combined = f"{title} {desc}"
    practical_bonus = 8 if any(pk in combined for pk in _PRACTICAL_KW) else 0

    return view_score + like_score + recency_score + kw_score + official_bonus + practical_bonus


# ---------------------------------------------------------------------------
# Search + Filter pipeline
# ---------------------------------------------------------------------------

def search_category_videos(
    category_name: str,
    keywords: list[str],
    hours: int = 36,
    max_per_query: int = 5,
    final_count: int = 5,
    global_seen_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Search YouTube for a category's keywords, filter and rank."""
    cutoff_dt = datetime.now() - timedelta(hours=hours)
    cutoff_str = cutoff_dt.strftime("%Y%m%d")

    if global_seen_ids is None:
        global_seen_ids = set()

    # Phase 1: Collect candidate URLs from all keywords
    logger.info("[%s] Phase 1: Searching %d keywords...", category_name, len(keywords))
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

    logger.info("[%s] Found %d unique candidates", category_name, len(all_urls))

    # Phase 2: Get full metadata + filter
    logger.info("[%s] Phase 2: Metadata + filtering...", category_name)
    detail_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True}
    pool: list[dict[str, Any]] = []

    with yt_dlp.YoutubeDL(detail_opts) as ydl:
        for url in all_urls:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    continue

                vid = info.get("id", "")

                # Global dedup
                if vid in global_seen_ids:
                    continue

                upload_date = info.get("upload_date", "")
                if not upload_date or upload_date < cutoff_str:
                    continue

                # Normal video only (no shorts, live, premiere)
                if not _is_normal_video(info):
                    continue

                matched_kw = id_to_keywords.get(vid, [])
                priority = _compute_priority(info, matched_kw, cutoff_dt)

                pool.append({
                    "id": vid,
                    "title": info.get("title", ""),
                    "channel": info.get("channel", ""),
                    "uploader": info.get("uploader", ""),
                    "upload_date": upload_date,
                    "view_count": info.get("view_count", 0) or 0,
                    "like_count": info.get("like_count", 0) or 0,
                    "duration": info.get("duration", 0) or 0,
                    "description": info.get("description", ""),
                    "tags": info.get("tags") or [],
                    "webpage_url": info.get("webpage_url", url),
                    "thumbnail": info.get("thumbnail", ""),
                    "matched_keywords": matched_kw,
                    "priority_score": priority,
                })
            except Exception:
                continue

    # Phase 3: Dedup (video_id already handled, now similar title dedup)
    pool = _deduplicate_by_title(pool)

    # Phase 4: Channel cap (max 2 per channel)
    pool = _apply_channel_cap(pool, max_per_channel=2)

    # Phase 5: Keyword cap (max 2 per keyword source)
    pool = _apply_keyword_cap(pool, max_per_keyword=2)

    # Phase 6: Sort by priority and select top N
    pool.sort(key=lambda x: x["priority_score"], reverse=True)
    selected = pool[:final_count]

    # Mark as globally seen
    for v in selected:
        global_seen_ids.add(v["id"])

    logger.info("[%s] Selected %d videos (from %d pool)", category_name, len(selected), len(pool))
    return selected


def _deduplicate_by_title(candidates: list[dict]) -> list[dict]:
    """Remove near-duplicate titles from same channel (85% similarity)."""
    result: list[dict] = []
    for c in candidates:
        is_dup = False
        for existing in result:
            if c["channel"] == existing["channel"]:
                sim = _title_similarity(c["title"], existing["title"])
                if sim >= 0.85:
                    # Keep the one with higher priority
                    if c["priority_score"] > existing["priority_score"]:
                        result.remove(existing)
                        result.append(c)
                    is_dup = True
                    break
        if not is_dup:
            result.append(c)
    return result


def _apply_channel_cap(candidates: list[dict], max_per_channel: int = 2) -> list[dict]:
    """Limit to N videos per channel, keeping highest priority."""
    candidates.sort(key=lambda x: x["priority_score"], reverse=True)
    channel_count: dict[str, int] = {}
    result: list[dict] = []
    for c in candidates:
        ch = c["channel"]
        if channel_count.get(ch, 0) < max_per_channel:
            result.append(c)
            channel_count[ch] = channel_count.get(ch, 0) + 1
    return result


def _apply_keyword_cap(candidates: list[dict], max_per_keyword: int = 2) -> list[dict]:
    """Limit contribution of each keyword to max N videos."""
    candidates.sort(key=lambda x: x["priority_score"], reverse=True)
    kw_count: dict[str, int] = {}
    result: list[dict] = []
    for c in candidates:
        # Check if any of its matched keywords still has room
        has_room = False
        for kw in c["matched_keywords"]:
            if kw_count.get(kw, 0) < max_per_keyword:
                has_room = True
                break
        if has_room:
            result.append(c)
            for kw in c["matched_keywords"]:
                kw_count[kw] = kw_count.get(kw, 0) + 1
    return result


# ---------------------------------------------------------------------------
# LLM summaries (Korean)
# ---------------------------------------------------------------------------

def _llm_one_line(transcript: TranscriptResult, meta: VideoMetadata) -> str:
    """Generate Korean one-line summary."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return _translate_to_korean(meta.title)
    try:
        from openai import OpenAI
        text = _get_full_transcript_text(transcript, max_chars=5000)
        if not text:
            return _translate_to_korean(meta.title)
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=150,
            messages=[{"role": "user", "content": (
                f"다음 YouTube 영상 자막을 한국어 한 문장(1~2줄)으로 핵심만 요약해줘. "
                f"고유명사는 원문 유지.\n제목: {meta.title}\n\n자막:\n{text}"
            )}],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return _translate_to_korean(meta.title)


def _llm_detailed(transcript: TranscriptResult, meta: VideoMetadata) -> str:
    """Generate Korean detailed summary."""
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
            messages=[{"role": "user", "content": f"""다음 YouTube 영상의 자막을 한국어로 주제별 상세 정리해줘.

제목: {meta.title}
채널: {meta.channel}

자막:
{text}

요구사항:
- 주제를 3~6개로 나눠서 정리
- 각 주제별 3~5문장으로 설명
- "▶ 주제명:" 형식으로 시작
- 자막 원문 복사 금지, 내용 재구성
- 구체적 도구명/방법론/팁 반드시 포함
- 전체 한국어로 작성 (고유명사 원문 유지)"""}],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return _build_content_summary(transcript, meta)


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def _format_date_iso(raw: str) -> str:
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _find_or_create_category_folder(
    api_key: str,
    parent_page_id: str,
    category_name: str,
    emoji: str,
) -> str | None:
    """Find or create a category folder page under the parent."""
    folder_title = f"{emoji} {category_name}"

    # Search existing children
    cursor: str | None = ""
    while cursor is not None:
        url = f"https://api.notion.com/v1/blocks/{parent_page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        try:
            resp = _notion_request("GET", url, api_key)
            if resp.status_code != 200:
                break
            data = resp.json()
            for block in data.get("results", []):
                if block.get("type") == "child_page":
                    block_title = block.get("child_page", {}).get("title", "")
                    if block_title == folder_title:
                        logger.info("[%s] Found existing folder: %s", category_name, block["id"])
                        return block["id"]
            cursor = data.get("next_cursor")
        except Exception:
            break

    # Create new folder
    page_body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": emoji},
        "properties": {"title": {"title": _build_rich_text(folder_title)}},
        "children": [
            _build_paragraph_block(f"{category_name} 카테고리의 일일 YouTube AI 리서치 결과가 날짜별로 정리됩니다."),
        ],
    }

    try:
        resp = _notion_request("POST", "https://api.notion.com/v1/pages", api_key, json_body=page_body)
        if resp.status_code != 200:
            logger.error("[%s] Folder creation failed: %d — %s", category_name, resp.status_code, resp.text[:300])
            return None
        folder_id = resp.json()["id"]
        logger.info("[%s] Created folder: %s (id=%s)", category_name, folder_title, folder_id)
        return folder_id
    except Exception as e:
        logger.error("[%s] Folder error: %s", category_name, e)
        return None


def _create_daily_page_with_db(
    api_key: str,
    folder_id: str,
    category_name: str,
    keywords_used: list[str],
    video_count: int,
    hours: int,
) -> tuple[str | None, str | None]:
    """Create daily research page + DB inside category folder."""
    today = datetime.now().strftime("%m%d")
    today_full = datetime.now().strftime("%Y-%m-%d")
    page_title = f"Daily Research_{today}"

    sample_kw = ", ".join(keywords_used[:8])
    criteria = (
        f"카테고리: {category_name} | 수집일: {today_full} | "
        f"최근 {hours}시간 | 상위 {video_count}개\n\n"
        f"[주요 검색 키워드] {sample_kw}\n\n"
        f"필터: 일반 영상만 | 채널당 2개 | 키워드당 2개 | 유사 제목 85% 중복 제거"
    )

    page_body = {
        "parent": {"type": "page_id", "page_id": folder_id},
        "icon": {"type": "emoji", "emoji": "📅"},
        "properties": {"title": {"title": _build_rich_text(page_title)}},
        "children": [
            _build_heading_block("수집 기준", level=3),
            _build_paragraph_block(criteria),
        ],
    }

    try:
        resp = _notion_request("POST", "https://api.notion.com/v1/pages", api_key, json_body=page_body)
        if resp.status_code != 200:
            logger.error("[%s] Daily page failed: %d", category_name, resp.status_code)
            return None, None
        page_id = resp.json()["id"]
    except Exception as e:
        logger.error("[%s] Daily page error: %s", category_name, e)
        return None, None

    # Create DB
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
            logger.error("[%s] DB failed: %d — %s", category_name, resp.status_code, resp.text[:200])
            return page_id, None
        db_id = resp.json()["id"]
        logger.info("[%s] Created daily page+DB: %s", category_name, page_title)
        return page_id, db_id
    except Exception as e:
        logger.error("[%s] DB error: %s", category_name, e)
        return page_id, None


def _upload_video_to_db(
    api_key: str,
    db_id: str,
    video: dict[str, Any],
    transcript: TranscriptResult,
    meta: VideoMetadata,
) -> bool:
    """Upload one video row with toggle blocks for timestamp/description."""
    # Translate title to Korean
    title_ko = _translate_to_korean(video["title"])

    # LLM summaries (in Korean)
    one_line = _llm_one_line(transcript, meta)
    key_insights = _build_key_insights(transcript, meta)
    practical_apps = _build_practical_applications(transcript, meta)
    detailed = _llm_detailed(transcript, meta)

    # Timestamp excerpt for toggle
    transcript_excerpt = _build_transcript_excerpt(transcript)

    # Description for toggle
    desc = video.get("description", "")[:1500] or "(설명란 없음)"

    # Build content blocks — timestamps and description in TOGGLE format
    children: list[dict] = [
        _build_heading_block("한줄 요약"),
        _build_paragraph_block(one_line),
        _build_heading_block("핵심 인사이트"),
        _build_paragraph_block(key_insights),
        _build_heading_block("실무 적용 포인트"),
        _build_paragraph_block(practical_apps),
        _build_heading_block("상세 내용 정리"),
        _build_paragraph_block(detailed),
        # Timestamp in TOGGLE (collapsible)
        _build_toggle_block("📎 타임스탬프 요약 (펼치기)", [
            _build_paragraph_block(transcript_excerpt),
        ]),
        # Description in TOGGLE (collapsible)
        _build_toggle_block("📝 설명란 요약 (펼치기)", [
            _build_paragraph_block(desc),
        ]),
    ]

    body = {
        "parent": {"database_id": db_id},
        "icon": {"type": "emoji", "emoji": "🎬"},
        "properties": {
            "제목": {"title": _build_rich_text(title_ko)},
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
# Main runner
# ---------------------------------------------------------------------------

def run_category_research(
    parent_page_id: str = "",
    hours: int = 36,
    videos_per_category: int = 5,
    whisper: bool = True,
    whisper_model: str = "base",
    categories: dict[str, dict] | None = None,
) -> None:
    """Run daily research for all 10 categories."""
    api_key = os.getenv("NOTION_API_KEY", "")
    parent_page_id = parent_page_id or os.getenv("NOTION_YOUTUBE_AI_PAGE_ID", "") or NOTION_YOUTUBE_AI_PAGE_ID

    if not api_key:
        print("NOTION_API_KEY not set.")
        return

    # ffmpeg path for Whisper
    ffmpeg_path = "C:/Users/USER/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1-full_build/bin"
    if ffmpeg_path not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_path + os.pathsep + os.environ.get("PATH", "")

    categories = categories or CATEGORIES
    today = datetime.now().strftime("%Y-%m-%d")
    total_cats = len(categories)

    print(f"\n{'='*60}")
    print(f" YouTube AI Daily Research — {today}")
    print(f" {total_cats}개 카테고리 × 최대 {videos_per_category}개 = 최대 {total_cats * videos_per_category}개")
    print(f" 수집 기간: 최근 {hours}시간")
    print(f"{'='*60}\n")

    total_uploaded = 0
    total_failed = 0
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    global_seen_ids: set[str] = set()

    for cat_idx, (cat_name, cat_config) in enumerate(categories.items(), 1):
        emoji = cat_config["emoji"]
        keywords = cat_config["keywords"]

        print(f"\n{'─'*50}")
        print(f" [{cat_idx}/{total_cats}] {emoji} {cat_name}")
        print(f"{'─'*50}")

        # Step 1: Search + filter
        print(f"  검색 중... ({len(keywords)}개 키워드)")
        selected = search_category_videos(
            cat_name, keywords,
            hours=hours, final_count=videos_per_category,
            global_seen_ids=global_seen_ids,
        )

        if not selected:
            print(f"  기준에 맞는 영상 없음. 스킵.")
            continue

        print(f"  {len(selected)}개 선정:")
        for i, v in enumerate(selected, 1):
            d = _format_date_iso(v["upload_date"])
            print(f"    {i}. [{v['priority_score']:3d}점] [{d}] [{v['view_count']:>8,}] {v['title'][:50]}")

        # Step 2: Find/create category folder + daily page + DB
        print(f"  Notion 카테고리 폴더 확인...")
        folder_id = _find_or_create_category_folder(api_key, parent_page_id, cat_name, emoji)
        if not folder_id:
            print(f"  카테고리 폴더 생성 실패. 스킵.")
            continue

        print(f"  Notion 데일리 페이지 생성...")
        page_id, db_id = _create_daily_page_with_db(
            api_key, folder_id, cat_name, keywords, len(selected), hours,
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
            info_path.write_text(
                json.dumps(video, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

            # Extract transcript
            transcript = extract_transcript(
                vid, video["webpage_url"],
                whisper_fallback=whisper, whisper_model=whisper_model,
            )

            meta = VideoMetadata(
                id=vid, title=video["title"], channel=video["channel"],
                uploader=video.get("uploader", ""),
                upload_date=video["upload_date"],
                duration=video["duration"],
                description=video.get("description", "")[:500],
                tags=video.get("tags") or [],
                webpage_url=video["webpage_url"],
                view_count=video["view_count"],
                like_count=video.get("like_count"),
                collected_at=collected_at,
            )

            # Upload to Notion
            ok = _upload_video_to_db(api_key, db_id, video, transcript, meta)
            if ok:
                total_uploaded += 1
                print("✓")
            else:
                total_failed += 1
                print("✗")

    print(f"\n{'='*60}")
    print(f" 완료: {total_uploaded} 업로드 / {total_failed} 실패")
    print(f" 카테고리: {total_cats}개 | 수집 기간: {hours}시간")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Category-based YouTube AI Daily Research")
    parser.add_argument("--page-id", type=str, default="",
                        help="Notion parent page ID (default: YouTube AI Research page)")
    parser.add_argument("--hours", type=int, default=36,
                        help="Hours to look back (default: 36)")
    parser.add_argument("--count", type=int, default=5,
                        help="Videos per category (default: 5)")
    parser.add_argument("--no-whisper", action="store_true",
                        help="Disable Whisper STT fallback")
    parser.add_argument("--whisper-model", type=str, default="base",
                        help="Whisper model size (default: base)")
    parser.add_argument("--categories", type=str, nargs="+", default=None,
                        help="Run specific categories only (e.g., 'AI뉴스' '모델비교분석')")
    args = parser.parse_args()

    # Filter categories if specified
    cats = CATEGORIES
    if args.categories:
        filtered = {}
        for name in args.categories:
            # Fuzzy match category names (strip spaces)
            for cat_name, cat_config in CATEGORIES.items():
                if name.replace(" ", "") in cat_name.replace(" ", ""):
                    filtered[cat_name] = cat_config
                    break
        if filtered:
            cats = filtered
        else:
            print(f"지정된 카테고리를 찾을 수 없습니다: {args.categories}")
            print(f"사용 가능한 카테고리: {', '.join(CATEGORIES.keys())}")
            sys.exit(1)

    run_category_research(
        parent_page_id=args.page_id,
        hours=args.hours,
        videos_per_category=args.count,
        whisper=not args.no_whisper,
        whisper_model=args.whisper_model,
        categories=cats,
    )
