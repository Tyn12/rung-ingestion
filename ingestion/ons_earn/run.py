"""Orchestrate ONS EARN01/02/03 ingestion."""
from __future__ import annotations
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from shared.config import load_env
load_env()

from ingestion.ons_earn.fetch import EARN_DATASETS, fetch_earn_xlsx
from ingestion.ons_earn.load import load
from ingestion.ons_earn.parse import parse_earn_file


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest ONS Average Weekly Earnings datasets.")
    p.add_argument(
        "--datasets",
        nargs="+",
        default=[d.code for d in EARN_DATASETS],
        help="Subset of dataset codes to fetch (default: all three).",
    )
    p.add_argument(
        "--since-years",
        type=int,
        default=2,
        help="Only keep observations within the last N years (default: 2).",
    )
    p.add_argument("--from-file", type=Path, default=None,
                   help="Parse a local file instead of fetching (requires --single-dataset).")
    p.add_argument("--single-dataset", default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    since = date.today() - timedelta(days=365 * args.since_years)
    codes = {c.upper() for c in args.datasets}
    total = 0

    if args.from_file:
        if not args.single_dataset:
            print("[ons_earn:run] --from-file requires --single-dataset")
            return 2
        ds = next((d for d in EARN_DATASETS if d.code == args.single_dataset.upper()), None)
        if ds is None:
            print(f"[ons_earn:run] unknown dataset: {args.single_dataset}")
            return 2
        records = parse_earn_file(args.from_file, dataset_code=ds.code, axis=ds.axis, since=since)
        print(f"[ons_earn:run] {ds.code}: parsed {len(records)} rows.")
        if args.dry_run:
            if records:
                print("[ons_earn:run] Sample:", records[0].to_dict())
            return 0
        return 0 if load(records) else 1

    for ds in EARN_DATASETS:
        if ds.code not in codes:
            continue
        try:
            path = fetch_earn_xlsx(ds)
        except Exception as e:  # noqa: BLE001 — any fetch failure is soft-skipped
            print(f"[ons_earn:run] {ds.code} fetch failed: {e}")
            continue
        try:
            records = parse_earn_file(path, dataset_code=ds.code, axis=ds.axis, since=since)
        except Exception as e:  # noqa: BLE001
            print(f"[ons_earn:run] {ds.code} parse failed: {e}")
            continue
        print(f"[ons_earn:run] {ds.code}: parsed {len(records)} rows.")

        if args.dry_run:
            if records:
                print(f"[ons_earn:run] {ds.code} sample:", records[0].to_dict())
            continue

        total += load(records)

    if args.dry_run:
        return 0
    print(f"[ons_earn:run] Upserted {total} observations.")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
