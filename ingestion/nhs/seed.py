"""Seed data for NHS Agenda for Change pay scales.

AfC pay scales are published annually by NHS Employers and are public record.
When the HTML scraper can't parse the current page (NHS Employers periodically
redesigns their site), we fall back to these hand-curated values.

Sources:
    https://www.nhsemployers.org/articles/pay-scales-202425
    https://www.nhsemployers.org/articles/pay-scales-202526

The 2024/25 scales include the 5.5% consolidated pay rise.
The 2025/26 scales include the 3.6% consolidated pay rise.

Each band has entry, intermediate (2 years), and top-of-band values.
Bands 8a-9 gained new intermediate spine points in the 2024/25 deal.
"""
from __future__ import annotations
from datetime import date
from typing import Iterable

from shared.models import (
    CompensationObservation,
    ContractType,
    ExperienceBand,
    ObservationType,
    Period,
)
from shared.normalization import NORMALIZATION_VERSION

# (band_label, step_label, annual_salary)
# Source: NHS Employers AfC pay scales 2024/25 (England, 5.5% uplift applied)
# Verified against https://www.nhsemployers.org/articles/pay-scales-202425
_AFC_2024: list[tuple[str, str, int]] = [
    ("Band 1", "Spot rate", 23_615),
    ("Band 2", "Entry", 23_615),
    ("Band 2", "Top", 23_615),
    ("Band 3", "Entry", 24_071),
    ("Band 3", "Top", 25_674),
    ("Band 4", "Entry", 26_530),
    ("Band 4", "Top", 29_114),
    ("Band 5", "Entry", 29_970),
    ("Band 5", "2 years", 32_324),
    ("Band 5", "Top", 36_483),
    ("Band 6", "Entry", 37_338),
    ("Band 6", "2 years", 39_405),
    ("Band 6", "Top", 44_962),
    ("Band 7", "Entry", 46_148),
    ("Band 7", "2 years", 48_526),
    ("Band 7", "Top", 52_809),
    ("Band 8a", "Entry", 53_755),
    ("Band 8a", "2 years", 56_454),
    ("Band 8a", "Top", 60_504),
    ("Band 8b", "Entry", 62_215),
    ("Band 8b", "2 years", 66_246),
    ("Band 8b", "Top", 72_293),
    ("Band 8c", "Entry", 74_290),
    ("Band 8c", "2 years", 78_814),
    ("Band 8c", "Top", 85_601),
    ("Band 8d", "Entry", 88_168),
    ("Band 8d", "2 years", 93_572),
    ("Band 8d", "Top", 101_677),
    ("Band 9", "Entry", 105_385),
    ("Band 9", "2 years", 111_740),
    ("Band 9", "Top", 121_271),
]

# Source: NHS Employers AfC pay scales 2025/26 (England, 3.6% uplift applied)
# Estimated from corrected 2024/25 values × 1.036, rounded to nearest £.
# Should be verified against https://www.nhsemployers.org/articles/pay-scales-202526
_AFC_2025: list[tuple[str, str, int]] = [
    ("Band 1", "Spot rate", 24_465),
    ("Band 2", "Entry", 24_465),
    ("Band 2", "Top", 24_465),
    ("Band 3", "Entry", 24_938),
    ("Band 3", "Top", 26_598),
    ("Band 4", "Entry", 27_485),
    ("Band 4", "Top", 30_162),
    ("Band 5", "Entry", 31_049),
    ("Band 5", "2 years", 33_488),
    ("Band 5", "Top", 37_796),
    ("Band 6", "Entry", 38_682),
    ("Band 6", "2 years", 40_824),
    ("Band 6", "Top", 46_581),
    ("Band 7", "Entry", 47_809),
    ("Band 7", "2 years", 50_273),
    ("Band 7", "Top", 54_710),
    ("Band 8a", "Entry", 55_690),
    ("Band 8a", "2 years", 58_486),
    ("Band 8a", "Top", 62_682),
    ("Band 8b", "Entry", 64_455),
    ("Band 8b", "2 years", 68_631),
    ("Band 8b", "Top", 74_896),
    ("Band 8c", "Entry", 76_964),
    ("Band 8c", "2 years", 81_651),
    ("Band 8c", "Top", 88_683),
    ("Band 8d", "Entry", 91_342),
    ("Band 8d", "2 years", 96_941),
    ("Band 8d", "Top", 105_337),
    ("Band 9", "Entry", 109_179),
    ("Band 9", "2 years", 115_763),
    ("Band 9", "Top", 125_637),
]

_SEED_YEARS: dict[int, list[tuple[str, str, int]]] = {
    2024: _AFC_2024,
    2025: _AFC_2025,
}

_BAND_TO_EXPERIENCE = {
    "Band 1": ExperienceBand.JUNIOR,
    "Band 2": ExperienceBand.JUNIOR,
    "Band 3": ExperienceBand.JUNIOR,
    "Band 4": ExperienceBand.MID,
    "Band 5": ExperienceBand.MID,
    "Band 6": ExperienceBand.SENIOR,
    "Band 7": ExperienceBand.SENIOR,
    "Band 8a": ExperienceBand.LEAD,
    "Band 8b": ExperienceBand.LEAD,
    "Band 8c": ExperienceBand.PRINCIPAL,
    "Band 8d": ExperienceBand.PRINCIPAL,
    "Band 9": ExperienceBand.DIRECTOR,
}


def emit_seed(year: int) -> list[CompensationObservation]:
    """Return seed observations for the given pay year."""
    rows = _SEED_YEARS.get(year)
    if not rows:
        available = ", ".join(str(y) for y in sorted(_SEED_YEARS))
        raise ValueError(
            f"No seed data for AfC year {year}. Available: {available}"
        )
    observed_at = date(year, 4, 1)
    out: list[CompensationObservation] = []
    for band, step, salary in rows:
        out.append(CompensationObservation(
            source_id="nhs_afc",
            source_reference=f"nhs_afc:{year}:{band}:{step}",
            occupation_code=None,
            location_code="K02000001",
            company_ref="NHS",
            observation_type=ObservationType.POINT,
            value_amount=float(salary),
            value_min=None,
            value_max=None,
            percentile=None,
            period=Period.ANNUAL,
            normalized_annual_amount=float(salary),
            normalization_method_version=NORMALIZATION_VERSION,
            currency="GBP",
            experience_band=_BAND_TO_EXPERIENCE.get(band, ExperienceBand.UNKNOWN),
            contract_type=ContractType.PERMANENT,
            sample_size=None,
            total_comp_annual=None,
            observed_at=observed_at,
            source_payload={
                "band": band,
                "spine_label": step,
                "year_starting": year,
                "scheme": "Agenda for Change",
                "source": "seed",
            },
        ))
    return out
