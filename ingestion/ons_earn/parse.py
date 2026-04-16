"""Parse ONS EARN01/02/03 XLSX into CompensationObservation records.

ONS time-series workbooks have a conventional layout:
    - A small metadata header at the top (Title, CDID, PreUnit, Unit, …)
    - Then a very long time-series column per series (month, quarter, year)
    - Each sheet typically contains a set of related CDID series

We:
    1. Detect the row that starts with a date-looking cell ("1963 JAN" / "2024 NOV" / "2024").
    2. Extract monthly time-series rows (YYYY MMM format) per column.
    3. Convert weekly earnings → annual × 52, normalise, emit POINT observations.

EARN02 (by industry) sheets are each dedicated to one industry, with the
sheet title giving the SIC section. EARN03 sheets mirror that for regions.
"""
from __future__ import annotations
import re
from calendar import month_abbr
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

import openpyxl

from shared.models import (
    CompensationObservation,
    ContractType,
    ExperienceBand,
    ObservationType,
    Period,
)
from shared.normalization import NORMALIZATION_VERSION

_MONTH = {name.upper(): i for i, name in enumerate(month_abbr) if name}

# Map SIC section labels (EARN02 sheet names) onto SOC-adjacent occupation hints.
# We store SIC code in source_payload rather than occupation_code (which uses SOC).
_SIC_SECTION_HINTS: dict[str, str] = {
    "AGRICULTURE": "A", "MINING": "B", "MANUFACTURING": "C",
    "ELECTRICITY": "D", "WATER": "E", "CONSTRUCTION": "F",
    "WHOLESALE": "G", "TRANSPORTATION": "H", "TRANSPORT": "H",
    "ACCOMMODATION": "I", "INFORMATION": "J", "ICT": "J",
    "FINANCE": "K", "REAL ESTATE": "L", "PROFESSIONAL": "M",
    "ADMINISTRATIVE": "N", "PUBLIC ADMIN": "O", "EDUCATION": "P",
    "HEALTH": "Q", "ARTS": "R", "OTHER": "S",
}

# Region labels → ONS geography codes (E12000001-E12000009, N92000002, S92000003, W92000004).
_REGION_TO_CODE: dict[str, str] = {
    "NORTH EAST": "E12000001",
    "NORTH WEST": "E12000002",
    "YORKSHIRE": "E12000003",
    "EAST MIDLANDS": "E12000004",
    "WEST MIDLANDS": "E12000005",
    "EAST OF ENGLAND": "E12000006",
    "EAST": "E12000006",
    "LONDON": "E12000007",
    "SOUTH EAST": "E12000008",
    "SOUTH WEST": "E12000009",
    "NORTHERN IRELAND": "N92000002",
    "SCOTLAND": "S92000003",
    "WALES": "W92000004",
}


_MONTHLY_TS_RE = re.compile(r"^\s*(\d{4})\s+([A-Za-z]{3})\s*$")


def _parse_period_label(raw: str) -> Optional[date]:
    m = _MONTHLY_TS_RE.match(str(raw))
    if not m:
        return None
    year = int(m.group(1))
    mi = _MONTH.get(m.group(2).upper())
    if not mi:
        return None
    return date(year, mi, 1)


def _safe_float(raw) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _infer_sic_from_sheet(name: str) -> Optional[str]:
    up = name.upper()
    for hint, code in _SIC_SECTION_HINTS.items():
        if hint in up:
            return code
    return None


def _infer_region_from_sheet(name: str) -> Optional[str]:
    up = name.upper()
    for hint, code in _REGION_TO_CODE.items():
        if hint in up:
            return code
    return None


def _observation(
    *,
    dataset_code: str,
    sheet_name: str,
    series_header: str,
    observed_at: date,
    weekly: float,
    axis: str,
) -> CompensationObservation:
    location_code = (
        _infer_region_from_sheet(sheet_name) if axis == "region" else "K02000001"
    )
    sic = _infer_sic_from_sheet(sheet_name) if axis == "industry" else None

    annual = weekly * 52.0
    ref = (
        f"ons_earn:{dataset_code}:{sheet_name}:{series_header}:{observed_at.isoformat()}"
    )
    return CompensationObservation(
        source_id="ons_earn",
        source_reference=ref,
        occupation_code=None,
        location_code=location_code,
        company_ref=None,
        observation_type=ObservationType.POINT,
        value_amount=weekly,
        value_min=None,
        value_max=None,
        percentile=None,
        period=Period.WEEKLY,
        normalized_annual_amount=annual,
        normalization_method_version=NORMALIZATION_VERSION,
        currency="GBP",
        experience_band=ExperienceBand.UNKNOWN,
        contract_type=ContractType.UNKNOWN,
        sample_size=None,
        total_comp_annual=None,
        observed_at=observed_at,
        source_payload={
            "dataset": dataset_code,
            "sheet": sheet_name,
            "series": series_header,
            "sic_section": sic,
            "axis": axis,
            "weekly_gbp": weekly,
        },
    )


def _extract_from_sheet(
    ws, *, dataset_code: str, axis: str
) -> list[CompensationObservation]:
    """Pick up monthly series rows on a sheet. Skip annual/quarterly rows."""
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    # Use the first non-empty row with >1 cell as the series header (CDID row
    # is typical but we tolerate other ONS variants).
    header_row: list[str] = []
    for row in rows[:20]:
        cells = [str(c).strip() if c is not None else "" for c in row]
        if sum(1 for c in cells if c) >= 2:
            header_row = cells
            break

    out: list[CompensationObservation] = []
    for row in rows:
        if not row:
            continue
        label = row[0]
        observed_at = _parse_period_label(label) if label else None
        if observed_at is None:
            continue
        for col_idx in range(1, len(row)):
            weekly = _safe_float(row[col_idx])
            if weekly is None or weekly < 50 or weekly > 5_000:
                continue
            header = (
                header_row[col_idx] if col_idx < len(header_row) else f"col{col_idx}"
            )
            out.append(
                _observation(
                    dataset_code=dataset_code,
                    sheet_name=ws.title,
                    series_header=header or f"col{col_idx}",
                    observed_at=observed_at,
                    weekly=weekly,
                    axis=axis,
                )
            )
    return out


def parse_earn_file(
    path: Path,
    *,
    dataset_code: str,
    axis: str,
    since: Optional[date] = None,
) -> list[CompensationObservation]:
    """Extract weekly earnings observations from an ONS EARN workbook.

    Pass ``since`` to limit to recent observations (e.g. only the last 2 years);
    otherwise the full time-series back to the 1960s comes through.
    """
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    all_obs: list[CompensationObservation] = []
    for ws in wb.worksheets:
        all_obs.extend(
            _extract_from_sheet(ws, dataset_code=dataset_code, axis=axis)
        )
    if since:
        all_obs = [o for o in all_obs if o.observed_at and o.observed_at >= since]
    return all_obs


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        raise SystemExit("usage: parse.py path.xlsx [dataset_code] [axis]")
    p = Path(sys.argv[1])
    code = sys.argv[2] if len(sys.argv) > 2 else "EARN01"
    axis = sys.argv[3] if len(sys.argv) > 3 else "overall"
    obs = parse_earn_file(p, dataset_code=code, axis=axis)
    print(f"{len(obs)} monthly observations parsed from {p}")
