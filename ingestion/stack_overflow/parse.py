"""Parse Stack Overflow survey CSV → UK respondent-level observations.

Key columns (2023/2024 schema — older years need alias maps):
    ResponseId               unique respondent id
    Country                  free-text country; we filter to 'United Kingdom of Great Britain and Northern Ireland'
    DevType                  role title (multi-select, ;-separated)
    YearsCodePro             experience in years
    Employment               full-time / self-employed / student / etc
    ConvertedCompYearly      USD-normalised annual comp (we prefer this for cross-year comparability)
    CompTotal                raw comp
    Currency                 raw currency
    Industry                 2024+ only

We emit one CompensationObservation per respondent, anchored to July 1 of the
survey year (survey fielded in May/June, roughly midpoint = July).
"""
from __future__ import annotations
import csv
import sys
from datetime import date
from pathlib import Path

# SO survey CSVs contain very large freetext fields that exceed the default
# 131072-byte limit.
csv.field_size_limit(sys.maxsize)
from typing import Iterator, Optional

from shared.models import (
    CompensationObservation,
    ContractType,
    ExperienceBand,
    ObservationType,
    Period,
)
from shared.normalization import NORMALIZATION_VERSION

UK_COUNTRY_NAMES = {
    "United Kingdom",
    "United Kingdom of Great Britain and Northern Ireland",
    "UK",
    "Great Britain",
}

# Column alias map — older SO surveys used different headers.
COL_ALIASES: dict[str, list[str]] = {
    "country":        ["Country"],
    "respondent_id":  ["ResponseId", "Respondent"],
    "dev_type":       ["DevType"],
    "years_code_pro": ["YearsCodePro", "WorkExp", "YearsCodedJob"],
    "employment":     ["Employment"],
    "comp_yearly":    ["ConvertedCompYearly", "ConvertedComp", "Salary"],
    "comp_raw":       ["CompTotal"],
    "currency":       ["Currency", "CurrencySymbol"],
    "industry":       ["Industry"],
}


def _pick(row: dict, key: str) -> Optional[str]:
    for alias in COL_ALIASES[key]:
        if alias in row and row[alias] not in (None, "", "NA"):
            return row[alias]
    return None


def _experience_from_years(s: Optional[str]) -> ExperienceBand:
    if not s:
        return ExperienceBand.UNKNOWN
    s = s.strip().lower()
    if s in ("less than 1 year", "< 1", "0"):
        return ExperienceBand.JUNIOR
    if s == "more than 50 years":
        return ExperienceBand.PRINCIPAL
    try:
        years = float(s)
    except ValueError:
        return ExperienceBand.UNKNOWN
    if years < 3:
        return ExperienceBand.JUNIOR
    if years < 7:
        return ExperienceBand.MID
    if years < 12:
        return ExperienceBand.SENIOR
    if years < 18:
        return ExperienceBand.LEAD
    return ExperienceBand.PRINCIPAL


def _contract_from_employment(s: Optional[str]) -> ContractType:
    if not s:
        return ContractType.UNKNOWN
    low = s.lower()
    if "part-time" in low or "part time" in low:
        return ContractType.PART_TIME
    if "independent contractor" in low or "freelanc" in low or "self-employed" in low:
        return ContractType.CONTRACT_DAILY
    if "full-time" in low or "full time" in low or "employed" in low:
        return ContractType.PERMANENT
    return ContractType.UNKNOWN


def parse_csv(path: Path, year: int) -> Iterator[CompensationObservation]:
    observed_at = date(year, 7, 1)
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            country = _pick(row, "country")
            if not country or country not in UK_COUNTRY_NAMES:
                continue
            comp_str = _pick(row, "comp_yearly")
            if not comp_str:
                continue
            try:
                comp = float(comp_str)
            except ValueError:
                continue
            # ConvertedCompYearly is in USD. Convert to GBP using a conservative
            # approximation — later we'll store the raw USD value too and let dbt
            # apply a time-variant FX rate. 0.79 is a mid-2024 average.
            # Note: we store this as POINT observation with currency=USD and let
            # the normalized_annual_amount carry the GBP-equivalent figure.
            gbp = comp * 0.79
            if gbp < 8_000 or gbp > 1_000_000:
                # Clip implausible entries (trolls, students reporting internship stipends, etc.)
                continue

            respondent_id = _pick(row, "respondent_id") or row.get("ResponseId") or ""
            yield CompensationObservation(
                source_id="stackoverflow_survey",
                source_reference=f"so_{year}_{respondent_id}",
                occupation_code=None,
                location_code="K02000001",   # UK as a whole; SO doesn't expose region
                company_ref=None,
                observation_type=ObservationType.POINT,
                value_amount=comp,
                value_min=None,
                value_max=None,
                percentile=None,
                period=Period.ANNUAL,
                normalized_annual_amount=gbp,
                normalization_method_version=NORMALIZATION_VERSION,
                currency="USD",
                experience_band=_experience_from_years(_pick(row, "years_code_pro")),
                contract_type=_contract_from_employment(_pick(row, "employment")),
                sample_size=1,
                total_comp_annual=None,
                observed_at=observed_at,
                source_payload={
                    "year": year,
                    "respondent_id": respondent_id,
                    "dev_type": _pick(row, "dev_type"),
                    "employment": _pick(row, "employment"),
                    "industry": _pick(row, "industry"),
                    "years_code_pro": _pick(row, "years_code_pro"),
                    "currency_native": _pick(row, "currency"),
                    "comp_native": _pick(row, "comp_raw"),
                    "comp_converted_usd": comp,
                },
            )


if __name__ == "__main__":
    import sys
    year = int(sys.argv[1])
    csv_path = Path(sys.argv[2])
    n = sum(1 for _ in parse_csv(csv_path, year))
    print(f"{n} UK respondents in survey {year}")
