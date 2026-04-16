"""Load parsed ONS/HMRC PAYE RTI observations into Postgres."""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    valid = [o for o in observations if _is_valid(o)]
    return bulk_upsert(valid)


def _is_valid(obs: CompensationObservation) -> bool:
    if obs.value_amount is None:
        return False
    # Monthly median pay below £500 or above £50k is almost certainly a
    # misparse (e.g. pulled a count of employees by accident).
    if obs.value_amount < 500 or obs.value_amount > 50_000:
        return False
    return True
