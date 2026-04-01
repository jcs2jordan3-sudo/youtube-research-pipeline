"""YouTube Research Pipeline — main entry point."""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from app.models import (
    ProcessingResult,
    PipelineManifest,
    TranscriptStatus,
    VideoMetadata,
    TranscriptResult,
)
from app.scraper import scrape_video
from app.transcript import extract_transcript
from app.formatter import save_markdown
from app.brief_generator import save_brief
from app.notion_export import save_notion_payload
from app.utils import load_urls, extract_video_id, setup_logging, RAW_DIR, DATA_DIR, PROCESSED_DIR

logger = setup_logging()


def process_single(
    url: str,
    collected_at: str,
    whisper_fallback: bool = False,
    whisper_model: str = "base",
) -> tuple[ProcessingResult, VideoMetadata | None, TranscriptResult | None]:
    """Process a single YouTube URL through the full pipeline."""
    video_id = extract_video_id(url)
    result = ProcessingResult(url=url, video_id=video_id or "", success=False)

    if not video_id:
        result.error = f"Could not extract video ID from URL: {url}"
        logger.error(result.error)
        return result, None, None

    # Step 1: Scrape metadata
    meta = scrape_video(url, collected_at)
    if meta is None:
        result.error = f"Metadata extraction failed for {url}"
        logger.error(result.error)
        return result, None, None
    result.metadata_ok = True
    result.video_id = meta.id

    # Step 2: Extract transcript
    transcript = extract_transcript(meta.id, url, whisper_fallback=whisper_fallback, whisper_model=whisper_model)
    result.transcript_status = transcript.status.value
    if transcript.status == TranscriptStatus.SUCCESS:
        result.transcript_ok = True

    # Step 3: Generate Markdown
    try:
        md_path = save_markdown(meta, transcript)
        result.markdown_path = md_path
    except Exception as e:
        logger.error("Markdown generation failed for %s: %s", meta.id, e)
        result.error = f"Markdown generation failed: {e}"
        return result, meta, transcript

    result.success = True
    return result, meta, transcript


