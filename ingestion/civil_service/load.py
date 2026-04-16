"""Load Civil Service pay band observations."""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    return bulk_upsert([o for o in observations if _is_valid(o)])


def _is_valid(obs: CompensationObservation) -> bool:
    # Bottom of AA ~£19k, top of Perm Sec ~£210k. Clip broadly.
    if obs.value_amount is None:
        return False
    if obs.value_min is not None and obs.value_max is not None:
        if obs.value_min > obs.value_max:
            return False
        if obs.value_min < 15_000 or obs.value_max > 250_000:
            return False
    return 15_000 <= obs.value_amount <= 250_000
