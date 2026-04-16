"""Orchestrate Local Government Transparency Code ingestion.

Designed to iterate the COUNCIL_REGISTRY and pull whatever has a URL set.
For manual fetches (before a URL is registered) pass --council + --url.
"""
from __future__ import annotations
import argparse
import sys
from datetime import date
from pathlib import Path

from shared.config import load_env
load_env()

from ingestion.local_gov.fetch import COUNCIL_REGISTRY, fetch_council_csv
from ingestion.local_gov.load import load
from ingestion.local_gov.parse import parse_senior_salaries_csv


def _default_year() -> int:
    # Councils publish around May-July for the financial year just ended.
    today = date.today()
    return today.year if today.month >= 8 else today.year - 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest Local Government Transparency senior salaries.")
    p.add_argument(
        "--councils",
        nargs="+",
        default=None,
        help="Subset of council codes (default: everything registered with a URL).",
    )
    p.add_argument("--council-url", default=None,
                   help="Override URL for the specified --councils (single council mode).")
    p.add_argument("--year", type=int, default=None,
                   help="Financial year ending (default: most recent).")
    p.add_argument("--from-file", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    year = args.year or _default_year()
    wanted = set(args.councils) if args.councils else None
    total = 0

    if args.from_file:
        if not args.councils or len(args.councils) != 1:
            print("[local_gov:run] --from-file requires exactly one --councils code")
            return 2
        council = next((c for c in COUNCIL_REGISTRY if c.code == args.councils[0]), None)
        if council is None:
            print(f"[local_gov:run] unknown council: {args.councils[0]}")
            return 2
        records = parse_senior_salaries_csv(
            args.from_file,
            council_code=council.code,
            council_name=council.name,
            gss_code=council.gss_code,
            observed_year=year,
        )
        print(f"[local_gov:run] {council.code}: parsed {len(records)} rows.")
        if args.dry_run:
            if records:
                print("[local_gov:run] Sample:", records[0].to_dict())
            return 0
        return 0 if load(records) else 1

    any_attempt = False
    for council in COUNCIL_REGISTRY:
        if wanted and council.code not in wanted:
            continue
        if council.url is None and not args.council_url:
            continue
        any_attempt = True
        try:
            path = fetch_council_csv(council, url_override=args.council_url)
        except Exception as e:  # noqa: BLE001
            print(f"[local_gov:run] {council.code} fetch failed: {e}")
            continue
        try:
            records = parse_senior_salaries_csv(
                path,
                council_code=council.code,
                council_name=council.name,
                gss_code=council.gss_code,
                observed_year=year,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[local_gov:run] {council.code} parse failed: {e}")
            continue
        print(f"[local_gov:run] {council.code}: parsed {len(records)} rows.")

        if args.dry_run:
            if records:
                print(f"[local_gov:run] {council.code} sample:", records[0].to_dict())
            continue

        total += load(records)

    if not any_attempt:
        print(
            "[local_gov:run] No councils have URLs registered. "
            "Populate COUNCIL_REGISTRY or run with --council-url + --councils <code>."
        )
        # Not an error: empty-but-healthy is fine for scheduled runs.
        return 0

    if args.dry_run:
        return 0
    print(f"[local_gov:run] Upserted {total} observations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
