"""Load UCU / UCEA single pay spine observations."""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    return bulk_upsert([o for o in observations if _is_valid(o)])


def _is_valid(obs: CompensationObservation) -> bool:
    # The single pay spine currently spans ~£21k at point 1 to ~£86k at
    # point 51 (2023/24). Clip well outside that to reject misparses.
    return (
        obs.value_amount is not None
        and 10_000 <= obs.value_amount <= 150_000
    )
