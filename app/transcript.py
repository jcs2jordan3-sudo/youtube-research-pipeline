"""Transcript extraction: youtube-transcript-api → yt-dlp subtitles → Whisper STT."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from app.models import TranscriptResult, TranscriptSegment, TranscriptStatus
from app.utils import RAW_DIR, LANGUAGE_PRIORITY, setup_logging

logger = setup_logging()


def _try_youtube_transcript_api(video_id: str) -> TranscriptResult | None:
    """Attempt extraction via youtube-transcript-api (v1.x API)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            TranscriptsDisabled,
            NoTranscriptFound,
            VideoUnavailable,
        )
    except ImportError:
        logger.debug("youtube-transcript-api not installed, skipping")
        return None

    ytt_api = YouTubeTranscriptApi()

    # Try fetching with language priority
    try:
        fetched = ytt_api.fetch(video_id, languages=LANGUAGE_PRIORITY)
        segments = [
            TranscriptSegment(
                text=s.text,
                start=s.start,
                duration=s.duration,
                language=fetched.language,
                is_generated=fetched.is_generated,
            )
            for s in fetched.snippets
        ]
        return TranscriptResult(
            video_id=video_id,
            status=TranscriptStatus.SUCCESS,
            language=fetched.language,
            is_generated=fetched.is_generated,
            segments=segments,
        )
    except TranscriptsDisabled:
        return TranscriptResult(
            video_id=video_id,
            status=TranscriptStatus.DISABLED,
            error_message="Transcripts are disabled for this video",
        )
    except VideoUnavailable:
        return TranscriptResult(
            video_id=video_id,
            status=TranscriptStatus.NOT_AVAILABLE,
            error_message="Video unavailable",
        )
    except NoTranscriptFound:
        pass
    except Exception as e:
        logger.warning("youtube-transcript-api fetch error for %s: %s", video_id, e)

    # Try fetching any available language via list
    try:
        transcript_list = ytt_api.list(video_id)
        for entry in transcript_list:
            lang_code = entry.language_code if hasattr(entry, "language_code") else str(entry)
            try:
                fetched = ytt_api.fetch(video_id, languages=[lang_code])
                segments = [
                    TranscriptSegment(
                        text=s.text,
                        start=s.start,
                        duration=s.duration,
                        language=fetched.language,
                        is_generated=fetched.is_generated,
                    )
                    for s in fetched.snippets
                ]
                return TranscriptResult(
                    video_id=video_id,
                    status=TranscriptStatus.SUCCESS,
                    language=fetched.language,
                    is_generated=fetched.is_generated,
                    segments=segments,
                )
            except Exception:
                continue
    except Exception as e:
        logger.warning("youtube-transcript-api list fallback failed for %s: %s", video_id, e)

    return None


def _try_yt_dlp_subtitles(video_id: str, url: str) -> TranscriptResult | None:
    """Fallback: extract subtitles via yt-dlp."""
    try:
        import yt_dlp
    except ImportError:
        return None

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": LANGUAGE_PRIORITY + ["all"],
        "subtitlesformat": "json3",
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                return None

            for sub_key in ("subtitles", "automatic_captions"):
                subs: dict[str, Any] = info.get(sub_key) or {}
                for lang in LANGUAGE_PRIORITY:
                    if lang in subs:
                        is_gen = sub_key == "automatic_captions"
                        return TranscriptResult(
                            video_id=video_id,
                            status=TranscriptStatus.SUCCESS,
                            language=lang,
                            is_generated=is_gen,
                            segments=[],
                            error_message="Subtitles available via yt-dlp but inline extraction not supported in fallback mode",
                        )
            return None

    except Exception as e:
        logger.warning("yt-dlp subtitle fallback failed for %s: %s", video_id, e)
        return None


# ---------------------------------------------------------------------------
# Whisper STT fallback: download audio → transcribe locally
# ---------------------------------------------------------------------------

def _download_audio(url: str, video_id: str) -> Path | None:
    """Download audio-only via yt-dlp. Returns path to audio file."""
    try:
        import yt_dlp
    except ImportError:
        return None

    audio_dir = RAW_DIR / "audio"
    audio_dir.mkdir(exist_ok=True)
    out_path = audio_dir / f"{video_id}.%(ext)s"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(out_path),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "64",
        }],
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        # Find the output file
        mp3_path = audio_dir / f"{video_id}.mp3"
        if mp3_path.exists():
            logger.info("Downloaded audio: %s (%.1f MB)", mp3_path, mp3_path.stat().st_size / 1024 / 1024)
            return mp3_path

        # Try other extensions
        for ext in ["m4a", "webm", "opus", "wav"]:
            p = audio_dir / f"{video_id}.{ext}"
            if p.exists():
                logger.info("Downloaded audio: %s", p)
                return p

        logger.error("Audio download completed but file not found for %s", video_id)
        return None

    except Exception as e:
        logger.error("Audio download failed for %s: %s", video_id, e)
        return None


