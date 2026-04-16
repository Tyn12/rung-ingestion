"""Parse UCEA pay spine + provide a hand-curated fallback spine.

Two code paths:
    - parse_spine_xlsx(path, year)  → reads a UCEA Excel/PDF-extracted spreadsheet
    - seed_spine(year)              → returns the 2023/24 spine as a deterministic
                                      fallback so we can still populate rows while
                                      waiting for the current-year circular.

The seeded figures here are the 1 August 2023 spine (as at autumn 2023, the
last broadly-published numbers before uplift negotiations). Once a new year's
circular lands, update KNOWN_SPINES in fetch.py; parsing takes over automatically.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

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
class SeedSpine:
    point: int
    salary: float


# 1 August 2023 spine (publicly documented baseline). Update alongside KNOWN_SPINES.
BASELINE_SPINE_2023: tuple[SeedSpine, ...] = tuple(
    SeedSpine(p, s) for p, s in [
        ( 1, 21_285), ( 2, 21_630), ( 3, 21_947), ( 4, 22_214), ( 5, 22_681),
        ( 6, 23_144), ( 7, 23_700), ( 8, 24_248), ( 9, 24_948), (10, 25_742),
        (11, 26_444), (12, 27_181), (13, 27_979), (14, 28_759), (15, 29_605),
        (16, 30_487), (17, 31_396), (18, 32_332), (19, 33_309), (20, 34_308),
        (21, 35_333), (22, 36_024), (23, 37_099), (24, 38_205), (25, 39_347),
        (26, 40_521), (27, 41_732), (28, 42_978), (29, 44_263), (30, 45_585),
        (31, 46_974), (32, 48_350), (33, 49_794), (34, 51_805), (35, 53_353),
        (36, 54_949), (37, 56_587), (38, 58_285), (39, 60_022), (40, 61_823),
        (41, 63_668), (42, 65_578), (43, 67_540), (44, 69_566), (45, 71_655),
        (46, 73_803), (47, 76_019), (48, 78_300), (49, 80_647), (50, 83_068),
        (51, 85_560),
    ]
)


def _observation(point: int, salary: float, year_starting: int) -> CompensationObservation:
    if point <= 10:
        band = ExperienceBand.JUNIOR
    elif point <= 25:
        band = ExperienceBand.MID
    elif point <= 40:
        band = ExperienceBand.SENIOR
    else:
        band = ExperienceBand.PRINCIPAL
    return CompensationObservation(
        source_id="ucu_pay_spine",
        source_reference=f"ucu_spine:{year_starting}:sp{point:02d}",
        occupation_code=None,
        location_code="K02000001",
        company_ref="UK HEI",
        observation_type=ObservationType.POINT,
        value_amount=salary,
        value_min=None,
        value_max=None,
        percentile=None,
        period=Period.ANNUAL,
        normalized_annual_amount=salary,
        normalization_method_version=NORMALIZATION_VERSION,
        currency="GBP",
        experience_band=band,
        contract_type=ContractType.PERMANENT,
        sample_size=None,
        total_comp_annual=None,
        observed_at=date(year_starting, 8, 1),
        source_payload={"spine_point": point, "year_starting": year_starting},
    )


def seed_spine(year_starting: int) -> list[CompensationObservation]:
    return [_observation(s.point, s.salary, year_starting) for s in BASELINE_SPINE_2023]


def parse_spine_xlsx(path: Path, year_starting: int) -> list[CompensationObservation]:
    """Very forgiving spreadsheet parser: find the first sheet with point + salary columns."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out: list[CompensationObservation] = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        # Find a row with a "point" or "spine" header cell and a salary-ish header.
        header_idx = None
        for i, row in enumerate(rows):
            labels = [str(c).lower().strip() if c else "" for c in row]
            if any("spine" in l or "point" in l for l in labels) and any(
                "salary" in l or "£" in l or "pay" in l for l in labels
            ):
                header_idx = i
                break
        if header_idx is None:
            continue
        headers = [str(c).lower().strip() if c else "" for c in rows[header_idx]]
        point_col = next(
            (i for i, h in enumerate(headers) if "spine" in h or "point" in h), None
        )
        salary_col = next(
            (i for i, h in enumerate(headers) if "salary" in h or "pay" in h), None
        )
        if point_col is None or salary_col is None:
            continue
        for row in rows[header_idx + 1 :]:
            try:
                point = int(row[point_col])
                salary = float(str(row[salary_col]).replace(",", "").replace("£", "").strip())
            except (TypeError, ValueError, IndexError):
                continue
            if 1 <= point <= 60 and 10_000 <= salary <= 150_000:
                out.append(_observation(point, salary, year_starting))
        if out:
            return out
    return out


if __name__ == "__main__":
    import sys
    records = seed_spine(int(sys.argv[1]) if len(sys.argv) > 1 else 2023)
    print(f"{len(records)} spine points in seed for year {records[0].observed_at.year}")
