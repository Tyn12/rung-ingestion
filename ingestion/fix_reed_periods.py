"""Fix Reed listings where daily rates were misclassified as hourly.

The original period detection heuristic used a threshold of £200 to separate
hourly from daily rates.  This caused daily rates in the £50-199 range (common
for supply teachers, agency nurses, trades) to be normalised as hourly:

    £100/day * 1950 hours/year = £195,000   (WRONG — 8.9x inflation)
    £100/day * 220 days/year   = £22,000    (CORRECT)

This script:
  1. Finds all Reed rows where the original minimumSalary/maximumSalary was
     in the £50-199 range AND the normalized_annual_amount suggests hourly
     normalisation was applied (value * 1950).
  2. Recomputes normalized_annual_amount using daily normalisation (value * 220).
  3. Updates the rows in the database.

Usage:
    python -m ingestion.fix_reed_periods --dry-run    # preview changes
    python -m ingestion.fix_reed_periods               # apply fixes
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

import psycopg2
import psycopg2.extras

from shared.config import load_env
from shared.db import get_conn
from shared.normalization import (
    DAILY_TO_ANNUAL_DAYS,
    HOURLY_TO_ANNUAL_HOURS,
    WEEKLY_TO_ANNUAL_WEEKS,
)


# The old threshold was < 200 → hourly.  The new threshold is < 50 → hourly.
# So values in [50, 200) were misclassified.  But some listings have a range
# (min ≠ max), so we check the raw values from source_payload.
OLD_HOURLY_CEILING = 200
NEW_HOURLY_CEILING = 50


def _detect_period_new(value: float) -> str:
    """New period detection matching the fixed reed/parse.py."""
    if value < 50:
        return "hourly"
    if value < 2000:
        return "daily"
    if value < 12000:
        return "weekly"
    return "annual"


def _normalise(value: float, period: str) -> float:
    if period == "hourly":
        return value * HOURLY_TO_ANNUAL_HOURS
    if period == "daily":
        return value * DAILY_TO_ANNUAL_DAYS
    if period == "weekly":
        return value * WEEKLY_TO_ANNUAL_WEEKS
    return value  # annual


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fix misclassified Reed daily rates.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    load_env()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Find all Reed rows — we check the raw salary from source_payload
    print("[fix_periods] Fetching Reed rows...")
    cur.execute("""
        SELECT id, observed_year, source_payload, normalized_annual_amount,
               value_amount, value_min, value_max, period
        FROM compensation_observations
        WHERE source_id = 'reed_jobseeker'
          AND normalized_annual_amount IS NOT NULL
          AND normalized_annual_amount > 0
    """)
    rows = cur.fetchall()
    print(f"[fix_periods] Found {len(rows)} Reed rows total.")

    fixes = []
    stats = Counter()

    for row in rows:
        payload = row["source_payload"] or {}
        min_s = payload.get("minimumSalary")
        max_s = payload.get("maximumSalary")
        ref = min_s or max_s
        if ref is None:
            continue
        ref = float(ref)

        # Only fix rows where ref was in the misclassified range [50, 200)
        if ref < NEW_HOURLY_CEILING or ref >= OLD_HOURLY_CEILING:
            stats["unchanged_outside_range"] += 1
            continue

        # This ref value was treated as hourly (old heuristic) but should be daily
        old_period = "hourly"
        new_period = _detect_period_new(ref)

        if new_period == old_period:
            stats["unchanged_same_period"] += 1
            continue

        # Recompute normalised annual amount
        if min_s and max_s and min_s != max_s:
            midpoint = (float(min_s) + float(max_s)) / 2
            new_normalised = _normalise(midpoint, new_period)
        else:
            new_normalised = _normalise(ref, new_period)

        old_normalised = row["normalized_annual_amount"]

        # Sanity check: the old value should be ~8.9x the new value
        # (1950/220 = 8.86x)
        if old_normalised > 0 and abs(old_normalised / new_normalised - HOURLY_TO_ANNUAL_HOURS / DAILY_TO_ANNUAL_DAYS) > 1.0:
            stats["skipped_unexpected_ratio"] += 1
            if args.verbose:
                ratio = old_normalised / new_normalised if new_normalised else 0
                print(f"  SKIP id={row['id']} ref={ref} old={old_normalised:.0f} new={new_normalised:.0f} ratio={ratio:.1f}")
            continue

        title = payload.get("jobTitle", "")
        fixes.append({
            "id": row["id"],
            "observed_year": row["observed_year"],
            "new_normalised": round(new_normalised, 2),
            "new_period": new_period,
            "old_normalised": old_normalised,
            "ref": ref,
            "title": title,
        })
        stats["to_fix"] += 1

    print(f"\n[fix_periods] Results:")
    print(f"  To fix (daily rates misclassified as hourly): {stats['to_fix']}")
    print(f"  Unchanged (outside £50-199 range): {stats['unchanged_outside_range']}")
    print(f"  Unchanged (same period after recheck): {stats['unchanged_same_period']}")
    print(f"  Skipped (unexpected ratio): {stats['skipped_unexpected_ratio']}")

    if not fixes:
        print("[fix_periods] Nothing to fix.")
        return 0

    # Show sample fixes
    print(f"\n[fix_periods] Sample fixes (first 20):")
    for f in fixes[:20]:
        print(
            f"  {f['title'][:45]:45} ref=£{f['ref']:.0f}"
            f"  old=£{f['old_normalised']:>10,.0f}"
            f"  new=£{f['new_normalised']:>8,.0f}"
            f"  ({f['new_period']})"
        )

    # Show SOC code distribution of affected rows (if occupation_code is available)
    if fixes:
        print(f"\n[fix_periods] Affected job titles (top 20):")
        title_counts = Counter(f["title"] for f in fixes)
        for title, count in title_counts.most_common(20):
            print(f"  {count:>4}x  {title[:60]}")

    if args.dry_run:
        print(f"\n[fix_periods] Dry run — no database changes.")
        return 0

    # Apply fixes in batches
    print(f"\n[fix_periods] Applying {len(fixes)} fixes...")
    batch_size = 500
    applied = 0

    for i in range(0, len(fixes), batch_size):
        batch = fixes[i:i + batch_size]
        for f in batch:
            cur.execute("""
                UPDATE compensation_observations
                SET normalized_annual_amount = %s,
                    period = %s
                WHERE id = %s AND observed_year = %s
            """, (f["new_normalised"], f["new_period"], f["id"], f["observed_year"]))
            applied += 1
        conn.commit()
        print(f"  Committed batch {i // batch_size + 1} ({applied}/{len(fixes)})")

    print(f"\n[fix_periods] Done. Fixed {applied} rows.")
    print("[fix_periods] NEXT STEP: Re-run the analytics refresh:")
    print("  python -m ingestion.analytics.refresh")

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
