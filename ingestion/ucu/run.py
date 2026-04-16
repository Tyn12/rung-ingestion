"""Orchestrate UCU / UCEA single pay spine ingestion.

Default behaviour tries to fetch the current year's circular; if no known URL
is registered and no --url is supplied, falls back to the hand-curated seed
spine so the pipeline still produces rows.

Usage:
    python -m ingestion.ucu.run --year 2024
    python -m ingestion.ucu.run --year 2024 --url https://www.ucea.ac.uk/.../spine.xlsx
    python -m ingestion.ucu.run --year 2024 --seed-fallback
    python -m ingestion.ucu.run --year 2024 --seed-only
"""
from __future__ import annotations
import argparse
import sys
from datetime import date
from pathlib import Path

from shared.config import load_env
load_env()

from ingestion.ucu.fetch import fetch_spine
from ingestion.ucu.load import load
from ingestion.ucu.parse import parse_spine_xlsx, seed_spine


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest UCU/UCEA single pay spine.")
    p.add_argument(
        "--year",
        type=int,
        default=None,
        help="Pay year starting (August). Defaults to current academic year.",
    )
    p.add_argument("--url", default=None, help="Override circular URL (.xlsx or .pdf).")
    p.add_argument("--from-file", type=Path, default=None)
    p.add_argument(
        "--seed-fallback",
        action="store_true",
        help="If fetch/parse finds nothing, fall back to the hand-curated seed spine.",
    )
    p.add_argument(
        "--seed-only",
        action="store_true",
        help="Skip fetch entirely and use the hand-curated seed spine.",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    # UK academic pay year starts 1 August; before August we're still in prior year.
    today = date.today()
    year = args.year or (today.year if today.month >= 8 else today.year - 1)

    records: list = []

    if args.seed_only:
        records = seed_spine(year)
        print(f"[ucu:run] Seed-only mode: emitted {len(records)} spine points for {year}.")
    else:
        try:
            path = args.from_file or fetch_spine(year, url=args.url)
            records = parse_spine_xlsx(path, year)
            print(f"[ucu:run] Parsed {len(records)} spine points from {path}.")
        except (ValueError, FileNotFoundError) as e:
            print(f"[ucu:run] Fetch/parse failed: {e}")
            if args.seed_fallback:
                records = seed_spine(year)
                print(
                    f"[ucu:run] Falling back to seed spine: {len(records)} points for {year}."
                )
            else:
                print("[ucu:run] Pass --seed-fallback to use the hand-curated spine.")
                return 1

        if not records and args.seed_fallback:
            records = seed_spine(year)
            print(
                f"[ucu:run] Parser produced 0 rows; falling back to seed spine ({len(records)})."
            )

    if not records:
        print("[ucu:run] No records to load.")
        return 1

    if args.dry_run:
        print("[ucu:run] Sample:", records[0].to_dict())
        return 0

    written = load(records)
    print(f"[ucu:run] Upserted {written} observations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
