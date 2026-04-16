"""Load Local Government Transparency Code observations."""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    return bulk_upsert([o for o in observations if _is_valid(o)])


def _is_valid(obs: CompensationObservation) -> bool:
    # The transparency code targets officers earning >£50k, but we keep the
    # floor lower to catch mid-senior posts that some councils disclose too.
    # Top end is conservatively £400k (covers chief executives at the largest
    # councils plus pension top-ups).
    if obs.value_amount is None:
        return False
    if obs.value_min is not None and obs.value_max is not None:
        if obs.value_min > obs.value_max:
            return False
    return 25_000 <= obs.value_amount <= 400_000
