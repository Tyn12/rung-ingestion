"""Run the full analytics refresh after data ingestion.

Usage:
    cd rung-ingestion
    python run_full_refresh.py

This is equivalent to:
    python -m ingestion.analytics.refresh
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from shared.config import load_env
load_env()

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)

from ingestion.analytics.refresh import refresh

logger = logging.getLogger(__name__)

def main():
    logger.info("Starting full analytics refresh...")
    stats = refresh()
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
        logger.error("Some profiles failed — check logs above.")
        sys.exit(1)
    else:
        logger.info("All profiles computed successfully!")


if __name__ == "__main__":
    main()
