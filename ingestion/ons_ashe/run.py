"""End-to-end orchestrator for ONS ASHE Table 2 ingestion.

Usage:
    python -m ingestion.ons_ashe.run <directory>
    python -m ingestion.ons_ashe.run "ONS occupation/ashetable22025provisional"
    python -m ingestion.ons_ashe.run --dry-run "ONS occupation/ashetable22025provisional"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from shared.config import load_env
load_env()

from ingestion.ons_ashe.load import load
from ingestion.ons_ashe.parse import parse_directory


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ingest ONS ASHE Table 2 Excel workbooks into compensation_observations."
    )
    p.add_argument(
        "directory",
        type=Path,
        help="Path to directory containing ASHE Table 2 .xlsx files.",
    )
    p.add_argument("--dry-run", action="store_true", help="Parse only; skip DB load.")
    p.add_argument(
        "--sheet",
        default="Full-Time",
        help="Sheet to parse (default: Full-Time).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.directory.is_dir():
        print(f"[ons_ashe:run] ERROR: {args.directory} is not a directory.")
        return 1

    observations = parse_directory(args.directory, sheet_name=args.sheet)
    print(f"[ons_ashe:run] Parsed {len(observations)} observations total.")

    if not observations:
        print("[ons_ashe:run] No observations parsed; check file names and sheet.")
        return 1

    if args.dry_run:
        print("[ons_ashe:run] Dry run — skipping database load.")
        # Show a few samples
        for obs in observations[:3]:
            d = obs.to_dict()
            print(f"  soc={d['occupation_code']} pctile={d['percentile']} "
                  f"val={d['value_amount']} annual={d['normalized_annual_amount']} "
                  f"period={d['period']}")
        return 0

    written = load(observations)
    print(f"[ons_ashe:run] Upserted {written} observations into compensation_observations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
