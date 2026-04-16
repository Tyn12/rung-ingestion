"""Parse Local Government Transparency Code senior salary CSV/XLSX files.

The DLUHC guidance standardises these columns (with variance in spelling):
    Post Unique Reference       → unique ID per post
    Post Title / Job Title      → role description
    Name / Post Holder          → who sits in the post (often blank / "Vacant")
    Grade                       → internal pay grade label
    Salary                      → exact annual salary, OR ...
    Salary Range From           → salary band floor
    Salary Range To             → salary band ceiling
    Bonus                       → bonuses
    Expense Allowances          → allowances
    Total Remuneration          → total package

We emit one observation per post. Where both an exact salary and a range
are available, we prefer the exact salary as a POINT observation and still
stash the range into source_payload.
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


_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "title":    ("post title", "job title", "title", "role title"),
    "grade":    ("grade", "pay grade", "level"),
    "salary":   ("salary", "actual salary", "annual salary", "basic salary"),
    "salary_from": ("salary range from", "salary from", "salary min", "band min", "min salary"),
    "salary_to":   ("salary range to", "salary to", "salary max", "band max", "max salary"),
    "total":    ("total remuneration", "total pay", "total package"),
    "unique":   ("post unique reference", "post id", "unique reference", "reference"),
    "name":     ("name", "post holder"),
}


def _normalize(raw: str) -> str:
    return re.sub(r"\s+", " ", str(raw).lower().strip())


def _resolve_columns(headers: list[str]) -> dict[str, int]:
    idx: dict[str, int] = {}
    norm = [_normalize(h) for h in headers]
    for key, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            for i, h in enumerate(norm):
                if alias == h or alias in h:
                    idx[key] = i
                    break
            if key in idx:
                break
    return idx


def _money(raw) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).replace(",", "").replace("£", "").strip()
    if not s or s.lower() in {"n/a", "na", "not applicable", "-", "vacant"}:
        return None
    # Some councils prefix "c.£" or append "pa".
    s = re.sub(r"[a-zA-Z\.]+", "", s).strip()
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _infer_band(title: str, grade: str) -> ExperienceBand:
    blob = f"{title} {grade}".lower()
    if any(t in blob for t in ("chief executive", "head of paid service", "chief exec")):
        return ExperienceBand.DIRECTOR
    if any(t in blob for t in ("director", "assistant chief executive")):
        return ExperienceBand.DIRECTOR
    if any(t in blob for t in ("deputy director", "head of")):
        return ExperienceBand.LEAD
    if any(t in blob for t in ("manager", "senior", "principal")):
        return ExperienceBand.SENIOR
    return ExperienceBand.MID


def _build_observation(
    *,
    cols: dict[str, int],
    row: list,
    council_code: str,
    council_name: str,
    gss: Optional[str],
    observed_year: int,
    row_idx: int,
) -> Optional[CompensationObservation]:
    def _col(key: str) -> str:
        idx = cols.get(key)
        if idx is None or idx >= len(row) or row[idx] is None:
            return ""
        return str(row[idx]).strip()

    title = _col("title")
    grade = _col("grade")
    salary = _money(row[cols["salary"]]) if "salary" in cols and cols["salary"] < len(row) else None
    s_from = _money(row[cols["salary_from"]]) if "salary_from" in cols and cols["salary_from"] < len(row) else None
    s_to   = _money(row[cols["salary_to"]])   if "salary_to"   in cols and cols["salary_to"]   < len(row) else None
    total  = _money(row[cols["total"]])       if "total"       in cols and cols["total"]       < len(row) else None
    unique = _col("unique") or f"row{row_idx}"

    if title == "" and salary is None and s_from is None:
        return None

    if salary is not None:
        obs_type = ObservationType.POINT
        value, vmin, vmax = salary, None, None
    elif s_from is not None and s_to is not None and s_from <= s_to:
        obs_type = ObservationType.RANGE
        midpoint = (s_from + s_to) / 2
        value, vmin, vmax = midpoint, s_from, s_to
    elif s_from is not None:
        # Single-point band minimum only → treat as POINT at the minimum.
        obs_type = ObservationType.POINT
        value, vmin, vmax = s_from, None, None
    else:
        return None

    ref = f"local_gov:{council_code}:{observed_year}:{unique}"
    return CompensationObservation(
        source_id="local_gov_transparency",
        source_reference=ref,
        occupation_code=None,
        location_code=gss,
        company_ref=council_name,
        observation_type=obs_type,
        value_amount=value,
        value_min=vmin,
        value_max=vmax,
        percentile=None,
        period=Period.ANNUAL,
        normalized_annual_amount=value,
        normalization_method_version=NORMALIZATION_VERSION,
        currency="GBP",
        experience_band=_infer_band(title, grade),
        contract_type=ContractType.PERMANENT,
        sample_size=None,
        total_comp_annual=total,
        observed_at=date(observed_year, 3, 31),  # UK council financial year end
        source_payload={
            "council_code": council_code,
            "council_name": council_name,
            "post_title": title,
            "grade": grade,
            "salary_range_from": s_from,
            "salary_range_to": s_to,
            "unique_reference": unique,
        },
    )


def _iter_csv_rows(path: Path):
    # Councils publish in various encodings; try utf-8 first, fall back to cp1252.
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with path.open("r", encoding=enc, newline="") as f:
                reader = csv.reader(f)
                return list(reader)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not decode {path}")


def _iter_xlsx_rows(path: Path):
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb.worksheets[0]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def parse_senior_salaries_csv(
    path: Path,
    *,
    council_code: str,
    council_name: str,
    gss_code: Optional[str],
    observed_year: int,
) -> list[CompensationObservation]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        rows = _iter_xlsx_rows(path)
    else:
        rows = _iter_csv_rows(path)
    if not rows:
        return []

    # Find the header row — the first row with at least 3 recognised aliases.
    header_idx = None
    for i, row in enumerate(rows[:20]):
        cols = _resolve_columns([str(c) if c is not None else "" for c in row])
        if len(cols) >= 3 and ("salary" in cols or "salary_from" in cols):
            header_idx = i
            break
    if header_idx is None:
        return []

    cols = _resolve_columns([str(c) if c is not None else "" for c in rows[header_idx]])
    out: list[CompensationObservation] = []
    for ri, row in enumerate(rows[header_idx + 1 :], start=header_idx + 1):
        if not row:
            continue
        obs = _build_observation(
            cols=cols,
            row=list(row),
            council_code=council_code,
            council_name=council_name,
            gss=gss_code,
            observed_year=observed_year,
            row_idx=ri,
        )
        if obs:
            out.append(obs)
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 5:
        raise SystemExit(
            "usage: parse.py path council_code council_name gss_code year"
        )
    rows = parse_senior_salaries_csv(
        Path(sys.argv[1]),
        council_code=sys.argv[2],
        council_name=sys.argv[3],
        gss_code=sys.argv[4],
        observed_year=int(sys.argv[5]) if len(sys.argv) > 5 else 2024,
    )
    print(f"{len(rows)} senior-salary rows parsed from {sys.argv[1]}")
