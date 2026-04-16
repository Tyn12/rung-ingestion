"""Orchestrate Stack Overflow Developer Survey ingestion."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from shared.config import load_env
load_env()

from ingestion.stack_overflow.fetch import KNOWN_RELEASES, fetch_release
from ingestion.stack_overflow.load import load
from ingestion.stack_overflow.parse import parse_csv


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest a Stack Overflow survey release.")
    p.add_argument("--year", type=int, required=True,
                   help=f"Survey year. Known: {sorted(KNOWN_RELEASES)}")
    p.add_argument("--url", default=None, help="Override the download URL.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from-file", type=Path, default=None,
                   help="Skip download; parse this CSV path instead.")
    args = p.parse_args(argv)

    if args.from_file:
        csv_path = args.from_file
    else:
        csv_path = fetch_release(args.year, url=args.url)

    records = list(parse_csv(csv_path, args.year))
    print(f"[so:run] Parsed {len(records)} UK respondents.")
    if args.dry_run:
        if records:
            print("[so:run] Sample:", records[0].to_dict())
        return 0
    written = load(records)
    print(f"[so:run] Upserted {written} observations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
