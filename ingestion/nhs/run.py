"""Orchestrate NHS Agenda for Change ingestion."""
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest NHS AfC pay scales.")
    p.add_argument("--year", type=int, default=None, help="Pay year starting (April). Defaults to current.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from-file", type=Path, default=None)
    args = p.parse_args(argv)

    year = args.year or (date.today().year if date.today().month >= 4 else date.today().year - 1)

    path = args.from_file or fetch_afc_table(year)
    records = parse_file(path, year)
    print(f"[nhs:run] Parsed {len(records)} spine points for {year}.")

    if args.dry_run:
        if records:
            print("[nhs:run] Sample:", records[0].to_dict())
        return 0

    written = load(records)
    print(f"[nhs:run] Upserted {written} observations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
