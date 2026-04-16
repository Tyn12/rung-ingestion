"""Load parsed Reed observations into Postgres."""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    """Validate then bulk-upsert. Returns the count written."""
    valid = [o for o in observations if _is_valid(o)]
    return bulk_upsert(valid)


def _is_valid(obs: CompensationObservation) -> bool:
    """Reed-specific quality gates."""
    if obs.normalized_annual_amount is None:
        return False
    # Implausibly low/high — catches hourly values we mis-classified, and
    # the occasional "£100,000,000 CEO" joke listing.
    if obs.normalized_annual_amount < 5_000:
        return False
    if obs.normalized_annual_amount > 5_000_000:
        return False
    # Only keep listings from years we have partitions for (2024+).
    # Reed search results occasionally include stale listings from years ago.
    if obs.observed_at is None:
        return False
    if obs.observed_at.year < 2024:
        return False
    return True
