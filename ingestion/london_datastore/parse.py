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
import xlrd

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
    if not s or s in {"..", "-", "x", "*", "#", ".."}:
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


def _detect_sheet_type(sheet_name: str) -> Optional[str]:
    """Classify the sheet. Returns 'weekly' or 'annual' or None to skip."""
    low = sheet_name.lower()
    if low in ("metadata",):
        return None
    if "hourly" in low:
        return None  # skip hourly sheets — values too granular for our model
    if "annual" in low:
        return "annual"
    if "weekly" in low or "workers" in low:
        return "weekly"
    return "weekly"  # default assumption for unrecognised data sheets


def _parse_rows(
    rows: list[list],
    *,
    dataset_code: str,
    axis: str,
    sheet_name: str,
    sheet_type: str,
) -> list[CompensationObservation]:
    """Parse rows from a single sheet (works for both openpyxl and xlrd data)."""
    out: list[CompensationObservation] = []

    # Find the header row with years
    header_idx = None
    year_cols: list[tuple[int, int]] = []
    for i, row in enumerate(rows[:25]):
        yc = _year_cols(list(row))
        if len(yc) >= 2:
            header_idx = i
            year_cols = yc
            break
    if header_idx is None:
        return out

    # Determine which column has the borough name vs GSS code.
    # If col 0 starts with 'E0' (GSS code), name is in col 1.
    first_data_row = None
    for row in rows[header_idx + 1:]:
        if row and row[0] is not None and str(row[0]).strip():
            first_data_row = row
            break
    if first_data_row is None:
        return out

    col0_val = str(first_data_row[0]).strip()
    if re.match(r"E\d{8}", col0_val):
        # GSS code in col 0, name in col 1
        gss_col = 0
        name_col = 1
    elif re.match(r"\d{2}[A-Z]{2}", col0_val):
        # Old-style council code in col 0 (e.g. 00AB), name in col 1
        gss_col = None
        name_col = 1
    else:
        gss_col = None
        name_col = 0

    # Weekly sheets have alternating year/conf% columns — skip conf% columns.
    # The year_cols list already has only the year columns, so we're fine.

    # Set value bounds based on sheet type
    if sheet_type == "annual":
        min_val, max_val = 5_000, 250_000
    else:
        # Weekly: median gross weekly ~£300-£2000
        min_val, max_val = 50, 10_000

    for row in rows[header_idx + 1:]:
        if not row or row[name_col] is None:
            continue

        # Skip blank or sub-header rows
        borough = str(row[name_col]).strip()
        if not borough or borough.lower() in ("code", "area", "date"):
            continue

        # Get GSS code directly if available, else look up by name
        if gss_col is not None and gss_col < len(row) and row[gss_col]:
            gss_code = str(row[gss_col]).strip()
            if not re.match(r"E\d{8}", gss_code):
                gss_code = _borough_code(borough)
        else:
            gss_code = _borough_code(borough)

        if gss_code is None:
            continue

        for col_idx, year in year_cols:
            if col_idx >= len(row):
                continue
            val = _safe_float(row[col_idx])
            if val is None or val < min_val or val > max_val:
                continue

            # Normalise weekly to annual
            if sheet_type == "weekly":
                annual_value = val * 52
            else:
                annual_value = val

            ref = f"london_datastore:{dataset_code}:{gss_code}:{sheet_name}:{year}"
            obs = CompensationObservation(
                source_id="london_datastore",
                source_reference=ref,
                occupation_code=None,
                location_code=gss_code,
                company_ref=None,
                observation_type=ObservationType.POINT,
                value_amount=val,
                value_min=None,
                value_max=None,
                percentile=50,
                period=Period.WEEKLY if sheet_type == "weekly" else Period.ANNUAL,
                normalized_annual_amount=annual_value,
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
                    "sheet_type": sheet_type,
                },
            )
            out.append(obs)

    return out


def _parse_xlsx(path: Path, *, dataset_code: str, axis: str) -> list[CompensationObservation]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out: list[CompensationObservation] = []
    for ws in wb.worksheets:
        sheet_type = _detect_sheet_type(ws.title)
        if sheet_type is None:
            continue
        rows = [list(row) for row in ws.iter_rows(values_only=True)]
        if len(rows) < 2:
            continue
        out.extend(_parse_rows(
            rows,
            dataset_code=dataset_code,
            axis=axis,
            sheet_name=ws.title,
            sheet_type=sheet_type,
        ))
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


def _parse_xls(path: Path, *, dataset_code: str, axis: str) -> list[CompensationObservation]:
    """Parse legacy .xls files using xlrd."""
    wb = xlrd.open_workbook(str(path))
    out: list[CompensationObservation] = []
    for ws in wb.sheets():
        sheet_type = _detect_sheet_type(ws.name)
        if sheet_type is None:
            continue
        rows = []
        for rx in range(ws.nrows):
            rows.append([ws.cell_value(rx, cx) for cx in range(ws.ncols)])
        if len(rows) < 2:
            continue
        out.extend(_parse_rows(
            rows,
            dataset_code=dataset_code,
            axis=axis,
            sheet_name=ws.name,
            sheet_type=sheet_type,
        ))
    return out


def parse_earnings_file(
    path: Path,
    *,
    dataset_code: str,
    axis: str,
) -> list[CompensationObservation]:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return _parse_xlsx(path, dataset_code=dataset_code, axis=axis)
    if suffix == ".xls":
        return _parse_xls(path, dataset_code=dataset_code, axis=axis)
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
