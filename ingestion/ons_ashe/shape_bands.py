"""Shape ASHE percentile distributions into experience-band variants using Reed ratios.

Strategy:
    1. From Reed listings, compute the median salary per (SOC code, experience band).
    2. For each SOC code with sufficient Reed data, compute the ratio:
           junior_ratio  = median(junior listings) / median(all tagged listings)
           mid_ratio     = median(mid listings)    / median(all tagged listings)
           senior_ratio  = median(senior listings)  / median(all tagged listings)
    3. Apply each ratio to every ASHE percentile for that SOC code to create
       synthetic band-specific observations.

The shaped observations are stored with source_id='ons_ashe_table2_shaped' and
confidence_weight=0.80 (lower than raw ASHE at 0.95, reflecting the added
assumption layer).

Minimum sample sizes:
    - Need >= 10 Reed listings in a band to compute its ratio
    - Need >= 20 total tagged listings per SOC to trust the overall median
    - Ratios are clamped to [0.50, 2.00] to prevent extreme outliers

Usage:
    python -m ingestion.ons_ashe.shape_bands                     # live
    python -m ingestion.ons_ashe.shape_bands --dry-run            # preview
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

from shared.config import load_env
load_env()

from shared.models import (
    CompensationObservation,
    ContractType,
    ExperienceBand,
    ObservationType,
    Period,
)
from shared.normalization import NORMALIZATION_VERSION, normalize_to_annual
from shared.soc_classifier import classify_title
from shared.db import bulk_upsert

# ── Constants ─────────────────────────────────────────────────────

SOURCE_ID = "ons_ashe_table2_shaped"
MIN_BAND_LISTINGS = 10    # minimum Reed listings to trust a band ratio
MIN_TOTAL_TAGGED = 20     # minimum total tagged listings per SOC
RATIO_FLOOR = 0.50
RATIO_CEILING = 2.00

# Experience band detection (same as Reed parser)
SENIOR_WORDS = r"\b(senior|sr\.?|principal|lead|head of|director|vp|staff|chief|cto|cfo|ceo)\b"
JUNIOR_WORDS = r"\b(junior|jr\.?|graduate|trainee|apprentice|intern|entry[- ]?level)\b"
MID_WORDS = r"\b(mid[- ]?level|associate)\b"

BAND_MAP = {
    "junior": ExperienceBand.JUNIOR,
    "mid": ExperienceBand.MID,
    "senior": ExperienceBand.SENIOR,
}


def _detect_band(title: str) -> Optional[str]:
    blob = title.lower()
    if re.search(SENIOR_WORDS, blob):
        return "senior"
    if re.search(JUNIOR_WORDS, blob):
        return "junior"
    if re.search(MID_WORDS, blob):
        return "mid"
    return None


# ── Step 1: Compute Reed ratios ──────────────────────────────────

def compute_reed_ratios(
    reed_dir: Path = Path("data/raw/reed"),
) -> dict[str, dict[str, float]]:
    """Return {soc_code: {band: ratio}} from Reed raw data.

    ratio = median(band listings) / median(all tagged listings for that SOC)
    """
    # Collect annual salaries per (soc, band)
    soc_band_salaries: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for page_file in sorted(reed_dir.rglob("page_*.json")):
        payload = json.loads(page_file.read_text())
        for r in (payload.get("results") or []):
            title = r.get("jobTitle", "")
            soc = classify_title(title)
            if not soc:
                continue
            band = _detect_band(title)
            if band is None:
                continue

            min_s = r.get("minimumSalary")
            max_s = r.get("maximumSalary")
            ref = min_s or max_s
            if not ref or ref == 0:
                continue
            ref = float(ref)
            if ref < 50:
                period = "hourly"
            elif ref < 2000:
                period = "daily"
            elif ref < 12000:
                period = "weekly"
            else:
                period = "annual"

            if min_s and max_s and min_s != max_s:
                annual = normalize_to_annual(
                    (float(min_s) + float(max_s)) / 2, period
                )
            else:
                annual = normalize_to_annual(ref, period)

            if annual and 10_000 < annual < 500_000:
                soc_band_salaries[soc][band].append(annual)

    # Compute ratios
    ratios: dict[str, dict[str, float]] = {}

    for soc, bands in soc_band_salaries.items():
        # Overall median across all tagged listings
        all_tagged = []
        for band_vals in bands.values():
            all_tagged.extend(band_vals)

        if len(all_tagged) < MIN_TOTAL_TAGGED:
            continue

        overall_median = statistics.median(all_tagged)
        if overall_median <= 0:
            continue

        soc_ratios: dict[str, float] = {}
        for band_name in ("junior", "mid", "senior"):
            vals = bands.get(band_name, [])
            if len(vals) < MIN_BAND_LISTINGS:
                continue
            band_median = statistics.median(vals)
            ratio = band_median / overall_median
            # Clamp
            ratio = max(RATIO_FLOOR, min(RATIO_CEILING, ratio))
            soc_ratios[band_name] = round(ratio, 4)

        if soc_ratios:
            ratios[soc] = soc_ratios

    return ratios


# ── Step 2: Load ASHE base observations ──────────────────────────

def load_ashe_base(
    ashe_dir: Path = Path("ONS occupation/ashetable22025provisional"),
) -> list[CompensationObservation]:
    """Load the annual-pay ASHE observations (base, unshaped)."""
    from ingestion.ons_ashe.parse import parse_workbook

    annual_file = None
    for f in ashe_dir.glob("*.xlsx"):
        if "Table 2.7a" in f.name and "Annual pay" in f.name:
            annual_file = f
            break

    if annual_file is None:
        raise FileNotFoundError("ASHE Table 2.7a annual pay file not found")

    return parse_workbook(annual_file)


# ── Step 3: Generate shaped observations ─────────────────────────

def generate_shaped_observations(
    ashe_obs: list[CompensationObservation],
    ratios: dict[str, dict[str, float]],
) -> list[CompensationObservation]:
    """Apply Reed ratios to ASHE observations, creating band-specific variants."""
    shaped: list[CompensationObservation] = []

    for obs in ashe_obs:
        soc = obs.occupation_code
        if soc is None:
            continue
        if soc not in ratios:
            continue
        # Only shape percentile observations (not mean/point)
        if obs.observation_type != ObservationType.PERCENTILE:
            continue
        if obs.percentile is None or obs.value_amount is None:
            continue

        for band_name, ratio in ratios[soc].items():
            shaped_value = round(obs.value_amount * ratio, 2)
            shaped_normalized = round(obs.normalized_annual_amount * ratio, 2)

            ref = (
                f"ashe_t2_shaped:{obs.source_payload.get('year', 2025)}:"
                f"annual:{soc}:p{obs.percentile}:{band_name}"
            )

            shaped.append(CompensationObservation(
                source_id=SOURCE_ID,
                source_reference=ref,
                occupation_code=soc,
                location_code=None,  # National
                company_ref=None,
                observation_type=ObservationType.PERCENTILE,
                value_amount=shaped_value,
                value_min=None,
                value_max=None,
                percentile=obs.percentile,
                period=Period.ANNUAL,
                normalized_annual_amount=shaped_normalized,
                normalization_method_version=NORMALIZATION_VERSION,
                currency="GBP",
                experience_band=BAND_MAP[band_name],
                contract_type=ContractType.PERMANENT,
                sample_size=obs.sample_size,
                total_comp_annual=None,
                observed_at=obs.observed_at,
                source_payload={
                    "method": "reed_ratio_shaping",
                    "base_source": "ons_ashe_table2",
                    "base_value": obs.value_amount,
                    "ratio": ratio,
                    "band": band_name,
                    "soc_code": soc,
                    "percentile": obs.percentile,
                    "year": obs.source_payload.get("year", 2025),
                },
            ))

    return shaped


# ── Main ─────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Shape ASHE data into experience bands using Reed ratios."
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    print("[shape_bands] Step 1: Computing Reed salary ratios...")
    ratios = compute_reed_ratios()
    print(f"[shape_bands] Ratios computed for {len(ratios)} SOC codes:")
    for soc in sorted(ratios.keys(), key=lambda x: (len(x), x)):
        parts = []
        for b in ("junior", "mid", "senior"):
            if b in ratios[soc]:
                parts.append(f"{b}={ratios[soc][b]:.2f}x")
        print(f"  SOC {soc}: {', '.join(parts)}")

    print("\n[shape_bands] Step 2: Loading ASHE base observations...")
    ashe_obs = load_ashe_base()
    annual_pctile = [
        o for o in ashe_obs
        if o.observation_type == ObservationType.PERCENTILE
    ]
    print(f"[shape_bands] {len(annual_pctile)} ASHE percentile observations loaded.")

    print("\n[shape_bands] Step 3: Generating shaped observations...")
    shaped = generate_shaped_observations(ashe_obs, ratios)
    print(f"[shape_bands] Generated {len(shaped)} shaped observations.")

    if not shaped:
        print("[shape_bands] No observations generated.")
        return 1

    # Show samples
    print("\n[shape_bands] Samples:")
    seen = set()
    for obs in shaped:
        key = (obs.occupation_code, obs.experience_band.value)
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > 8:
            break
        print(
            f"  SOC {obs.occupation_code} {obs.experience_band.value:>8}: "
            f"p{obs.percentile} = £{obs.value_amount:,.0f} "
            f"(base £{obs.source_payload['base_value']:,.0f} × {obs.source_payload['ratio']:.2f})"
        )

    if args.dry_run:
        print("\n[shape_bands] Dry run — no database changes.")
        return 0

    print(f"\n[shape_bands] Upserting {len(shaped)} observations...")
    written = bulk_upsert(shaped)
    print(f"[shape_bands] Done. Upserted {written} observations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
