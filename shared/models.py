"""Shared data models for compensation observations across all sources."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import date
from enum import Enum
from typing import Optional


class ObservationType(str, Enum):
    POINT = "point"
    RANGE = "range"
    PERCENTILE = "percentile"


class ContractType(str, Enum):
    PERMANENT = "permanent"
    CONTRACT_DAILY = "contract_daily"
    CONTRACT_HOURLY = "contract_hourly"
    PART_TIME = "part_time"
    UNKNOWN = "unknown"


class Period(str, Enum):
    ANNUAL = "annual"
    DAILY = "daily"
    HOURLY = "hourly"
    WEEKLY = "weekly"


class ExperienceBand(str, Enum):
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    PRINCIPAL = "principal"
    DIRECTOR = "director"
    UNKNOWN = "unknown"


@dataclass
class CompensationObservation:
    """One normalized compensation data point, ready to insert into the database.

    Maps directly to the compensation_observations table schema.
    """
    source_id: str                       # e.g., "nomis_ashe", "reed_listing"
    source_reference: str                # unique upstream identifier for idempotency
    occupation_code: Optional[str]       # SOC 2020 code (required for most)
    location_code: Optional[str]         # NUTS/region/city code; None for national
    company_ref: Optional[str]           # Companies House number or slug; None if unresolved
    observation_type: ObservationType
    value_amount: Optional[float]        # for point / percentile
    value_min: Optional[float]           # for range
    value_max: Optional[float]           # for range
    percentile: Optional[int]            # for percentile (1-99)
    period: Period
    normalized_annual_amount: Optional[float]
    normalization_method_version: Optional[str] = None
    currency: str = "GBP"
    experience_band: ExperienceBand = ExperienceBand.UNKNOWN
    contract_type: ContractType = ContractType.UNKNOWN
    sample_size: Optional[int] = None
    total_comp_annual: Optional[float] = None
    observed_at: Optional[date] = None
    source_payload: dict = field(default_factory=dict)  # original row for audit

    def to_dict(self) -> dict:
        d = asdict(self)
        # Enums → their values
        for k in ("observation_type", "period", "experience_band", "contract_type"):
            if d.get(k) is not None:
                d[k] = d[k].value if hasattr(d[k], "value") else d[k]
        # Derive observed_year from observed_at (matches the CHECK constraint).
        if self.observed_at is not None:
            d["observed_year"] = self.observed_at.year
            d["observed_at"] = self.observed_at.isoformat()
        else:
            raise ValueError(
                f"observed_at is required (source_id={self.source_id}, "
                f"ref={self.source_reference})"
            )
        return d
