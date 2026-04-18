"""Reclassify Reed job listings using the updated SOC classifier.

Finds all Reed rows in compensation_observations that have NULL occupation_code,
extracts the jobTitle from source_payload JSONB, runs it through the SOC classifier,
and updates the occupation_code for matched rows.

Usage:
    # Dry run (no DB changes, just report):
    python -m ingestion.reclassify_reed --dry-run

    # Live run:
    python -m ingestion.reclassify_reed

    # Verbose (show every classification):
    python -m ingestion.reclassify_reed --dry-run -v
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter

import psycopg2
import psycopg2.extras

from shared.config import load_env
from shared.db import get_conn
from shared.soc_classifier import classify_title_verbose

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def fetch_unclassified_reed_rows(conn) -> list[dict]:
    """Fetch all Reed rows with NULL occupation_code that have a jobTitle."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                id,
                observed_year,
                source_payload->>'jobTitle' AS job_title
            FROM compensation_observations
            WHERE occupation_code IS NULL
              AND source_id = 'reed_jobseeker'
              AND source_payload->>'jobTitle' IS NOT NULL
        """)
        return cur.fetchall()


def reclassify(dry_run: bool = False, verbose: bool = False) -> dict:
    """Run the reclassification pipeline."""
    t_start = time.monotonic()
    stats = {
        "total_unclassified": 0,
        "classified": 0,
        "still_unclassified": 0,
        "updated": 0,
        "elapsed_s": 0.0,
    }
    soc_counts: Counter = Counter()
    still_unclassified_titles: Counter = Counter()

    with get_conn() as conn:
        rows = fetch_unclassified_reed_rows(conn)
        stats["total_unclassified"] = len(rows)
        logger.info("Found %d unclassified Reed rows with jobTitle", len(rows))

        if not rows:
            logger.info("Nothing to reclassify.")
            return stats

        # Classify each row
        updates: list[tuple[str, int, int]] = []  # (soc_code, id, observed_year)
        for row in rows:
            title = row["job_title"]
            soc_code, desc = classify_title_verbose(title)

            if soc_code:
                updates.append((soc_code, row["id"], row["observed_year"]))
                soc_counts[soc_code] += 1
                if verbose:
                    logger.debug("  %-50s -> SOC %s (%s)", title, soc_code, desc)
            else:
                still_unclassified_titles[title] += 1
                if verbose:
                    logger.debug("  %-50s -> NO MATCH", title)

        stats["classified"] = len(updates)
        stats["still_unclassified"] = stats["total_unclassified"] - len(updates)

        # Report SOC distribution
        logger.info("\nClassification results:")
        logger.info("  Classified:       %d / %d (%.1f%%)",
                     stats["classified"], stats["total_unclassified"],
                     100 * stats["classified"] / max(stats["total_unclassified"], 1))
        logger.info("  Still unmatched:  %d", stats["still_unclassified"])
        logger.info("\nSOC code distribution:")
        for code, count in soc_counts.most_common():
            logger.info("  SOC %-4s: %5d rows", code, count)

        if still_unclassified_titles:
            logger.info("\nTop 30 still-unclassified titles:")
            for title, count in still_unclassified_titles.most_common(30):
                logger.info("  %4d x  %s", count, title)

        # Apply updates
        if not dry_run and updates:
            logger.info("\nApplying %d updates to database...", len(updates))
            with conn.cursor() as cur:
                for i in range(0, len(updates), BATCH_SIZE):
                    batch = updates[i:i + BATCH_SIZE]
                    psycopg2.extras.execute_batch(
                        cur,
                        "UPDATE compensation_observations SET occupation_code = %s WHERE id = %s AND observed_year = %s",
                        batch,
                    )
                    conn.commit()
                    done = min(i + BATCH_SIZE, len(updates))
                    logger.info("  Committed batch: %d / %d", done, len(updates))
            stats["updated"] = len(updates)
            logger.info("All updates committed.")
        elif dry_run:
            logger.info("\nDRY RUN — no database changes made.")

    stats["elapsed_s"] = round(time.monotonic() - t_start, 1)
    return stats


def main() -> None:
    load_env()

    parser = argparse.ArgumentParser(
        description="Reclassify Reed listings using updated SOC classifier",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report classifications without writing to the database",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show every title classification",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("Starting Reed reclassification%s", " (DRY RUN)" if args.dry_run else "")

    stats = reclassify(dry_run=args.dry_run, verbose=args.verbose)

    logger.info(
        "\nDone in %.1fs — total=%d, classified=%d, updated=%d, still_unclassified=%d",
        stats["elapsed_s"],
        stats["total_unclassified"],
        stats["classified"],
        stats["updated"],
        stats["still_unclassified"],
    )


if __name__ == "__main__":
    main()
