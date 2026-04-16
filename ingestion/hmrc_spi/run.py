"""Orchestrate HMRC SPI ingestion."""
from __future__ import annotations
import argparse
import sys
from datetime import date
from pathlib import Path

from shared.config import load_env
load_env()

from ingestion.hmrc_spi.fetch import fetch_spi_xlsx
from ingestion.hmrc_spi.load import load
from ingestion.hmrc_spi.parse import parse_spi_xlsx, seed_percentiles


def _latest_likely_tax_year() -> int:
    """HMRC publishes SPI ~2-3 years after TY close. Default to TY ending
    2 years before current year as a realistic latest available."""
    today = date.today()
    return today.year - 2 if today.month >= 4 else today.year - 3


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest HMRC Survey of Personal Incomes.")
    p.add_argument(
        "--tax-year-ending",
        type=int,
        default=None,
        help="Tax year ending (e.g. 2022 for TY 2021-22).",
    )
    p.add_argument("--url", default=None)
    p.add_argument("--from-file", type=Path, default=None)
    p.add_argument("--seed-fallback", action="store_true")
    p.add_argument("--seed-only", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    ty = args.tax_year_ending or _latest_likely_tax_year()

    records: list = []

    if args.seed_only:
        records = seed_percentiles(ty)
        print(f"[hmrc_spi:run] Seed-only: {len(records)} percentiles for TY{ty}.")
    else:
        try:
            path = args.from_file or fetch_spi_xlsx(ty, url=args.url)
            records = parse_spi_xlsx(path, ty)
            print(f"[hmrc_spi:run] Parsed {len(records)} percentiles from {path}.")
        except (ValueError, FileNotFoundError) as e:
            print(f"[hmrc_spi:run] Fetch/parse failed: {e}")
            if args.seed_fallback:
                records = seed_percentiles(ty)
                print(
                    f"[hmrc_spi:run] Falling back to seeded percentiles: {len(records)}."
                )
            else:
                print("[hmrc_spi:run] Pass --seed-fallback to use baseline.")
                return 1

        if not records and args.seed_fallback:
            records = seed_percentiles(ty)
            print(
                f"[hmrc_spi:run] Parser produced 0 rows; seed fallback ({len(records)})."
            )

    if not records:
        print("[hmrc_spi:run] No records to load.")
        return 1

    if args.dry_run:
        print("[hmrc_spi:run] Sample:", records[0].to_dict())
        return 0

    written = load(records)
    print(f"[hmrc_spi:run] Upserted {written} observations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
