"""Parse Nomis ASHE CSV data into CompensationObservation records.

The Nomis `.data.csv` response has flat columns including:
    DATE_NAME, GEOGRAPHY_CODE, GEOGRAPHY_NAME, SEX_NAME, ITEM_NAME,
    ITEM_CODE, PAY_NAME, OCCUPATION_CODE, OCCUPATION_NAME, OBS_VALUE,
    OBS_STATUS_NAME

Item codes we care about:
    2  → median (percentile=50)
    3  → mean  (stored as POINT with percentile=None)
    10, 25, 75, 90 → percentile value directly

We store gross weekly figures as `period=weekly` and let the shared normalizer
multiply through to annual. This keeps the raw value in the DB for audit trails.
"""
from __future__ import annotations
import csv
import json
from datetime import date
from io import StringIO
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


# Map Nomis "ITEM_CODE" → (observation_type, percentile)
ITEM_CODE_MAP = {
    "2":  (ObservationType.PERCENTILE, 50),   # median
    "3":  (ObservationType.POINT, None),       # mean
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


def _parse_year(date_name: str) -> Optional[int]:
    """Extract year from DATE_NAME like '2024' or 'Apr 2024'."""
    if not date_name:
        return None
    # Take last 4 digits that look like a year
    for token in reversed(date_name.split()):
        token = token.strip()
        if len(token) == 4 and token.isdigit():
            y = int(token)
            if 1990 <= y <= 2100:
                return y
    return None


def _safe_float(raw: str) -> Optional[float]:
    if not raw or raw.strip() in ("", "..", "x", "z", ":", "-", "*"):
        return None
    try:
        return float(raw.strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def _classify_pay(pay_name: str) -> Optional[Period]:
    """Map PAY_NAME to a Period, or None if the row should be skipped.

    ASHE datasets return several pay types including annual, weekly,
    hourly, and percentage-change rows.  We only ingest monetary values
    and tag them with the correct period so the normalizer works.
    """
    low = pay_name.lower()
    if "percent" in low or "change" in low:
        return None          # skip percentage-change rows
    if "annual" in low:
        return Period.ANNUAL
    if "weekly" in low:
        return Period.WEEKLY
    if "hourly" in low:
        return Period.HOURLY
    # Unknown pay type — skip to be safe
    return None


def parse_nomis_csv(csv_text: str, dataset_id: str) -> Iterable[CompensationObservation]:
    """Yield CompensationObservation records from a Nomis CSV response."""
    reader = csv.DictReader(StringIO(csv_text))
    for row in reader:
        value = _safe_float(row.get("OBS_VALUE", ""))
        if value is None:
            continue

        item_code = (row.get("ITEM_CODE") or "").strip()
        obs_type_info = ITEM_CODE_MAP.get(item_code)
        if not obs_type_info:
            continue
        obs_type, percentile = obs_type_info

        # Determine period from PAY_NAME; skip non-monetary rows
        pay_name = (row.get("PAY_NAME") or "").strip()
        period = _classify_pay(pay_name)
        if period is None:
            continue

        geography = (row.get("GEOGRAPHY_CODE") or "").strip() or None
        occupation = (row.get("OCCUPATION_CODE") or "").strip() or None
        date_name = (row.get("DATE_NAME") or "").strip()
        year = _parse_year(date_name)

        # ASHE is published for the April reference period
        observed_at = date(year, 4, 1) if year else None

        # Build deterministic reference for upserts
        ref_parts = [
            dataset_id,
            str(year) if year else "_",
            geography or "_",
            (row.get("SEX_NAME") or "7").strip(),
            item_code,
            pay_name[:20] or "7",
            occupation or "_",
        ]
        ref = ":".join(ref_parts)

        normalized = normalize_to_annual(value, period.value)

        yield CompensationObservation(
            source_id=f"nomis_ashe_{dataset_id.lower()}",
            source_reference=ref,
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
            experience_band=ExperienceBand.UNKNOWN,
            contract_type=ContractType.PERMANENT,
            sample_size=None,
            total_comp_annual=None,
            observed_at=observed_at,
            source_payload={
                "dataset": dataset_id,
                "date_name": date_name,
                "geography_name": (row.get("GEOGRAPHY_NAME") or "").strip(),
                "sex": (row.get("SEX_NAME") or "").strip(),
                "item": (row.get("ITEM_NAME") or "").strip(),
                "pay": (row.get("PAY_NAME") or "").strip(),
                "occupation_name": (row.get("OCCUPATION_NAME") or "").strip(),
                "obs_status": (row.get("OBS_STATUS_NAME") or "").strip(),
                "raw_value": value,
                "period": period.value,
            },
        )


def parse_file(path: Path, dataset_id: str) -> list[CompensationObservation]:
    """Parse a saved CSV file."""
    text = path.read_text(encoding="utf-8")
    return list(parse_nomis_csv(text, dataset_id))


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python -m ingestion.nomis.parse <dataset_id> <path_to_csv>")
        sys.exit(1)
    dataset_id = sys.argv[1]
    records = parse_file(Path(sys.argv[2]), dataset_id)
    print(f"Parsed {len(records)} observations from {sys.argv[2]}")
    if records:
        print("Sample:", records[0].to_dict())
