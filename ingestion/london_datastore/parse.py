"""Parse London Datastore earnings workbooks.

The Earnings by Borough workbooks contain one sheet per measure (e.g.
"Full-time", "Male FT", "Female FT", "Part-time"), with borough names
down column A and year columns across the top. Cells are median annual
gross pay in £ (workplace or residence based).

We emit POINT observations per (borough, year) with borough GSS codes
resolved from a lookup table, axis stashed in source_payload.
"""
from __future__ import annotations
import csv
import re
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


# London borough GSS codes (E090000XX). Updated to cover all 33 boroughs + City.
BOROUGH_CODES: dict[str, str] = {
    "BARKING AND DAGENHAM": "E09000002",
    "BARNET": "E09000003",
    "BEXLEY": "E09000004",
    "BRENT": "E09000005",
    "BROMLEY": "E09000006",
    "CAMDEN": "E09000007",
    "CITY OF LONDON": "E09000001",
    "CROYDON": "E09000008",
    "EALING": "E09000009",
    "ENFIELD": "E09000010",
    "GREENWICH": "E09000011",
    "HACKNEY": "E09000012",
    "HAMMERSMITH AND FULHAM": "E09000013",
    "HARINGEY": "E09000014",
    "HARROW": "E09000015",
    "HAVERING": "E09000016",
    "HILLINGDON": "E09000017",
    "HOUNSLOW": "E09000018",
    "ISLINGTON": "E09000019",
    "KENSINGTON AND CHELSEA": "E09000020",
    "KINGSTON UPON THAMES": "E09000021",
    "LAMBETH": "E09000022",
    "LEWISHAM": "E09000023",
    "MERTON": "E09000024",
    "NEWHAM": "E09000025",
    "REDBRIDGE": "E09000026",
    "RICHMOND UPON THAMES": "E09000027",
    "SOUTHWARK": "E09000028",
    "SUTTON": "E09000029",
    "TOWER HAMLETS": "E09000030",
    "WALTHAM FOREST": "E09000031",
    "WANDSWORTH": "E09000032",
    "WESTMINSTER": "E09000033",
    "LONDON": "E12000007",
    "INNER LONDON": "E13000001",
    "OUTER LONDON": "E13000002",
}


_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _borough_code(name: str) -> Optional[str]:
    key = name.upper().strip().replace("&", "AND")
    key = re.sub(r"\s+", " ", key)
    return BOROUGH_CODES.get(key)


def _safe_float(raw) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).replace(",", "").replace("£", "").strip()
    if not s or s in {"..", "-", "x", "*"}:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _year_cols(header_row: list) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for i, cell in enumerate(header_row):
        if cell is None:
            continue
        m = _YEAR_RE.search(str(cell))
        if m:
            out.append((i, int(m.group())))
    return out


def _build_observation(
    *,
    borough: str,
    year: int,
    value: float,
    axis: str,
    sheet_name: str,
    dataset_code: str,
) -> Optional[CompensationObservation]:
    code = _borough_code(borough)
    if code is None:
        return None
    ref = f"london_datastore:{dataset_code}:{code}:{sheet_name}:{year}"
    return CompensationObservation(
        source_id="london_datastore",
        source_reference=ref,
        occupation_code=None,
        location_code=code,
        company_ref=None,
        observation_type=ObservationType.POINT,
        value_amount=value,
        value_min=None,
        value_max=None,
        percentile=50,
        period=Period.ANNUAL,
        normalized_annual_amount=value,
        normalization_method_version=NORMALIZATION_VERSION,
        currency="GBP",
        experience_band=ExperienceBand.UNKNOWN,
        contract_type=ContractType.UNKNOWN,
        sample_size=None,
        total_comp_annual=None,
        observed_at=date(year, 12, 31),
        source_payload={
            "dataset": dataset_code,
            "axis": axis,
            "sheet": sheet_name,
            "borough": borough,
        },
    )


def _parse_xlsx(path: Path, *, dataset_code: str, axis: str) -> list[CompensationObservation]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out: list[CompensationObservation] = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2:
            continue

        # Find the first row with ≥3 year cells — that's the header row.
        header_idx = None
        year_cols: list[tuple[int, int]] = []
        for i, row in enumerate(rows[:25]):
            yc = _year_cols(list(row))
            if len(yc) >= 2:
                header_idx = i
                year_cols = yc
                break
        if header_idx is None:
            continue

        for row in rows[header_idx + 1 :]:
            if not row or row[0] is None:
                continue
            borough = str(row[0]).strip()
            for col_idx, year in year_cols:
                if col_idx >= len(row):
                    continue
                val = _safe_float(row[col_idx])
                if val is None or val < 5_000 or val > 250_000:
                    continue
                obs = _build_observation(
                    borough=borough,
                    year=year,
                    value=val,
                    axis=axis,
                    sheet_name=ws.title,
                    dataset_code=dataset_code,
                )
                if obs:
                    out.append(obs)
    return out


def _parse_csv(path: Path, *, dataset_code: str, axis: str) -> list[CompensationObservation]:
    out: list[CompensationObservation] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if len(rows) < 2:
        return out
    header = rows[0]
    year_cols = _year_cols(header)
    if not year_cols:
        return out
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        borough = row[0].strip()
        for col_idx, year in year_cols:
            if col_idx >= len(row):
                continue
            val = _safe_float(row[col_idx])
            if val is None or val < 5_000 or val > 250_000:
                continue
            obs = _build_observation(
                borough=borough,
                year=year,
                value=val,
                axis=axis,
                sheet_name=path.stem,
                dataset_code=dataset_code,
            )
            if obs:
                out.append(obs)
    return out


def parse_earnings_file(
    path: Path,
    *,
    dataset_code: str,
    axis: str,
) -> list[CompensationObservation]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return _parse_xlsx(path, dataset_code=dataset_code, axis=axis)
    if suffix == ".csv":
        return _parse_csv(path, dataset_code=dataset_code, axis=axis)
    raise ValueError(f"Unsupported file type for {path}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        raise SystemExit("usage: parse.py path [dataset_code] [axis]")
    p = Path(sys.argv[1])
    code = sys.argv[2] if len(sys.argv) > 2 else "EARN_WORKPLACE"
    axis = sys.argv[3] if len(sys.argv) > 3 else "workplace"
    obs = parse_earnings_file(p, dataset_code=code, axis=axis)
    print(f"{len(obs)} borough-year observations parsed from {p}")
