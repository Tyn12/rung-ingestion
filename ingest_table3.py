"""One-shot script to ingest ASHE Table 3 data into Supabase.

Usage:
    cd rung-ingestion
    python ingest_table3.py

This will:
1. Parse all Table 3 workbooks from ../ashetable32025provisional/
2. Load them into compensation_observations via bulk_upsert
"""
import sys
from pathlib import Path

# Ensure shared modules are importable
sys.path.insert(0, str(Path(__file__).parent))

from shared.config import load_env
load_env()

from ingestion.ons_ashe.parse_table3 import parse_directory
from shared.db import bulk_upsert

DATA_DIR = Path(__file__).parent.parent / "ashetable32025provisional"


def main():
    if not DATA_DIR.exists():
        print(f"ERROR: Data directory not found: {DATA_DIR}")
        print(f"Expected ASHE Table 3 files in: {DATA_DIR.resolve()}")
        sys.exit(1)

    print(f"Parsing ASHE Table 3 files from: {DATA_DIR}")
    observations = parse_directory(DATA_DIR)

    if not observations:
        print("No observations parsed. Check the data directory.")
        sys.exit(1)

    both = sum(1 for o in observations if o.occupation_code and o.location_code)
    print(f"\nParsed {len(observations)} total observations")
    print(f"  With BOTH occupation + region: {both}")
    print(f"\nLoading into database...")

    count = bulk_upsert(observations)
    print(f"Done! Upserted {count} rows into compensation_observations.")


if __name__ == "__main__":
    main()
