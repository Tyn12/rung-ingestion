"""Refresh runner for dashboard analytics.

Uses bulk pre-computation: ~10 SQL queries to pre-compute ALL percentiles,
trends, distributions, and metadata, then assembles ~16K payloads in Python
and batch-upserts them with execute_values.

Previous approach: per-profile loop with ~20 queries each = ~300K queries.
New approach: ~10 bulk queries + Python assembly + batch upsert = minutes.

Usage:
    # Refresh all profile combinations:
    python -m ingestion.analytics.refresh

    # Refresh a single occupation:
    python -m ingestion.analytics.refresh --occupation 2136

    # Dry-run (compute but don't write):
    python -m ingestion.analytics.refresh --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from shared.config import load_env
from shared.db import get_conn

from .compute import (
    PCTILE_POINTS,
    ProfileKey,
)
from .bulk_compute import (
    PrecomputedData,
    assemble_payload,
    bulk_precompute,
)

logger = logging.getLogger(__name__)

# How many rows per INSERT ... VALUES batch
DEFAULT_BATCH_SIZE = 500


# -------------------------------------------------------------------
# Profile key discovery (unchanged from original)
# -------------------------------------------------------------------

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

    # Generate _all aggregate keys so fallback queries work
    seen = {
        (k.occupation_code, k.location_code, k.sector, k.experience_band)
        for k in keys
    }
    extra_keys: list[ProfileKey] = []
    occupation_codes = {k.occupation_code for k in keys}
    location_codes = {k.location_code for k in keys}

    for occ in occupation_codes:
        for loc in location_codes:
            combo = (occ, loc, "_all", "_all")
            if combo not in seen:
                extra_keys.append(ProfileKey(*combo))
                seen.add(combo)

        combo = (occ, "_all", "_all", "_all")
        if combo not in seen:
            extra_keys.append(ProfileKey(*combo))
            seen.add(combo)

    if extra_keys:
        logger.info(
            "Adding %d _all aggregate keys for fallback coverage",
            len(extra_keys),
        )
        keys.extend(extra_keys)

    logger.info("Discovered %d profile combinations to compute", len(keys))
    return keys


# -------------------------------------------------------------------
# Scoring helpers (unchanged from original)
# -------------------------------------------------------------------

def compute_sample_size(analytics: dict) -> int:
    """Extract total sample size from a computed analytics payload."""
    market = analytics.get("market", {})
    regional = market.get("regional_percentiles", {})
    return regional.get("sample_size", 0)


def compute_confidence(analytics: dict) -> float:
    """Derive a confidence score (0.0-1.0) from the analytics payload."""
    market = analytics.get("market", {})
    regional = market.get("regional_percentiles", {})
    sample = regional.get("sample_size", 0)
    filled = sum(
        1 for p in PCTILE_POINTS if regional.get(f"p{p}") is not None
    )
    sources = len(analytics.get("metadata", {}).get("sources_used", []))

    pctile_score = filled / len(PCTILE_POINTS)
    sample_score = min(1.0, sample / 500)
    source_score = min(1.0, sources / 3)
    return round(
        pctile_score * 0.4 + sample_score * 0.4 + source_score * 0.2, 2
    )


# -------------------------------------------------------------------
# Bulk refresh pipeline
# -------------------------------------------------------------------

def refresh(
    occupation_code: Optional[str] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> dict:
    """Run the full refresh cycle using bulk pre-computation.

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
        # ---------------------------------------------------------------
        # Phase 1: Discover profile keys
        # ---------------------------------------------------------------
        keys = discover_profile_keys(conn, occupation_code)
        stats["discovered"] = len(keys)

        # ---------------------------------------------------------------
        # Phase 2: Bulk pre-compute (~10 SQL queries)
        # ---------------------------------------------------------------
        logger.info("Phase 1: Bulk pre-computation...")
        t_pre = time.monotonic()
        precomputed = bulk_precompute(conn)
        logger.info(
            "Pre-computation took %.1fs", time.monotonic() - t_pre
        )

        # ---------------------------------------------------------------
        # Phase 3: Assemble payloads (pure Python, no SQL)
        # ---------------------------------------------------------------
        logger.info(
            "Phase 2: Assembling %d payloads...", len(keys)
        )
        t_assemble = time.monotonic()

        upsert_rows: list[tuple] = []
        for i, key in enumerate(keys):
            try:
                analytics = assemble_payload(precomputed, key)
                sample = compute_sample_size(analytics)

                if sample == 0:
                    stats["skipped"] += 1
                    continue

                stats["computed"] += 1
                confidence = compute_confidence(analytics)

                # Extract freshness date from metadata
                meta = analytics.get("metadata", {})
                freshness_date = None
                if meta.get("data_window_end"):
                    try:
                        freshness_date = datetime.fromisoformat(
                            meta["data_window_end"].replace("Z", "+00:00")
                        ).date()
                    except (ValueError, AttributeError):
                        pass

                if not dry_run:
                    upsert_rows.append((
                        key.occupation_code,
                        key.location_code,
                        key.sector,
                        key.experience_band,
                        psycopg2.extras.Json(analytics),
                        sample,
                        confidence,
                        freshness_date,
                    ))

            except Exception:
                label = (
                    f"{key.occupation_code}/{key.location_code}"
                    f"/{key.sector}/{key.experience_band}"
                )
                logger.exception(
                    "[%d/%d] FAIL %s", i + 1, len(keys), label
                )
                stats["failed"] += 1

        logger.info(
            "Assembly took %.1fs — computed=%d, skipped=%d, failed=%d",
            time.monotonic() - t_assemble,
            stats["computed"],
            stats["skipped"],
            stats["failed"],
        )

        # ---------------------------------------------------------------
        # Phase 4: Batch upsert (execute_values)
        # ---------------------------------------------------------------
        if not dry_run and upsert_rows:
            logger.info(
                "Phase 3: Batch upserting %d rows...", len(upsert_rows)
            )
            t_upsert = time.monotonic()

            for i in range(0, len(upsert_rows), batch_size):
                batch = upsert_rows[i : i + batch_size]
                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        """
                        INSERT INTO dashboard_analytics (
                            occupation_code, location_code, sector,
                            experience_band, analytics, sample_size,
                            confidence, data_freshness_date, computed_at
                        ) VALUES %s
                        ON CONFLICT (
                            occupation_code, location_code, sector,
                            experience_band
                        )
                        DO UPDATE SET
                            analytics           = EXCLUDED.analytics,
                            sample_size         = EXCLUDED.sample_size,
                            confidence          = EXCLUDED.confidence,
                            data_freshness_date = EXCLUDED.data_freshness_date,
                            computed_at         = NOW()
                        """,
                        batch,
                        template="(%s, %s, %s, %s, %s, %s, %s, %s, NOW())",
                    )
                conn.commit()
                stats["upserted"] += len(batch)
                logger.info(
                    "  Upserted batch %d–%d of %d",
                    i + 1,
                    min(i + batch_size, len(upsert_rows)),
                    len(upsert_rows),
                )

            logger.info(
                "Upsert took %.1fs", time.monotonic() - t_upsert
            )

    stats["elapsed_s"] = round(time.monotonic() - t_start, 1)
    return stats


# -------------------------------------------------------------------
# CLI entry point
# -------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    load_env()

    parser = argparse.ArgumentParser(
        description="Refresh dashboard analytics cache (bulk mode)",
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
        help=f"Rows per upsert batch (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute analytics but don't write to the database",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info(
        "Starting dashboard analytics refresh (BULK mode)%s",
        " (DRY RUN)" if args.dry_run else "",
    )
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
