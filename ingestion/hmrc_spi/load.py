"""Load HMRC SPI percentile observations."""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    return bulk_upsert([o for o in observations if _is_valid(o)])


def _is_valid(obs: CompensationObservation) -> bool:
    # SPI percentiles realistically span ~£6k (P10) to >£400k (P99). Allow a
    # broad envelope.
    if obs.value_amount is None or obs.percentile is None:
        return False
    if not (1 <= obs.percentile <= 99):
        return False
    return 1_000 <= obs.value_amount <= 5_000_000
