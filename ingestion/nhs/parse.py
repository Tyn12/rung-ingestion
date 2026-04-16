"""Parse NHS Agenda for Change HTML into CompensationObservation rows.

The NHS Employers page structures each band as a table whose caption / preceding
heading names the band (e.g. "Band 5"). Within each table, rows are spine points
and columns are "Years at point" + "Annual salary".

We emit one observation per (band, spine point) × year. Each is a POINT
observation (authoritative pay, not a percentile) so we set value_amount to the
annual figure directly.

Because layout can drift between years, we:
    - Walk every <table> in the document
    - Look backwards to the nearest <h2> / <h3> / caption for the band name
    - Find the column whose header contains "salary" (case-insensitive)
    - Extract any £-prefixed or pure-numeric cell values from that column
"""
from __future__ import annotations
import re
from datetime import date
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup

from shared.models import (
    CompensationObservation,
    ContractType,
    ExperienceBand,
    ObservationType,
    Period,
)
from shared.normalization import NORMALIZATION_VERSION

BAND_RE = re.compile(r"band\s*(\d)", re.IGNORECASE)
MONEY_RE = re.compile(r"£?\s*([\d,]+(?:\.\d+)?)")


def _find_nearest_band(table) -> str | None:
    """Walk backwards through siblings looking for a Band heading."""
    el = table
    while el is not None:
        el = el.find_previous(["h1", "h2", "h3", "h4", "caption"])
        if el is None:
            return None
        m = BAND_RE.search(el.get_text(" ", strip=True))
        if m:
            return f"Band {m.group(1)}"
    return None


def _header_indices(table) -> dict:
    headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
    if not headers:
        # Fallback: treat the first row as headers
        first_row = table.find("tr")
        if first_row:
            headers = [c.get_text(" ", strip=True).lower() for c in first_row.find_all(["th", "td"])]
    result = {}
    for i, h in enumerate(headers):
        if "salary" in h:
            result.setdefault("salary", i)
        if "year" in h or "point" in h:
            result.setdefault("label", i)
    return result


def _parse_money(cell_text: str) -> float | None:
    m = MONEY_RE.search(cell_text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _band_to_experience(band_name: str) -> ExperienceBand:
    """Rough map so the app can filter by seniority."""
    n = int(re.search(r"(\d)", band_name).group(1))
    if n <= 3:
        return ExperienceBand.JUNIOR
    if n <= 5:
        return ExperienceBand.MID
    if n <= 7:
        return ExperienceBand.SENIOR
    if n == 8:
        return ExperienceBand.LEAD
    return ExperienceBand.PRINCIPAL


def parse_afc_html(html: str, year_starting: int) -> list[CompensationObservation]:
    soup = BeautifulSoup(html, "lxml")
    observed_at = date(year_starting, 4, 1)
    out: list[CompensationObservation] = []

    for table in soup.find_all("table"):
        band = _find_nearest_band(table)
        if not band:
            continue
        cols = _header_indices(table)
        if "salary" not in cols:
            continue
        salary_idx = cols["salary"]
        label_idx = cols.get("label", 0)

        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) <= salary_idx:
                continue
            salary = _parse_money(cells[salary_idx].get_text(" ", strip=True))
            if not salary:
                continue
            label = cells[label_idx].get_text(" ", strip=True)[:100] if label_idx < len(cells) else ""
            # Skip header-like rows
            if "salary" in label.lower():
                continue

            out.append(CompensationObservation(
                source_id="nhs_afc",
                source_reference=f"nhs_afc:{year_starting}:{band}:{label}",
                occupation_code=None,            # Band-level; occupation mapping is 1-to-many
                location_code="K02000001",       # UK-wide, with HCAS supplements on top
                company_ref="NHS",
                observation_type=ObservationType.POINT,
                value_amount=salary,
                value_min=None,
                value_max=None,
                percentile=None,
                period=Period.ANNUAL,
                normalized_annual_amount=salary,
                normalization_method_version=NORMALIZATION_VERSION,
                currency="GBP",
                experience_band=_band_to_experience(band),
                contract_type=ContractType.PERMANENT,
                sample_size=None,
                total_comp_annual=None,
                observed_at=observed_at,
                source_payload={
                    "band": band,
                    "spine_label": label,
                    "year_starting": year_starting,
                    "scheme": "Agenda for Change",
                },
            ))
    return out


def parse_file(path: Path, year_starting: int) -> list[CompensationObservation]:
    return parse_afc_html(path.read_text(encoding="utf-8"), year_starting)


if __name__ == "__main__":
    import sys
    year = int(sys.argv[1])
    path = Path(sys.argv[2])
    records = parse_file(path, year)
    print(f"{len(records)} spine points parsed for year {year}")
