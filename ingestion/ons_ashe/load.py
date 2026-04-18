"""Load parsed ONS ASHE Table 2 observations into Postgres."""
from __future__ import annotations
from typing import Iterable

from shared.db import bulk_upsert
from shared.models import CompensationObservation


def load(observations: Iterable[CompensationObservation]) -> int:
    """Validate then bulk-upsert. Returns the count written."""
    all_obs = list(observations)
    validated = [o for o in all_obs if _is_valid(o)]
    rejected = len(all_obs) - len(validated)
    if rejected > 0:
        for o in all_obs[:5]:
            reasons = _rejection_reasons(o)
            valid = not reasons
            print(f"[ons_ashe:load] Sample: soc={o.occupation_code} "
                  f"val={o.value_amount} annual={o.normalized_annual_amount} "
                  f"valid={valid} reasons={reasons or 'OK'}")
        print(f"[ons_ashe:load] {rejected}/{len(all_obs)} observations rejected.")
    written = bulk_upsert(validated)
    return written


def _rejection_reasons(obs: CompensationObservation) -> list[str]:
    reasons = []
    if obs.value_amount is None and obs.value_min is None:
        reasons.append("no value")
    if obs.normalized_annual_amount is not None:
        if obs.normalized_annual_amount < 5_000:
            reasons.append(f"annual={obs.normalized_annual_amount:.0f} < 5000")
        if obs.normalized_annual_amount > 500_000:
            reasons.append(f"annual={obs.normalized_annual_amount:.0f} > 500000")
    return reasons


def _is_valid(obs: CompensationObservation) -> bool:
    """Validation rules for ASHE Table 2.

    We require a value and a plausible annual amount.
    Occupation code can be None for the "All employees" aggregate row.
    """
    if obs.value_amount is None and obs.value_min is None:
        return False
    if obs.normalized_annual_amount is not None:
        if obs.normalized_annual_amount < 5_000 or obs.normalized_annual_amount > 500_000:
            return False
    return True
