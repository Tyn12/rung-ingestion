"""Load ONS EARN observations."""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    return bulk_upsert([o for o in observations if _is_valid(o)])


def _is_valid(obs: CompensationObservation) -> bool:
    # Weekly earnings realistically £100–£3,000; annual £5k–£150k.
    if obs.value_amount is None or obs.normalized_annual_amount is None:
        return False
    if not (50 <= obs.value_amount <= 5_000):
        return False
    return 5_000 <= obs.normalized_annual_amount <= 250_000
