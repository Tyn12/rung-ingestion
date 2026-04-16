"""Orchestrate NHS Agenda for Change ingestion.

Supports three modes:
    1. Default: fetch HTML page from NHS Employers + parse
    2. --seed-fallback: try fetch+parse, fall back to seed data if 0 rows
    3. --seed-only: skip fetch, emit seed data directly
"""
from __future__ import annotations
import argparse
import sys
from datetime import date
from pathlib import Path

from shared.config import load_env
load_env()

from ingestion.nhs.fetch import fetch_afc_table
from ingestion.nhs.load import load
from ingestion.nhs.parse import parse_file
from ingestion.nhs.seed import emit_seed


def _try_seed_with_fallback(year: int) -> list:
    """Try seed data for the given year, then fall back to previous years."""
    for y in (year, year - 1, year - 2):
        try:
            records = emit_seed(y)
            if records:
                print(f"[nhs:run] Seed data: emitted {len(records)} spine points for {y}.")
                return records
        except ValueError:
            print(f"[nhs:run] No seed data for {y}, trying {y - 1}...")
            continue
    print("[nhs:run] No seed data available for any recent year.")
    return []


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest NHS AfC pay scales.")
    p.add_argument("--year", type=int, default=None, help="Pay year starting (April). Defaults to current.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from-file", type=Path, default=None)
    p.add_argument("--seed-only", action="store_true",
                   help="Skip fetch; emit hand-curated seed data only.")
    p.add_argument("--seed-fallback", action="store_true",
                   help="Try fetch+parse; fall back to seed if 0 rows parsed.")
    args = p.parse_args(argv)

    current_year = date.today().year if date.today().month >= 4 else date.today().year - 1
    year = args.year or current_year
    records = []

    if args.seed_only:
        records = _try_seed_with_fallback(year)
        print(f"[nhs:run] Seed-only mode: emitted {len(records)} spine points.")
    else:
        try:
            path = args.from_file or fetch_afc_table(year)
            records = parse_file(path, year)
            print(f"[nhs:run] Parsed {len(records)} spine points for {year}.")
        except Exception as e:
            print(f"[nhs:run] Fetch/parse failed for {year}: {e}")

        if not records and args.seed_fallback:
            print(f"[nhs:run] No rows from fetch; falling back to seed data.")
            records = _try_seed_with_fallback(year)

    if not records:
        print("[nhs:run] No observations to load.")
        return 1

    if args.dry_run:
        print("[nhs:run] Sample:", records[0].to_dict())
        return 0

    written = load(records)
    print(f"[nhs:run] Upserted {written} observations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())