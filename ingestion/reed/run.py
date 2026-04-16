"""End-to-end orchestrator for the Reed ingestion pipeline.

Usage:
    python -m ingestion.reed.run                       # broad sweep, today
    python -m ingestion.reed.run --locations London Manchester
    python -m ingestion.reed.run --keywords "software engineer"
    python -m ingestion.reed.run --dry-run             # fetch + parse, no DB write
    python -m ingestion.reed.run --from-dir data/raw/reed/2026-04-16
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from shared.config import load_env
load_env()

from ingestion.reed.fetch import UK_LOCATIONS, fetch_listings
from ingestion.reed.load import load
from ingestion.reed.parse import parse_raw_dir


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the Reed Jobseeker ingestion pipeline.")
    p.add_argument("--locations", nargs="*", help="Subset of UK_LOCATIONS to sweep.")
    p.add_argument("--keywords", default=None, help="Optional keyword filter.")
    p.add_argument("--max-pages", type=int, default=10, help="Max pages per location.")
    p.add_argument("--dry-run", action="store_true", help="Skip DB load.")
    p.add_argument(
        "--from-dir",
        type=Path,
        default=None,
        help="Skip fetch; parse this existing raw directory instead.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.from_dir:
        run_dir = args.from_dir
        print(f"[reed:run] Using cached directory {run_dir}")
    else:
        name_to_radius = dict(UK_LOCATIONS)
        locs = None
        if args.locations:
            locs = [(n, name_to_radius.get(n, 15)) for n in args.locations]

        files = fetch_listings(
            locations=locs,
            keywords=args.keywords,
            max_pages_per_location=args.max_pages,
        )
        if not files:
            print("[reed:run] No pages fetched; exiting.")
            return 1
        run_dir = files[0].parent.parent  # data/raw/reed/{date}

    observations = parse_raw_dir(run_dir)
    print(f"[reed:run] Parsed {len(observations)} unique listings.")

    if args.dry_run:
        print("[reed:run] Dry run — skipping database load.")
        if observations:
            print("[reed:run] Sample:", observations[0].to_dict())
        return 0

    written = load(observations)
    print(f"[reed:run] Upserted {written} observations into compensation_observations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
