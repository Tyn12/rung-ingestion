"""End-to-end orchestrator for the ONS/HMRC PAYE RTI ingestion pipeline."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from shared.config import load_env
load_env()

from ingestion.hmrc_paye.fetch import fetch_latest
from ingestion.hmrc_paye.load import load
from ingestion.hmrc_paye.parse import parse_run_dir


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest latest ONS/HMRC PAYE RTI bulletin.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from-dir", type=Path, default=None)
    args = p.parse_args(argv)

    if args.from_dir:
        run_dir = args.from_dir
    else:
        files = fetch_latest()
        if not files:
            print("[hmrc_paye:run] No files fetched.")
            return 1
        run_dir = files[0].parent

    records = parse_run_dir(run_dir)
    print(f"[hmrc_paye:run] Parsed {len(records)} observations.")
    if args.dry_run:
        if records:
            print("[hmrc_paye:run] Sample:", records[0].to_dict())
        return 0
    written = load(records)
    print(f"[hmrc_paye:run] Upserted {written} observations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