def _transcribe_with_whisper(
    audio_path: Path,
    video_id: str,
    model_size: str = "base",
) -> TranscriptResult | None:
    """Transcribe audio using faster-whisper."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.warning("faster-whisper not installed. Install with: pip install faster-whisper")
        return None

    logger.info("Transcribing %s with Whisper (model=%s)...", video_id, model_size)

    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments_iter, info = model.transcribe(
            str(audio_path),
            beam_size=5,
            language=None,  # auto-detect
            vad_filter=True,
        )

        detected_lang = info.language
        logger.info("Whisper detected language: %s (prob=%.2f)", detected_lang, info.language_probability)

        segments = []
        for seg in segments_iter:
            segments.append(
                TranscriptSegment(
                    text=seg.text.strip(),
                    start=seg.start,
                    duration=seg.end - seg.start,
                    language=detected_lang,
                    is_generated=True,
                )
            )

        if not segments:
            return TranscriptResult(
                video_id=video_id,
                status=TranscriptStatus.EXTRACTION_FAILED,
                error_message="Whisper produced no segments",
            )

        logger.info("Whisper transcription complete: %d segments, lang=%s", len(segments), detected_lang)

        return TranscriptResult(
            video_id=video_id,
            status=TranscriptStatus.SUCCESS,
            language=detected_lang,
            is_generated=True,
            segments=segments,
        )

    except Exception as e:
        logger.error("Whisper transcription failed for %s: %s", video_id, e, exc_info=True)
        return None


def _try_whisper_stt(video_id: str, url: str, model_size: str = "base") -> TranscriptResult | None:
    """Full Whisper STT fallback: download audio → transcribe."""
    logger.info("Attempting Whisper STT for %s", video_id)

    # Check if audio already downloaded
    audio_dir = RAW_DIR / "audio"
    existing = None
    for ext in ["mp3", "m4a", "webm", "opus", "wav"]:
        p = audio_dir / f"{video_id}.{ext}"
        if p.exists():
            existing = p
            break

    audio_path = existing or _download_audio(url, video_id)
    if audio_path is None:
        return TranscriptResult(
            video_id=video_id,
            status=TranscriptStatus.EXTRACTION_FAILED,
            error_message="Audio download failed",
        )

    result = _transcribe_with_whisper(audio_path, video_id, model_size)
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def extract_transcript(
    video_id: str,
    url: str,
    whisper_fallback: bool = True,
    whisper_model: str = "base",
) -> TranscriptResult:
    """Extract transcript for a video, trying multiple methods.

    Priority:
    1. youtube-transcript-api (fastest, uses existing subtitles)
    2. yt-dlp subtitle extraction
    3. Whisper STT (downloads audio, transcribes locally)
    """
    logger.info("Extracting transcript for %s", video_id)

    # Primary: youtube-transcript-api
    result = _try_youtube_transcript_api(video_id)
    if result is not None and result.status == TranscriptStatus.SUCCESS:
        _save_transcript(result)
        return result

    # Fallback 1: yt-dlp subtitles
    result = _try_yt_dlp_subtitles(video_id, url)
    if result is not None and result.status == TranscriptStatus.SUCCESS and result.segments:
        _save_transcript(result)
        return result

    # Fallback 2: Whisper STT
    if whisper_fallback:
        result = _try_whisper_stt(video_id, url, whisper_model)
        if result is not None:
            _save_transcript(result)
            return result

    # Nothing worked
    result = TranscriptResult(
        video_id=video_id,
        status=TranscriptStatus.EXTRACTION_FAILED,
        error_message="All extraction methods failed (subtitles + Whisper STT)",
    )
    _save_transcript(result)
    return result


def _save_transcript(result: TranscriptResult) -> None:
    """Save transcript result to JSON."""
    out_path = RAW_DIR / f"{result.video_id}.transcript.json"
    out_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.debug("Saved transcript: %s (status=%s)", out_path, result.status.value)
