"""Command-line entry point for the offline article archiver.

The CLI is intentionally thin:
- validate that runtime dependencies exist
- parse user-facing options into ArchiveConfig
- hand execution over to the archive pipeline
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional


def _check_runtime_deps() -> None:
    """Fail early with a clear message if required scraping libraries are missing."""
    missing = []
    for mod in ("httpx", "bs4", "readability", "lxml"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if missing:
        print(
            "Missing runtime dependencies: "
            + ", ".join(missing)
            + "\nInstall with: `pip install -r requirements.txt` (or `pip install -e .`)",
            file=sys.stderr,
        )
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    """Build the user-facing CLI contract.

    Keeping argument definitions in one place makes it easier to inspect the
    supported archive behaviors without reading the pipeline implementation.
    """
    parser = argparse.ArgumentParser(prog="news_crawler")
    sub = parser.add_subparsers(dest="command", required=True)

    archive = sub.add_parser("archive", help="Archive articles for offline viewing")
    archive.add_argument("--input", required=True, help="Input file: urls.txt or urls.jsonl")
    archive.add_argument("--out", default="output", help="Output directory (default: output)")
    archive.add_argument("--concurrency", type=int, default=8, help="Global concurrency (default: 8)")
    archive.add_argument("--per-domain", type=int, default=2, help="Per-domain concurrency (default: 2)")
    archive.add_argument("--timeout", type=float, default=20.0, help="Request timeout seconds (default: 20)")
    archive.add_argument("--retries", type=int, default=2, help="Retry count per request (default: 2)")
    archive.add_argument("--render-fallback", action="store_true", help="Use Playwright if HTTP extraction fails")
    archive.add_argument("--only-http", action="store_true", help="Never use Playwright")
    archive.add_argument("--only-render", action="store_true", help="Always use Playwright for HTML acquisition")
    archive.add_argument("--force", action="store_true", help="Re-archive even if already successful")
    archive.add_argument("--max-images", type=int, default=30, help="Max images per article (default: 30)")
    archive.add_argument("--max-image-bytes", type=int, default=15_000_000, help="Max bytes per image (default: 15000000)")
    archive.add_argument("--user-agent", default=None, help="Custom User-Agent header")
    archive.add_argument("--accept-language", default="en-US,en;q=0.9", help="Accept-Language header")
    archive.add_argument(
        "--save-raw-on-failure",
        action="store_true",
        help="Save raw.html when extraction fails",
    )
    archive.add_argument(
        "--no-save-raw-on-failure",
        dest="save_raw_on_failure",
        action="store_false",
        help="Do not save raw.html on extraction failure",
    )
    archive.set_defaults(save_raw_on_failure=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Parse CLI arguments and execute the requested subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "archive":
        _check_runtime_deps()
        from .archive import ArchiveConfig, run_archive

        cfg = ArchiveConfig(
            input_path=Path(args.input),
            out_dir=Path(args.out),
            concurrency=max(1, args.concurrency),
            per_domain=max(1, args.per_domain),
            timeout_seconds=max(1.0, args.timeout),
            retries=max(0, args.retries),
            render_fallback=bool(args.render_fallback),
            only_http=bool(args.only_http),
            only_render=bool(args.only_render),
            force=bool(args.force),
            max_images=max(0, args.max_images),
            max_image_bytes=max(1, args.max_image_bytes),
            user_agent=args.user_agent,
            accept_language=args.accept_language,
            save_raw_on_failure=bool(args.save_raw_on_failure),
        )
        if cfg.only_http and (cfg.only_render or cfg.render_fallback):
            parser.error("--only-http conflicts with --only-render/--render-fallback")
        if cfg.only_render and cfg.only_http:
            parser.error("--only-render conflicts with --only-http")
        run_archive(cfg)
        return 0

    parser.error("Unknown command")
    return 2
