"""Load London Datastore observations."""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    return bulk_upsert([o for o in observations if _is_valid(o)])


def _is_valid(obs: CompensationObservation) -> bool:
    """Validate before upsert.

    Partitions only exist for 2024+, so reject older observations.
    Use normalized_annual_amount for plausibility check since value_amount
    varies by period (weekly £300-£2000, annual £15k-£110k).
    """
    if obs.observed_at is not None and obs.observed_at.year < 2024:
        return False
    if obs.value_amount is None and obs.value_min is None:
        return False
    if obs.normalized_annual_amount is not None:
        if obs.normalized_annual_amount < 5_000 or obs.normalized_annual_amount > 500_000:
            return False
    return True
