"""Backfill SOC 2020 codes onto existing Reed observations in the database.

Uses the keyword-based classifier (shared.soc_classifier) to map job titles
to SOC codes, with a salary sanity guard: if a listing's normalized annual
salary is more than 15% below the ASHE P10 or 15% above the ASHE P90 for
that SOC code, the classification is rejected. This prevents misclassified
low/high-paying roles from polluting the distribution.

Usage:
    python -m ingestion.reed.backfill_soc                    # live update
    python -m ingestion.reed.backfill_soc --dry-run          # preview only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from shared.config import load_env
load_env()

import psycopg2
import psycopg2.extras
import os

from ingestion.ons_ashe.parse import parse_workbook
from shared.soc_classifier import classify_title

# ── Salary guard: 15% tolerance beyond ASHE P10/P90 ──────────────

TOLERANCE = 0.15  # 15%

def _load_ashe_bounds() -> dict[str, tuple[float, float]]:
    """Load P10/P90 annual salary bounds per SOC code from ASHE Table 2.

    Returns dict mapping soc_code → (floor, ceiling) where:
        floor   = P10 * (1 - TOLERANCE)
        ceiling = P90 * (1 + TOLERANCE)
    """
    ashe_dir = Path("ONS occupation/ashetable22025provisional")
    annual_file = None
    for f in ashe_dir.glob("*.xlsx"):
        if "Table 2.7a" in f.name and "Annual pay" in f.name:
            annual_file = f
            break

    if annual_file is None:
        print("[backfill_soc] WARNING: ASHE annual pay file not found. "
              "Salary guard disabled.")
        return {}

    obs = parse_workbook(annual_file)

    # Collect P10 and P90 per SOC code
    raw: dict[str, dict[str, float]] = {}
    for o in obs:
        soc = o.occupation_code
        if soc is None:
            continue
        if soc not in raw:
            raw[soc] = {}
        if o.percentile == 10:
            raw[soc]["p10"] = o.value_amount
        elif o.percentile == 90:
            raw[soc]["p90"] = o.value_amount

    bounds: dict[str, tuple[float, float]] = {}
    for soc, vals in raw.items():
        p10 = vals.get("p10")
        p90 = vals.get("p90")
        if p10 is not None and p90 is not None:
            bounds[soc] = (p10 * (1 - TOLERANCE), p90 * (1 + TOLERANCE))

    # Also populate 1-digit major group bounds from 2-digit ranges
    # e.g. SOC "1" gets the min P10 floor and max P90 ceiling of SOC 11, 12
    for soc, (floor, ceil) in list(bounds.items()):
        if len(soc) == 2:
            major = soc[0]
            if major in bounds:
                existing = bounds[major]
                bounds[major] = (min(existing[0], floor), max(existing[1], ceil))
            else:
                bounds[major] = (floor, ceil)

    return bounds


def _salary_in_range(
    normalized_annual: float | None,
    soc_code: str,
    bounds: dict[str, tuple[float, float]],
) -> bool:
    """Check if a salary falls within the ±15% ASHE range for a SOC code."""
    if normalized_annual is None:
        return True  # No salary to check — allow classification
    if soc_code not in bounds:
        return True  # No ASHE data for this code — allow
    floor, ceiling = bounds[soc_code]
    return floor <= normalized_annual <= ceiling


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill SOC codes onto Reed observations."
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only.")
    args = parser.parse_args(argv)

    print("[backfill_soc] Loading ASHE salary bounds...")
    bounds = _load_ashe_bounds()
    print(f"[backfill_soc] Loaded bounds for {len(bounds)} SOC codes.")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Fetch all Reed observations without an occupation code
    cur.execute("""
        SELECT id, source_reference, occupation_code, normalized_annual_amount,
               source_payload
        FROM compensation_observations
        WHERE source_id = 'reed_jobseeker'
          AND (occupation_code IS NULL OR occupation_code = '')
    """)
    rows = cur.fetchall()
    print(f"[backfill_soc] Found {len(rows)} Reed observations without SOC code.")

    classified = 0
    rejected_salary = 0
    unclassified = 0
    updates: list[tuple[str, int]] = []  # (soc_code, id)

    for row in rows:
        payload = row["source_payload"] or {}
        title = payload.get("jobTitle", "")
        soc_code = classify_title(title)

        if soc_code is None:
            unclassified += 1
            continue

        annual = row["normalized_annual_amount"]
        if not _salary_in_range(annual, soc_code, bounds):
            rejected_salary += 1
            continue

        classified += 1
        updates.append((soc_code, row["id"]))

    total = len(rows)
    print(f"\n[backfill_soc] Results:")
    print(f"  Classified:        {classified:,d} ({classified/total*100:.1f}%)")
    print(f"  Rejected (salary): {rejected_salary:,d} ({rejected_salary/total*100:.1f}%)")
    print(f"  Unclassified:      {unclassified:,d} ({unclassified/total*100:.1f}%)")

    if args.dry_run:
        print("\n[backfill_soc] Dry run — no database changes.")
        # Show some samples
        from collections import Counter
        soc_dist = Counter(soc for soc, _ in updates)
        print(f"\n  SOC distribution (top 15):")
        for soc, cnt in soc_dist.most_common(15):
            print(f"    SOC {soc}: {cnt:,d}")
        cur.close()
        conn.close()
        return 0

    # Batch UPDATE
    print(f"\n[backfill_soc] Updating {len(updates):,d} rows...")
    psycopg2.extras.execute_batch(
        cur,
        "UPDATE compensation_observations SET occupation_code = %s WHERE id = %s",
        updates,
        page_size=1000,
    )
    conn.commit()
    print(f"[backfill_soc] Done. Updated {len(updates):,d} rows.")

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
