"""Parse ONS / HMRC PAYE RTI XLSX workbooks into CompensationObservation rows.

Each workbook tends to have multiple sheets: a cover/contents sheet followed by
data sheets whose titles describe the cut (industry, region, age, etc.). The
data sheets are wide — columns are usually months (YYYY-MM), rows are categories
like a SIC code or NUTS region name.

This parser:
    - Opens every XLSX under a given run dir
    - Skips obvious non-data sheets (cover/contents/notes/methodology)
    - Melts wide-format month columns into long-format observations
    - Classifies the axis of each sheet (industry / region / age) by inspecting
      the first data column's values
    - Emits one CompensationObservation per (sheet, row, month) cell

Because ONS workbooks have gentle drift in layout between months, we're
deliberately forgiving: unknown sheets are logged and skipped rather than
causing the whole run to fail.
"""
from __future__ import annotations
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
from shared.normalization import NORMALIZATION_VERSION, normalize_to_annual

SKIP_SHEET_PATTERNS = (
    r"cover",
    r"contents",
    r"notes?",
    r"metadata",
    r"methodology",
    r"definitions?",
    r"^info",
)

MONTH_HEADER = re.compile(r"^(19|20)\d{2}[- /]?\d{2}$")   # 2024-05, 2024 05, etc.


def _is_data_sheet(name: str) -> bool:
    low = name.lower().strip()
    for pat in SKIP_SHEET_PATTERNS:
        if re.search(pat, low):
            return False
    return True


def _month_to_date(cell_value) -> Optional[date]:
    if isinstance(cell_value, date):
        return cell_value
    s = str(cell_value).strip()
    if MONTH_HEADER.match(s):
        y, m = re.split(r"[- /]", s)
        return date(int(y), int(m), 1)
    # ONS sometimes uses "May 2024"
    m = re.match(r"([A-Za-z]+)\s+(\d{4})$", s)
    if m:
        month_name, year = m.groups()
        try:
            return date(int(year), _month_number(month_name), 1)
        except (KeyError, ValueError):
            return None
    return None


_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month_number(name: str) -> int:
    n = _MONTH_NAMES.get(name.lower().strip())
    if n is None:
        raise ValueError(f"Unknown month {name}")
    return n


def _classify_sheet(name: str) -> dict:
    low = name.lower()
    out = {"axis": "unknown", "source_key": name}
    if "industry" in low or "sic" in low:
        out["axis"] = "industry"
    elif "region" in low or "country" in low or "uk" in low:
        out["axis"] = "region"
    elif "age" in low:
        out["axis"] = "age"
    elif "sex" in low or "gender" in low:
        # We collect anyway but won't expose in the app; kept for validation only.
        out["axis"] = "sex"
    return out


def _iter_rows(ws) -> Iterable[tuple[list, list]]:
    """Yield (header_row, data_row) pairs from a worksheet.

    ONS sheets often have 4-8 rows of preamble before the real header row.
    We find the header by looking for the first row where at least 2 cells
    parse as month dates.
    """
    rows = list(ws.iter_rows(values_only=True))
    header_row = None
    for i, row in enumerate(rows):
        month_count = sum(1 for c in row if _month_to_date(c) is not None)
        if month_count >= 2:
            header_row = i
            break
    if header_row is None:
        return
    header = list(rows[header_row])
    for row in rows[header_row + 1 :]:
        if not any(c not in (None, "") for c in row):
            continue
        yield header, list(row)


def parse_workbook(path: Path) -> list[CompensationObservation]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out: list[CompensationObservation] = []
    for sheet_name in wb.sheetnames:
        if not _is_data_sheet(sheet_name):
            continue
        meta = _classify_sheet(sheet_name)
        ws = wb[sheet_name]
        rows = list(_iter_rows(ws))
        if not rows:
            continue
        header = rows[0][0]
        # First non-date column in header = category label (industry name, region, age band)
        for row_header, row in rows:
            label = None
            for c in row:
                if c is None or c == "":
                    continue
                if _month_to_date(c) is None and not isinstance(c, (int, float)):
                    label = str(c).strip()
                    break
            if not label:
                continue
            for col_idx, month_cell in enumerate(row_header):
                obs_date = _month_to_date(month_cell)
                if obs_date is None:
                    continue
                if col_idx >= len(row):
                    continue
                raw = row[col_idx]
                if raw in (None, "", "..", "x", "z", ":"):
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                # PAYE pay figures are published as MONTHLY median pounds.
                # Convert monthly → annual by × 12.
                normalized = value * 12
                out.append(CompensationObservation(
                    source_id="hmrc_paye_rti",
                    source_reference=f"{path.stem}:{sheet_name}:{label}:{obs_date.isoformat()}",
                    occupation_code=None,
                    location_code=label if meta["axis"] == "region" else None,
                    company_ref=None,
                    observation_type=ObservationType.PERCENTILE,
                    value_amount=value,
                    value_min=None,
                    value_max=None,
                    percentile=50,                  # ONS PAYE headline series are medians
                    period=Period.ANNUAL,           # We're storing the annualized figure
                    normalized_annual_amount=normalized,
                    normalization_method_version=NORMALIZATION_VERSION,
                    currency="GBP",
                    experience_band=ExperienceBand.UNKNOWN,
                    contract_type=ContractType.UNKNOWN,
                    sample_size=None,
                    total_comp_annual=None,
                    observed_at=obs_date,
                    source_payload={
                        "workbook": path.name,
                        "sheet": sheet_name,
                        "axis": meta["axis"],
                        "category": label,
                        "month": obs_date.isoformat(),
                        "monthly_median_pay": value,
                    },
                ))
    return out


def parse_run_dir(run_dir: Path) -> list[CompensationObservation]:
    results: list[CompensationObservation] = []
    for xlsx in sorted(run_dir.glob("*.xlsx")):
        try:
            results.extend(parse_workbook(xlsx))
        except Exception as e:
            print(f"[hmrc_paye:parse] Skipping {xlsx.name}: {e}")
    return results


if __name__ == "__main__":
    import sys
    records = parse_run_dir(Path(sys.argv[1]))
    print(f"Parsed {len(records)} observations.")
