"""Parse raw Nomis ASHE JSON into CompensationObservation records.

The Nomis `.data.json` response carries an `obs` array where each entry is a single
observation indexed by its dimension values. Typical shape:

    {
      "obs": [
        {
          "time": {"value": "2024", "description": "2024"},
          "geography": {"value": "E12000007", "description": "London"},
          "sex": {"value": "7", "description": "Full-time"},
          "item": {"value": "2",   "description": "Median"},
          "pay": {"value": "7",    "description": "Gross weekly pay (£)"},
          "occupation": {"value": "2136", "description": "Programmers..."},
          "obs_value": {"value": 985.40}
        },
        ...
      ]
    }

Different Nomis datasets return slightly different dimension keys. We normalize
defensively — any missing dimension becomes None rather than crashing the batch.

Item codes we care about:
    2  → median (percentile=50, but flagged as 'median' in the payload)
    3  → mean  (stored as POINT with percentile=None)
    10, 25, 50, 75, 90 → percentile value directly

We store gross weekly figures as `period=weekly` and let the shared normalizer
multiply through to annual. This keeps the raw value in the DB for audit trails.
"""
from __future__ import annotations
import json
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from shared.models import (
    CompensationObservation,
    ContractType,
    ExperienceBand,
    ObservationType,
    Period,
)
from shared.normalization import NORMALIZATION_VERSION, normalize_to_annual


# Map Nomis "item" dimension → (observation_type, percentile)
# Codes based on the ASHE item codelist. Mean is stored as a POINT because it
# isn't a percentile; everything else is stored with its percentile number.
ITEM_CODE_MAP = {
    "2":  (ObservationType.PERCENTILE, 50),   # median
    "3":  (ObservationType.POINT, None),      # mean
    "10": (ObservationType.PERCENTILE, 10),
    "20": (ObservationType.PERCENTILE, 20),
    "25": (ObservationType.PERCENTILE, 25),
    "30": (ObservationType.PERCENTILE, 30),
    "40": (ObservationType.PERCENTILE, 40),
    "50": (ObservationType.PERCENTILE, 50),
    "60": (ObservationType.PERCENTILE, 60),
    "70": (ObservationType.PERCENTILE, 70),
    "75": (ObservationType.PERCENTILE, 75),
    "80": (ObservationType.PERCENTILE, 80),
    "90": (ObservationType.PERCENTILE, 90),
}


# Pay measure codes → (period, human label). ASHE publishes both weekly and
# annual across different tables; we tag whichever came back.
PAY_MEASURE_MAP = {
    "1": (Period.WEEKLY, "Gross weekly pay"),
    "7": (Period.WEEKLY, "Gross weekly pay"),
    "8": (Period.ANNUAL, "Gross annual pay"),
    "9": (Period.HOURLY, "Gross hourly pay"),
}


def _dim_value(obs: dict, key: str) -> Optional[str]:
    """Pull the `value` out of a dimension block, tolerating missing keys."""
    block = obs.get(key)
    if not block:
        return None
    if isinstance(block, dict):
        return str(block.get("value")) if block.get("value") is not None else None
    return str(block)


def _obs_value(obs: dict) -> Optional[float]:
    """Nomis nests the numeric value under obs_value.value (sometimes just value)."""
    v = obs.get("obs_value") or obs.get("value")
    if isinstance(v, dict):
        v = v.get("value")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _build_reference(
    dataset_id: str,
    year: Optional[str],
    geography: Optional[str],
    sex: Optional[str],
    item: Optional[str],
    pay: Optional[str],
    occupation: Optional[str],
) -> str:
    """Deterministic unique key per observation tuple (idempotent upserts)."""
    parts = [dataset_id, year or "_", geography or "_", sex or "_", item or "_", pay or "_", occupation or "_"]
    return ":".join(parts)


def parse_nomis_json(raw: dict, dataset_id: str) -> Iterable[CompensationObservation]:
    """Yield CompensationObservation records from a Nomis `.data.json` response."""
    observations = raw.get("obs") or []
    for obs in observations:
        year = _dim_value(obs, "time") or _dim_value(obs, "date")
        geography = _dim_value(obs, "geography")
        sex = _dim_value(obs, "sex")
        item = _dim_value(obs, "item")
        pay = _dim_value(obs, "pay")
        occupation = _dim_value(obs, "occupation")
        value = _obs_value(obs)

        if value is None:
            # Nomis suppresses low-confidence cells with nulls / "x". Skip them.
            continue

        obs_type_info = ITEM_CODE_MAP.get(item)
        if not obs_type_info:
            # Unknown item code — skip rather than miscategorize.
            continue
        obs_type, percentile = obs_type_info

        pay_info = PAY_MEASURE_MAP.get(pay) or (Period.WEEKLY, "Gross weekly pay")
        period, _ = pay_info

        normalized = normalize_to_annual(value, period.value)

        observed_at = None
        if year and year.isdigit():
            # ASHE is published for the April reference period; we anchor to April.
            observed_at = date(int(year), 4, 1)

        yield CompensationObservation(
            source_id=f"nomis_ashe_{dataset_id.lower()}",
            source_reference=_build_reference(dataset_id, year, geography, sex, item, pay, occupation),
            occupation_code=occupation,
            location_code=geography,
            company_ref=None,
            observation_type=obs_type,
            value_amount=value,
            value_min=None,
            value_max=None,
            percentile=percentile,
            period=period,
            normalized_annual_amount=normalized,
            normalization_method_version=NORMALIZATION_VERSION,
            currency="GBP",
            experience_band=ExperienceBand.UNKNOWN,   # ASHE doesn't break out experience
            contract_type=ContractType.PERMANENT,     # Full-time employees by filter
            sample_size=None,                         # Only available via separate CV/precision series
            total_comp_annual=None,
            observed_at=observed_at,
            source_payload=obs,
        )


def parse_file(path: Path, dataset_id: str) -> list[CompensationObservation]:
    """Convenience wrapper for `python -m ingestion.nomis.parse path/to/file.json`."""
    raw = json.loads(path.read_text())
    return list(parse_nomis_json(raw, dataset_id))


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python -m ingestion.nomis.parse <dataset_id> <path_to_json>")
        sys.exit(1)
    dataset_id = sys.argv[1]
    records = parse_file(Path(sys.argv[2]), dataset_id)
    print(f"Parsed {len(records)} observations from {sys.argv[2]}")
    if records:
        print("Sample:", records[0].to_dict())
