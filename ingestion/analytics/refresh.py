"""Refresh runner for dashboard analytics.

CLI entry point that discovers which (occupation, location, sector, band)
combinations have data, computes the analytics payload for each, and
upserts the results into dashboard_analytics.

Usage:
    # Refresh all profile combinations:
    python -m ingestion.analytics.refresh

    # Refresh a single occupation:
    python -m ingestion.analytics.refresh --occupation 2136

    # Dry-run (compute but don't write):
    python -m ingestion.analytics.refresh --dry-run

    # Limit concurrency for large refreshes:
    python -m ingestion.analytics.refresh --batch-size 50
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras

from shared.config import load_env
from shared.db import get_conn

from .compute import ProfileKey, compute_analytics, compute_confidence, compute_sample_size

logger = logging.getLogger(__name__)

# Default batch size for upserts (rows per commit)
DEFAULT_BATCH_SIZE = 100


def discover_profile_keys(
    conn,
    occupation_code: Optional[str] = None,
) -> list[ProfileKey]:
    """Find all (occupation, location, sector, band) combinations with data.

    Queries compensation_observations for distinct combinations that have
    at least some observations.  Returns ProfileKey objects.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        occ_filter = ""
        params: list = []
        if occupation_code:
            occ_filter = "AND occupation_code = %s"
            params.append(occupation_code)

        cur.execute(f"""
            SELECT DISTINCT
                COALESCE(occupation_code, '_all') AS occupation_code,
                COALESCE(location_code, '_all') AS location_code,
                COALESCE(sector, '_all') AS sector,
                COALESCE(experience_band, '_all') AS experience_band
            FROM (
                SELECT
                    occupation_code,
                    location_code,
                    -- Derive sector from source metadata or observation fields
                    CASE
                        WHEN source_id IN (
                            SELECT source_id FROM dim_source
                            WHERE source_id LIKE 'nhs%%'
                        ) THEN 'nhs'
                        WHEN source_id IN (
                            SELECT source_id FROM dim_source
                            WHERE source_id LIKE 'public%%'
                               OR source_id IN ('ashe')
                        ) THEN 'public'
                        ELSE 'private'
                    END AS sector,
                    experience_band
                FROM compensation_observations
                WHERE normalized_annual_amount IS NOT NULL
                  AND normalized_annual_amount > 0
                  {occ_filter}
            ) sub
            ORDER BY occupation_code, location_code, sector, experience_band
        """, params)

        rows = cur.fetchall()

    keys = [
        ProfileKey(
            occupation_code=row["occupation_code"],
            location_code=row["location_code"],
            sector=row["sector"],
            experience_band=row["experience_band"],
        )
        for row in rows
    ]

    logger.info("Discovered %d profile combinations to compute", len(keys))
    return keys


def upsert_analytics(conn, key: ProfileKey, analytics: dict) -> None:
    """Write a computed analytics payload into dashboard_analytics."""
    sample_size = compute_sample_size(analytics)
    confidence = compute_confidence(analytics)

    # Extract data freshness from metadata
    meta = analytics.get("metadata", {})
    freshness_date = None
    if meta.get("data_window_end"):
        try:
            freshness_date = datetime.fromisoformat(
                meta["data_window_end"].replace("Z", "+00:00")
            ).date()
        except (ValueError, AttributeError):
            pass

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO dashboard_analytics (
                occupation_code, location_code, sector, experience_band,
                analytics, sample_size, confidence,
                data_freshness_date, computed_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, NOW()
            )
            ON CONFLICT (occupation_code, location_code, sector, experience_band)
            DO UPDATE SET
                analytics           = EXCLUDED.analytics,
                sample_size         = EXCLUDED.sample_size,
                confidence          = EXCLUDED.confidence,
                data_freshness_date = EXCLUDED.data_freshness_date,
                computed_at         = NOW()
        """, (
            key.occupation_code,
            key.location_code,
            key.sector,
            key.experience_band,
            psycopg2.extras.Json(analytics),
            sample_size,
            confidence,
            freshness_date,
        ))


def refresh(
    occupation_code: Optional[str] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> dict:
    """Run the full refresh cycle.

    Returns a summary dict with timing and counts.
    """
    t_start = time.monotonic()
    stats = {
        "discovered": 0,
        "computed": 0,
        "failed": 0,
        "skipped": 0,
        "upserted": 0,
        "elapsed_s": 0.0,
    }

    with get_conn() as conn:
        keys = discover_profile_keys(conn, occupation_code)
        stats["discovered"] = len(keys)

        batch_count = 0
        for i, key in enumerate(keys, 1):
            label = (
                f"{key.occupation_code}/{key.location_code}"
                f"/{key.sector}/{key.experience_band}"
            )
            try:
                t_key = time.monotonic()
                analytics = compute_analytics(conn, key)
                dt = time.monotonic() - t_key

                sample = compute_sample_size(analytics)
                if sample == 0:
                    logger.debug(
                        "[%d/%d] SKIP %s — zero sample size (%.1fs)",
                        i, len(keys), label, dt,
                    )
                    stats["skipped"] += 1
                    continue

                stats["computed"] += 1
                logger.info(
                    "[%d/%d] OK   %s — n=%d, confidence=%.2f (%.1fs)",
                    i, len(keys), label, sample,
                    compute_confidence(analytics), dt,
                )

                if not dry_run:
                    upsert_analytics(conn, key, analytics)
                    stats["upserted"] += 1
                    batch_count += 1

                    # Commit in batches to keep transactions manageable
                    if batch_count >= batch_size:
                        conn.commit()
                        batch_count = 0
                        logger.info("Committed batch of %d rows", batch_size)

            except Exception:
                logger.exception(
                    "[%d/%d] FAIL %s", i, len(keys), label,
                )
                stats["failed"] += 1
                # Roll back the failed statement but keep going
                conn.rollback()

        # Final commit for any remaining rows
        if not dry_run and batch_count > 0:
            conn.commit()
            logger.info("Committed final batch of %d rows", batch_count)

    stats["elapsed_s"] = round(time.monotonic() - t_start, 1)
    return stats


def main() -> None:
    """CLI entry point."""
    load_env()

    parser = argparse.ArgumentParser(
        description="Refresh dashboard analytics cache",
    )
    parser.add_argument(
        "--occupation",
        type=str,
        default=None,
        help="Refresh only this SOC/ESCO occupation code (default: all)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per commit batch (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute analytics but don't write to the database",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("Starting dashboard analytics refresh%s",
                " (DRY RUN)" if args.dry_run else "")
    if args.occupation:
        logger.info("Filtering to occupation: %s", args.occupation)

    stats = refresh(
        occupation_code=args.occupation,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )

    logger.info(
        "Refresh complete in %.1fs — "
        "discovered=%d, computed=%d, upserted=%d, skipped=%d, failed=%d",
        stats["elapsed_s"],
        stats["discovered"],
        stats["computed"],
        stats["upserted"],
        stats["skipped"],
        stats["failed"],
    )

    if stats["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
