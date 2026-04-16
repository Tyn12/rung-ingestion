"""Parse Civil Service pay bands + hand-curated fallback baseline.

We emit one RANGE observation per grade (min–max), tagging experience band
based on the standard civil-service ladder mapping.

Mapping:
    AA, AO              → JUNIOR
    EO                  → JUNIOR (entry/early-career)
    HEO, SEO            → MID
    G7                  → SENIOR
    G6                  → SENIOR
    SCS1 (Deputy Dir)   → LEAD
    SCS2 (Dir)          → DIRECTOR
    SCS3 (Dir General)  → DIRECTOR
    SCS4 (Perm Sec)     → DIRECTOR
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import openpyxl

from shared.models import (
    CompensationObservation,
    ContractType,
    ExperienceBand,
    ObservationType,
    Period,
)
from shared.normalization import NORMALIZATION_VERSION


@dataclass(frozen=True)
class CivilServiceBand:
    code: str
    label: str
    min_salary: float
    max_salary: float
    experience_band: ExperienceBand


# Hand-curated baseline (2023/24, delegated grades from typical department
# scales + SCS framework published minima/maxima). Update annually as new
# SCS pay framework PDFs land. Values are national minima; London adds ~15%.
BASELINE_BANDS_2023: tuple[CivilServiceBand, ...] = (
    CivilServiceBand("AA",   "Administrative Assistant",     19_800,  21_300, ExperienceBand.JUNIOR),
    CivilServiceBand("AO",   "Administrative Officer",       22_800,  25_400, ExperienceBand.JUNIOR),
    CivilServiceBand("EO",   "Executive Officer",            27_200,  30_800, ExperienceBand.JUNIOR),
    CivilServiceBand("HEO",  "Higher Executive Officer",     32_700,  37_500, ExperienceBand.MID),
    CivilServiceBand("SEO",  "Senior Executive Officer",     40_500,  47_200, ExperienceBand.MID),
    CivilServiceBand("G7",   "Grade 7",                      52_200,  65_000, ExperienceBand.SENIOR),
    CivilServiceBand("G6",   "Grade 6",                      65_100,  82_000, ExperienceBand.SENIOR),
    CivilServiceBand("SCS1", "Senior Civil Service Pay Band 1 (Deputy Director)",  75_000, 117_800, ExperienceBand.LEAD),
    CivilServiceBand("SCS2", "Senior Civil Service Pay Band 2 (Director)",         97_000, 162_500, ExperienceBand.DIRECTOR),
    CivilServiceBand("SCS3", "Senior Civil Service Pay Band 3 (Director General)", 120_000, 200_000, ExperienceBand.DIRECTOR),
    CivilServiceBand("SCS4", "Senior Civil Service Pay Band 4 (Permanent Secretary)", 150_000, 208_100, ExperienceBand.DIRECTOR),
)


def _observation(band: CivilServiceBand, year_starting: int) -> CompensationObservation:
    midpoint = (band.min_salary + band.max_salary) / 2
    return CompensationObservation(
        source_id="civil_service_pay",
        source_reference=f"civil_service:{year_starting}:{band.code}",
        occupation_code=None,
        location_code="K02000001",
        company_ref="UK Civil Service",
        observation_type=ObservationType.RANGE,
        value_amount=midpoint,
        value_min=band.min_salary,
        value_max=band.max_salary,
        percentile=None,
        period=Period.ANNUAL,
        normalized_annual_amount=midpoint,
        normalization_method_version=NORMALIZATION_VERSION,
        currency="GBP",
        experience_band=band.experience_band,
        contract_type=ContractType.PERMANENT,
        sample_size=None,
        total_comp_annual=None,
        observed_at=date(year_starting, 4, 1),
        source_payload={
            "grade_code": band.code,
            "grade_label": band.label,
            "year_starting": year_starting,
            "note": "national minima; London weighting typically +10-15%",
        },
    )


def seed_bands(year_starting: int) -> list[CompensationObservation]:
    return [_observation(b, year_starting) for b in BASELINE_BANDS_2023]


# Grade-code → experience band lookup (case-insensitive, tolerant of spaces).
_GRADE_TO_BAND: dict[str, ExperienceBand] = {
    "AA": ExperienceBand.JUNIOR,
    "AO": ExperienceBand.JUNIOR,
    "EO": ExperienceBand.JUNIOR,
    "HEO": ExperienceBand.MID,
    "SEO": ExperienceBand.MID,
    "G7": ExperienceBand.SENIOR,
    "GRADE7": ExperienceBand.SENIOR,
    "G6": ExperienceBand.SENIOR,
    "GRADE6": ExperienceBand.SENIOR,
    "SCS1": ExperienceBand.LEAD,
    "SCSPB1": ExperienceBand.LEAD,
    "SCS2": ExperienceBand.DIRECTOR,
    "SCSPB2": ExperienceBand.DIRECTOR,
    "SCS3": ExperienceBand.DIRECTOR,
    "SCSPB3": ExperienceBand.DIRECTOR,
    "SCS4": ExperienceBand.DIRECTOR,
    "SCSPB4": ExperienceBand.DIRECTOR,
}


def _normalize_grade(raw: str) -> str:
    return raw.upper().replace(" ", "").replace("-", "").replace("_", "")


def parse_bands(path: Path, year_starting: int) -> list[CompensationObservation]:
    """Parse a gov.uk spreadsheet with grade / min / max columns."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out: list[CompensationObservation] = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        header_idx = None
        for i, row in enumerate(rows):
            labels = [str(c).lower().strip() if c else "" for c in row]
            has_grade = any("grade" in l or "band" in l for l in labels)
            has_min = any("min" in l or "minimum" in l or "floor" in l for l in labels)
            has_max = any("max" in l or "maximum" in l or "ceiling" in l for l in labels)
            if has_grade and has_min and has_max:
                header_idx = i
                break
        if header_idx is None:
            continue
        headers = [str(c).lower().strip() if c else "" for c in rows[header_idx]]
        grade_col = next(
            (i for i, h in enumerate(headers) if "grade" in h or "band" in h), None
        )
        min_col = next(
            (i for i, h in enumerate(headers) if "min" in h or "floor" in h), None
        )
        max_col = next(
            (i for i, h in enumerate(headers) if "max" in h or "ceiling" in h), None
        )
        if None in (grade_col, min_col, max_col):
            continue
        for row in rows[header_idx + 1 :]:
            if not row or row[grade_col] is None:
                continue
            try:
                grade_raw = str(row[grade_col]).strip()
                min_s = float(str(row[min_col]).replace(",", "").replace("£", "").strip())
                max_s = float(str(row[max_col]).replace(",", "").replace("£", "").strip())
            except (TypeError, ValueError, IndexError):
                continue
            code = _normalize_grade(grade_raw)
            exp = _GRADE_TO_BAND.get(code, ExperienceBand.UNKNOWN)
            if not (15_000 <= min_s <= max_s <= 250_000):
                continue
            out.append(
                _observation(
                    CivilServiceBand(code, grade_raw, min_s, max_s, exp),
                    year_starting,
                )
            )
        if out:
            return out
    return out


if __name__ == "__main__":
    import sys
    records = seed_bands(int(sys.argv[1]) if len(sys.argv) > 1 else 2023)
    print(f"{len(records)} bands in seed for year {records[0].observed_at.year}")
