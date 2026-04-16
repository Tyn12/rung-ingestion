"""Orchestrate London Datastore earnings ingestion."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from shared.config import load_env
load_env()

from ingestion.london_datastore.fetch import LONDON_DATASETS, fetch_latest_resource
from ingestion.london_datastore.load import load
from ingestion.london_datastore.parse import parse_earnings_file


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest London Datastore earnings.")
    p.add_argument(
        "--datasets",
        nargs="+",
        default=[d.slug for d in LONDON_DATASETS],
        help="Subset of CKAN slugs to fetch (default: all).",
    )
    p.add_argument("--from-file", type=Path, default=None)
    p.add_argument("--single-dataset", default=None,
                   help="Required when --from-file; matches LondonDataset.slug.")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    total = 0
    slugs = set(args.datasets)

    if args.from_file:
        if not args.single_dataset:
            print("[london_datastore:run] --from-file requires --single-dataset")
            return 2
        ds = next((d for d in LONDON_DATASETS if d.slug == args.single_dataset), None)
        if ds is None:
            print(f"[london_datastore:run] unknown dataset: {args.single_dataset}")
            return 2
        records = parse_earnings_file(args.from_file, dataset_code=ds.code, axis=ds.axis)
        print(f"[london_datastore:run] {ds.code}: parsed {len(records)} rows.")
        if args.dry_run:
            if records:
                print("[london_datastore:run] Sample:", records[0].to_dict())
            return 0
        return 0 if load(records) else 1

    any_success = False
    for ds in LONDON_DATASETS:
        if ds.slug not in slugs:
            continue
        try:
            path = fetch_latest_resource(ds)
        except Exception as e:  # noqa: BLE001
            print(f"[london_datastore:run] {ds.code} fetch failed: {e}")
            continue
        try:
            records = parse_earnings_file(path, dataset_code=ds.code, axis=ds.axis)
        except Exception as e:  # noqa: BLE001
            print(f"[london_datastore:run] {ds.code} parse failed: {e}")
            continue
        print(f"[london_datastore:run] {ds.code}: parsed {len(records)} rows.")

        if args.dry_run:
            if records:
                print(f"[london_datastore:run] {ds.code} sample:", records[0].to_dict())
            any_success = True
            continue

        total += load(records)
        any_success = True

    if args.dry_run:
        return 0 if any_success else 1
    print(f"[london_datastore:run] Upserted {total} observations.")
    return 0 if any_success else 1


if __name__ == "__main__":
    sys.exit(main())
