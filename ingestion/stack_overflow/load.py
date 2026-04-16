"""Load Stack Overflow survey observations."""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    return bulk_upsert([o for o in observations if _is_valid(o)])


def _is_valid(obs: CompensationObservation) -> bool:
    return (
        obs.normalized_annual_amount is not None
        and 8_000 <= obs.normalized_annual_amount <= 1_000_000
    )
