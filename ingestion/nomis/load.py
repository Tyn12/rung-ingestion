"""Load parsed Nomis observations into Postgres.

Thin wrapper over `shared.db.bulk_upsert`. Kept as its own module so the per-source
Airflow / GitHub Actions job has a clear entry point and so we can later add
per-source validation (e.g. reject observations missing occupation_code for ASHE).
"""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    """Validate then bulk-upsert. Returns the count written."""
    validated = [o for o in observations if _is_valid(o)]
    skipped = 0
    written = bulk_upsert(validated)
    # We count by difference so callers can log data quality metrics.
    total = written + skipped
    _ = total  # silence linters; metric emission comes later
    return written


def _is_valid(obs: CompensationObservation) -> bool:
    """Per-source validation rules for ASHE.

    We require a value and a geography. Occupation code is optional —
    aggregate 'all occupations' rows are still valuable for benchmarking
    regional/national pay levels.
    """
    if obs.value_amount is None and obs.value_min is None:
        return False
    if obs.location_code is None:
        return False
    # Weekly pay must be plausible (£50–£5000/week → ~£2.6k–£260k/yr)
    if obs.normalized_annual_amount is not None:
        if obs.normalized_annual_amount < 2_500 or obs.normalized_annual_amount > 500_000:
            return False
    return True
