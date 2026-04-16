"""End-to-end orchestrator for the Nomis ingestion pipeline.

Usage:
    python -m ingestion.nomis.run                  # latest year, primary dataset
    python -m ingestion.nomis.run --years 2022 2023 2024
    python -m ingestion.nomis.run --dataset NM_30_1
    python -m ingestion.nomis.run --dry-run        # fetch + parse but don't write DB
    python -m ingestion.nomis.run --from-file path/to/saved.csv --dataset NM_99_1

Called by the GitHub Actions workflow on a yearly schedule (post-ASHE release)
and manually on demand.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from shared.config import load_env
load_env()

from ingestion.nomis.fetch import (
    LEGACY_DATASET,
    PRIMARY_DATASET,
    fetch_data,
    fetch_metadata,
)
from ingestion.nomis.load import load
from ingestion.nomis.parse import parse_file


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the Nomis ASHE ingestion pipeline.")
    p.add_argument(
        "--dataset",
        default=PRIMARY_DATASET,
        choices=[PRIMARY_DATASET, LEGACY_DATASET],
        help="Nomis dataset ID. Defaults to SOC 2020 primary.",
    )
    p.add_argument(
        "--years",
        nargs="*",
        type=int,
        help="One or more years to fetch. Omit for 'latest'.",
    )
    p.add_argument(
        "--occupations",
        nargs="*",
        default=None,
        help="Specific SOC codes to fetch. Omit for all SOC groupings.",
    )
    p.add_argument(
        "--skip-metadata",
        action="store_true",
        help="Skip the metadata refresh step (faster for testing).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + parse but don't write to the database.",
    )
    p.add_argument(
        "--from-file",
        type=Path,
        default=None,
        help="Skip fetch; parse this previously downloaded CSV file instead.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dataset_id = args.dataset

    if args.from_file:
        path = args.from_file
        print(f"[nomis:run] Using cached file {path}")
    else:
        if not args.skip_metadata:
            print(f"[nomis:run] Refreshing metadata for {dataset_id}...")
            fetch_metadata(dataset_id)
        path = fetch_data(
            dataset_id=dataset_id,
            years=args.years,
            occupations=args.occupations,
        )

    observations = parse_file(path, dataset_id)
    print(f"[nomis:run] Parsed {len(observations)} candidate observations.")

    if args.dry_run:
        print("[nomis:run] Dry run — skipping database load.")
        if observations:
            print("[nomis:run] Sample:", observations[0].to_dict())
        return 0

    written = load(observations)
    print(f"[nomis:run] Upserted {written} observations into compensation_observations.")
    if written == 0 and not args.dry_run:
        print("[nomis:run] WARNING: 0 observations written — check API parameters.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
