"""Load NHS Agenda for Change observations."""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    return bulk_upsert([o for o in observations if _is_valid(o)])


def _is_valid(obs: CompensationObservation) -> bool:
    # AfC salaries realistically fall between ~£23k (band 2 start) and
    # ~£130k (top of band 9 + future uplifts). Anything outside is a misparse.
    return (
        obs.value_amount is not None
        and 15_000 <= obs.value_amount <= 160_000
    )
