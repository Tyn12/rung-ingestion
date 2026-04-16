"""Orchestrate Civil Service pay band ingestion."""
from __future__ import annotations
import argparse
import sys
from datetime import date
from pathlib import Path

from shared.config import load_env
load_env()

from ingestion.civil_service.fetch import fetch_bands
from ingestion.civil_service.load import load
from ingestion.civil_service.parse import parse_bands, seed_bands


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest UK Civil Service pay bands.")
    p.add_argument("--year", type=int, default=None, help="Pay year starting (April).")
    p.add_argument("--url", default=None)
    p.add_argument("--from-file", type=Path, default=None)
    p.add_argument("--seed-fallback", action="store_true")
    p.add_argument("--seed-only", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    today = date.today()
    year = args.year or (today.year if today.month >= 4 else today.year - 1)

    records: list = []

    if args.seed_only:
        records = seed_bands(year)
        print(f"[civil_service:run] Seed-only: {len(records)} bands for {year}.")
    else:
        try:
            path = args.from_file or fetch_bands(year, url=args.url)
            records = parse_bands(path, year)
            print(f"[civil_service:run] Parsed {len(records)} bands from {path}.")
        except (ValueError, FileNotFoundError) as e:
            print(f"[civil_service:run] Fetch/parse failed: {e}")
            if args.seed_fallback:
                records = seed_bands(year)
                print(
                    f"[civil_service:run] Falling back to seed bands: {len(records)}."
                )
            else:
                print("[civil_service:run] Pass --seed-fallback to use baseline.")
                return 1

        if not records and args.seed_fallback:
            records = seed_bands(year)
            print(
                f"[civil_service:run] Parser produced 0 rows; seed fallback ({len(records)})."
            )

    if not records:
        print("[civil_service:run] No records to load.")
        return 1

    if args.dry_run:
        print("[civil_service:run] Sample:", records[0].to_dict())
        return 0

    written = load(records)
    print(f"[civil_service:run] Upserted {written} observations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
