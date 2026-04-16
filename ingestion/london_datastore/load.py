"""Load London Datastore observations."""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    return bulk_upsert([o for o in observations if _is_valid(o)])


def _is_valid(obs: CompensationObservation) -> bool:
    # Borough medians realistically £15k-£110k (Westminster outlier ~£90k).
    return (
        obs.value_amount is not None
        and 5_000 <= obs.value_amount <= 250_000
    )