def run_pipeline(
    urls_path: Path | None = None,
    theme: str = "YouTube Research",
    lang: str = "ko,en",
    output_dir: Path | None = None,
    skip_existing: bool = False,
    parallel: int = 1,
    summarize: bool = False,
    obsidian: bool = False,
    notion_upload: bool = False,
    whisper: bool = False,
    whisper_model: str = "base",
) -> PipelineManifest:
    """Run the full pipeline on all URLs.

    Args:
        urls_path: Path to the URL list file.
        theme: Research theme name shown in the header.
        lang: Comma-separated language priority (e.g. "ko,en").
        output_dir: Custom output base directory (unused placeholder for future).
        skip_existing: When True, skip videos whose processed .md already exists.
        parallel: Number of parallel workers (1 = sequential).
        summarize: When True, generate LLM summaries via Claude API.
        obsidian: When True, export Obsidian-formatted notes.
        notion_upload: When True, upload to Notion API (requires env vars).
    """
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    urls = load_urls(urls_path)

    if not urls:
        logger.warning("No URLs found. Add URLs to data/inbox/youtube_urls.txt")
        print("\n⚠ No URLs found in data/inbox/youtube_urls.txt")
        return PipelineManifest(run_at=collected_at)

    manifest = PipelineManifest(
        run_at=collected_at,
        total_urls=len(urls),
    )

    videos: list[tuple[VideoMetadata, TranscriptResult]] = []
    skipped_count = 0

    print(f"\n{'='*60}")
    print(f" {theme}")
    print(f" {collected_at} | {len(urls)} URLs | workers={parallel}")
    if skip_existing:
        print(f" --skip-existing enabled")
    if whisper:
        print(f" --whisper enabled (model={whisper_model})")
    if summarize:
        print(f" --summarize enabled (Claude API)")
    if obsidian:
        print(f" --obsidian enabled")
    print(f"{'='*60}\n")

    # Filter out already-processed URLs if skip_existing
    urls_to_process: list[tuple[int, str]] = []
    for i, url in enumerate(urls, 1):
        if skip_existing:
            video_id = extract_video_id(url)
            if video_id:
                processed_path = PROCESSED_DIR / f"{video_id}.md"
                if processed_path.exists():
                    skipped_count += 1
                    logger.info("Skipping already-processed video %s", video_id)
                    print(f"[{i}/{len(urls)}] {url}")
                    print(f"  -> SKIPPED (already processed)")
                    continue
        urls_to_process.append((i, url))

    # Process URLs (parallel or sequential)
    if parallel > 1 and len(urls_to_process) > 1:
        workers = min(parallel, len(urls_to_process))
        print(f"Processing {len(urls_to_process)} URLs with {workers} parallel workers...\n")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(process_single, url, collected_at, whisper, whisper_model): (idx, url)
                for idx, url in urls_to_process
            }
            for future in as_completed(futures):
                idx, url = futures[future]
                result, meta, transcript = future.result()
                manifest.results.append(result)
                if result.success and meta and transcript:
                    manifest.success_count += 1
                    videos.append((meta, transcript))
                    if result.transcript_ok:
                        manifest.transcript_success_count += 1
                    status = "OK"
                    if not result.transcript_ok:
                        status += f" (transcript: {result.transcript_status})"
                    print(f"[{idx}/{len(urls)}] {url}\n  -> {status}")
                else:
                    manifest.failure_count += 1
                    manifest.failed_urls.append(url)
                    print(f"[{idx}/{len(urls)}] {url}\n  -> FAILED: {result.error}")
    else:
        for idx, url in urls_to_process:
            print(f"[{idx}/{len(urls)}] {url}")
            result, meta, transcript = process_single(url, collected_at, whisper, whisper_model)
            manifest.results.append(result)
            if result.success and meta and transcript:
                manifest.success_count += 1
                videos.append((meta, transcript))
                if result.transcript_ok:
                    manifest.transcript_success_count += 1
                status = "OK"
                if not result.transcript_ok:
                    status += f" (transcript: {result.transcript_status})"
                print(f"  -> {status}")
            else:
                manifest.failure_count += 1
                manifest.failed_urls.append(url)
                print(f"  -> FAILED: {result.error}")

    # Step 4: LLM Summarization (optional)
    if summarize and videos:
        print("\nGenerating LLM summaries...")
        try:
            from app.summarizer import summarize_video, inject_summary_into_markdown
            for meta, transcript in videos:
                summary = summarize_video(meta, transcript)
                if summary:
                    md_path = PROCESSED_DIR / f"{meta.id}.md"
                    if md_path.exists():
                        original = md_path.read_text(encoding="utf-8")
                        updated = inject_summary_into_markdown(original, summary)
                        md_path.write_text(updated, encoding="utf-8")
                        print(f"  LLM summary injected: {meta.id}")
        except Exception as e:
            logger.error("LLM summarization step failed: %s", e)
            print(f"  LLM summarization error: {e}")

    # Step 5: Generate research brief
    if videos:
        brief_path = save_brief(videos, theme)
        print(f"\nResearch brief: {brief_path}")

    # Step 6: Generate Notion payload
    if videos:
        notion_path = save_notion_payload(videos)
        print(f"Notion payload: {notion_path}")

    # Step 6b: Notion upload (optional)
    if notion_upload and videos:
        try:
            from app.notion_export import upload_to_notion
            print("\nUploading to Notion...")
            upload_result = upload_to_notion(videos)
            print(f"  Notion upload: {upload_result}")
        except Exception as e:
            logger.error("Notion upload failed: %s", e)
            print(f"  Notion upload error: {e}")

    # Step 7: Obsidian export (optional)
    if obsidian and videos:
        try:
            from app.obsidian_export import save_obsidian_note, save_obsidian_index
            print("\nExporting Obsidian notes...")
            for meta, transcript in videos:
                obs_path = save_obsidian_note(meta, transcript)
                print(f"  Obsidian note: {obs_path}")
            index_path = save_obsidian_index(videos)
            print(f"  Obsidian index: {index_path}")
        except Exception as e:
            logger.error("Obsidian export failed: %s", e)
            print(f"  Obsidian export error: {e}")

    # Save manifest
    manifest_path = DATA_DIR / "manifest.json"
    manifest.save(manifest_path)

    # Save failed URLs
    if manifest.failed_urls:
        failed_path = DATA_DIR / "failed_urls.txt"
        failed_path.write_text(
            "\n".join(manifest.failed_urls) + "\n", encoding="utf-8"
        )
        print(f"Failed URLs: {failed_path}")

    # Summary
    print(f"\n{'='*60}")
    print(f" Pipeline Complete")
    print(f"{'='*60}")
    print(f"  Total:      {manifest.total_urls}")
    if skipped_count:
        print(f"  Skipped:    {skipped_count}")
    print(f"  Processed:  {manifest.success_count + manifest.failure_count}")
    print(f"  Success:    {manifest.success_count}")
    print(f"  Failed:     {manifest.failure_count}")
    print(f"  Transcript: {manifest.transcript_success_count}/{manifest.success_count}")
    print(f"  Manifest:   {manifest_path}")
    print()

    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="YouTube Research Pipeline - scrape, transcribe, and summarise YouTube videos.",
    )
    parser.add_argument(
        "--urls", "-u",
        type=Path,
        default=None,
        help="Path to URL file (default: data/inbox/youtube_urls.txt)",
    )
    parser.add_argument(
        "--theme", "-t",
        type=str,
        default="YouTube Research",
        help='Research theme name (default: "YouTube Research")',
    )
    parser.add_argument(
        "--lang",
        type=str,
        default="ko,en",
        help='Comma-separated language priority (default: "ko,en")',
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=None,
        help="Custom output base directory (optional)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=False,
        help="Skip already-processed video IDs (checks data/processed/{id}.md)",
    )
    parser.add_argument(
        "--parallel", "-p",
        type=int,
        default=1,
        help="Number of parallel workers (default: 1 = sequential)",
    )
    parser.add_argument(
        "--summarize",
        action="store_true",
        default=False,
        help="Generate LLM summaries via Claude API (requires ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--obsidian",
        action="store_true",
        default=False,
        help="Export Obsidian-formatted notes to data/obsidian/",
    )
    parser.add_argument(
        "--notion-upload",
        action="store_true",
        default=False,
        help="Upload to Notion API (requires NOTION_API_KEY and NOTION_DATABASE_ID)",
    )
    parser.add_argument(
        "--whisper",
        action="store_true",
        default=False,
        help="Use Whisper STT fallback when subtitles unavailable (requires faster-whisper)",
    )
    parser.add_argument(
        "--whisper-model",
        type=str,
        default="base",
        help='Whisper model size: tiny, base, small, medium, large-v3 (default: "base")',
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    run_pipeline(
        urls_path=args.urls,
        theme=args.theme,
        lang=args.lang,
        output_dir=args.output_dir,
        skip_existing=args.skip_existing,
        parallel=args.parallel,
        summarize=args.summarize,
        obsidian=args.obsidian,
        notion_upload=args.notion_upload,
        whisper=args.whisper,
        whisper_model=args.whisper_model,
    )
