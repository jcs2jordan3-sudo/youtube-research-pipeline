"""Allow running as: python -m app"""

from app.main import run_pipeline, parse_args

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
