"""Parse HMRC SPI income-distribution tables and provide a seeded fallback.

We emit PERCENTILE observations (P10, P25, P50, P75, P90, P95, P99) so the
Underpaid Detector can compare a user salary against the tax-return-based
national earnings distribution.

The seeded baseline here is the 2020-21 tax year (published 2023) — the
last broadly-circulated SPI before the prompt knowledge cutoff. Update
KNOWN_SPI_URLS in fetch.py as newer tables are released and the parser
will take over automatically.
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
class SpiPercentile:
    percentile: int
    value_gbp: float


# 2020-21 tax year — Total income percentiles (all taxpayers, HMRC SPI
# Table 3.1a, published 2023). Numbers rounded for seed use.
BASELINE_TOTAL_INCOME_2020_21: tuple[SpiPercentile, ...] = (
    SpiPercentile(10, 12_900),
    SpiPercentile(25, 17_100),
    SpiPercentile(50, 25_600),
    SpiPercentile(75, 39_200),
    SpiPercentile(90, 58_100),
    SpiPercentile(95, 76_500),
    SpiPercentile(99, 180_000),
)


def _observation(
    pct: SpiPercentile,
    tax_year_ending: int,
) -> CompensationObservation:
    ref = f"hmrc_spi:total_income:{tax_year_ending}:p{pct.percentile:02d}"
    return CompensationObservation(
        source_id="hmrc_spi",
        source_reference=ref,
        occupation_code=None,
        location_code="K02000001",
        company_ref=None,
        observation_type=ObservationType.PERCENTILE,
        value_amount=pct.value_gbp,
        value_min=None,
        value_max=None,
        percentile=pct.percentile,
        period=Period.ANNUAL,
        normalized_annual_amount=pct.value_gbp,
        normalization_method_version=NORMALIZATION_VERSION,
        currency="GBP",
        experience_band=ExperienceBand.UNKNOWN,
        contract_type=ContractType.UNKNOWN,
        sample_size=None,
        total_comp_annual=None,
        observed_at=date(tax_year_ending, 4, 5),  # UK tax year end
        source_payload={
            "tax_year_ending": tax_year_ending,
            "measure": "total_income_all_taxpayers",
        },
    )


def seed_percentiles(tax_year_ending: int = 2021) -> list[CompensationObservation]:
    return [_observation(p, tax_year_ending) for p in BASELINE_TOTAL_INCOME_2020_21]


def _safe_float(raw) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").replace("£", "").strip())
    except (TypeError, ValueError):
        return None


def parse_spi_xlsx(path: Path, tax_year_ending: int) -> list[CompensationObservation]:
    """Forgiving parser: look for a percentile-value pair table in any sheet.

    Heuristic: first row containing both a 'percentile' label and a numeric
    header row below with percentile values 10/25/50/75/90/95/99.
    """
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    expected_pcts = {10, 25, 50, 75, 90, 95, 99}
    out: list[CompensationObservation] = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        for row in rows:
            if not row:
                continue
            numeric = []
            for cell in row:
                try:
                    numeric.append(int(float(str(cell).strip().strip("%"))))
                except (TypeError, ValueError):
                    numeric.append(None)
            if sum(1 for n in numeric if n in expected_pcts) >= 4:
                # This row indexes percentile columns; the following row(s)
                # contain the matching £ values. Look up to 5 rows ahead for
                # a numeric row with the same cardinality.
                col_pcts = {i: n for i, n in enumerate(numeric) if n in expected_pcts}
                start_idx = rows.index(row)
                for below in rows[start_idx + 1 : start_idx + 10]:
                    if not below:
                        continue
                    vals = {i: _safe_float(below[i]) for i in col_pcts if i < len(below)}
                    # Accept the first row where all mapped percentile cells
                    # resolve to sane money values.
                    if vals and all(v and 1_000 <= v <= 5_000_000 for v in vals.values()):
                        for col_idx, pct in col_pcts.items():
                            out.append(
                                _observation(
                                    SpiPercentile(pct, vals[col_idx]),
                                    tax_year_ending,
                                )
                            )
                        return out
    return out


if __name__ == "__main__":
    import sys
    records = seed_percentiles(int(sys.argv[1]) if len(sys.argv) > 1 else 2021)
    print(f"{len(records)} percentile observations seeded for TY ending {records[0].observed_at.year}")
