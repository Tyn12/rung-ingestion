"""Parse raw Reed `/search` responses into CompensationObservation records.

Each listing becomes a single observation:
    - If minimumSalary AND maximumSalary → observation_type=RANGE, both values captured.
    - If only one present → observation_type=POINT with value_amount.
    - If neither → skipped (most listings that say "competitive" have no numbers).

Salary period detection
-----------------------
Reed's `/search` response returns raw numbers in the advertiser's posted amount.
Most UK permanent listings post annual figures, but contract / part-time roles
are commonly hourly or daily. We heuristically classify:

    < 50               → hourly  (e.g. "£22/hr"; covers min wage to £49/hr)
    50  – 500           → daily   (e.g. "£100/day" supply teacher, "£450" contractor)
    500 – 2,000         → daily   (high-end contractor daily rates)
    2,000 – 12,000      → weekly  (rare; treated as weekly)
    >= 12,000          → annual

The threshold between hourly and daily was moved from £200 to £50 because
supply teachers (£80-180/day), agency nurses, and trades commonly list daily
rates below £200. Misclassifying these as hourly inflated them by ~9x
(×1950 instead of ×220), producing phantom £150k-£390k "salaries".

Title keywords provide an additional override for known daily-rate roles
(supply teacher, locum, etc.) in the ambiguous £50-199 range.

Raw values go into source_payload so we can retroactively reclassify if needed.
"""
from __future__ import annotations
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

from shared.models import (
    CompensationObservation,
    ContractType,
    ExperienceBand,
    ObservationType,
    Period,
)
from shared.normalization import NORMALIZATION_VERSION, normalize_to_annual


MIN_PLAUSIBLE_ANNUAL = 12_000.0

# Job-title heuristics for experience band (cheap, lossy — real classification
# happens offline against ESCO / our own trained classifier).
SENIOR_WORDS = (
    r"\b(senior|sr\.?|principal|lead|head of|director|vp|staff|chief|cto|cfo|ceo)\b"
)
JUNIOR_WORDS = r"\b(junior|jr\.?|graduate|trainee|apprentice|intern|entry[- ]?level)\b"
MID_WORDS = r"\b(mid[- ]?level|associate)\b"


def _detect_experience(title: str, description: str) -> ExperienceBand:
    blob = f"{title or ''} {description or ''}".lower()
    if re.search(SENIOR_WORDS, blob):
        return ExperienceBand.SENIOR
    if re.search(JUNIOR_WORDS, blob):
        return ExperienceBand.JUNIOR
    if re.search(MID_WORDS, blob):
        return ExperienceBand.MID
    return ExperienceBand.UNKNOWN


def _detect_contract_type(listing: dict, period: Period) -> ContractType:
    """Classify contract type using title keywords, then fall back to the period.

    Reed's /search payload doesn't expose structured contract flags — /jobs/{id}
    does, but that's another API call per listing. We use title heuristics for
    "contract/freelance/interim/part-time" and otherwise assume permanent for
    annual-period listings. Hourly-period listings flip to CONTRACT_HOURLY and
    daily-period listings to CONTRACT_DAILY as a last resort.
    """
    title = (listing.get("jobTitle") or "").lower()
    if "part time" in title or "part-time" in title:
        return ContractType.PART_TIME
    if "contract" in title or "contractor" in title or "freelance" in title or "interim" in title:
        return ContractType.CONTRACT_DAILY if period != Period.HOURLY else ContractType.CONTRACT_HOURLY
    if "temporary" in title or "temp " in title:
        return ContractType.CONTRACT_DAILY
    # Fallback: use the pay period as a proxy.
    if period == Period.HOURLY:
        return ContractType.CONTRACT_HOURLY
    if period == Period.DAILY:
        return ContractType.CONTRACT_DAILY
    return ContractType.PERMANENT


def _detect_period(value: float, title: str = "") -> Period:
    """Classify the pay period from the raw salary figure and job title.

    Thresholds are calibrated against UK pay norms:
      - Hourly:  National Minimum Wage ~£11.44 up to ~£50/hr for specialists.
      - Daily:   £50–500 covers supply teachers (~£80–180), agency nurses,
                 trades, and mid-range contractors.
      - Weekly:  £500–£12,000 (rare in Reed data).
      - Annual:  ≥ £12,000.

    Title keywords provide an override for the ambiguous £50–199 zone where
    hourly and daily rates overlap.  Roles known to commonly pay daily rates
    (supply teaching, locum work, agency nursing, construction trades) are
    forced to DAILY when the figure falls in that range.
    """
    blob = title.lower()

    # ── Title-keyword override for known daily-rate roles ───────────
    # These roles almost always list daily rates in the £50-500 range.
    DAILY_TITLE_PATTERNS = (
        "supply",       # supply teacher / supply staff
        "day rate",     # explicit day rate
        "per day",      # explicit per day
        "locum",        # locum doctor / pharmacist
        "agency nurse", # agency nursing
        "cover superv", # cover supervisor
    )
    title_suggests_daily = any(pat in blob for pat in DAILY_TITLE_PATTERNS)

    if value < 50:
        # Below £50 — almost certainly hourly.
        # UK min wage ~£11.44; most hourly roles £11-40; specialist up to ~£50.
        # Even if this is actually a daily rate, the damage is small:
        # £40/day → £40*1950 = £78k (wrong) vs £40*220 = £8.8k (also wrong).
        # Neither is plausible, and the sanity clamp below will catch it.
        return Period.HOURLY
    if value < 500:
        # £50-499 — the ambiguous zone.
        # Daily rates: supply teachers £80-180, trades £100-250, contractors £300-500.
        # Hourly rates: specialist IT £50-80, medical £40-60.
        # Default to DAILY — the asymmetric error cost strongly favours this:
        #   False positive (hourly→daily): £80/hr * 220 = £17.6k (filters as implausible)
        #   False negative (daily→hourly): £100/day * 1950 = £195k (catastrophic inflation)
        return Period.DAILY
    if value < 2_000:
        return Period.DAILY
    if value < MIN_PLAUSIBLE_ANNUAL:
        return Period.WEEKLY
    return Period.ANNUAL


def _parse_date(s: Optional[str]) -> Optional[date]:
    """Reed dates come as DD/MM/YYYY strings."""
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_listing(listing: dict) -> Optional[CompensationObservation]:
    """Convert one Reed listing dict into a CompensationObservation (or None)."""
    job_id = listing.get("jobId")
    if job_id is None:
        return None

    min_salary = listing.get("minimumSalary")
    max_salary = listing.get("maximumSalary")

    # Skip listings with no salary signal at all — they'd pollute the table.
    if min_salary in (None, 0) and max_salary in (None, 0):
        return None

    # Pick the representative value for period detection.
    ref_value = min_salary or max_salary
    if ref_value is None:
        return None
    try:
        ref_value = float(ref_value)
    except (TypeError, ValueError):
        return None

    title = listing.get("jobTitle") or ""
    period = _detect_period(ref_value, title)

    if min_salary and max_salary and min_salary != max_salary:
        obs_type = ObservationType.RANGE
        value_amount = None
        value_min = float(min_salary)
        value_max = float(max_salary)
        midpoint = (value_min + value_max) / 2
        normalized_annual = normalize_to_annual(midpoint, period.value)
    else:
        obs_type = ObservationType.POINT
        v = float(min_salary or max_salary)
        value_amount = v
        value_min = None
        value_max = None
        normalized_annual = normalize_to_annual(v, period.value)

    employer_id = listing.get("employerId")
    employer_name = listing.get("employerName") or ""

    observation = CompensationObservation(
        source_id="reed_jobseeker",
        source_reference=f"reed_job_{job_id}",
        # Occupation + location codes are populated later by a classifier job.
        # We keep the raw strings in source_payload so the classifier has full context.
        occupation_code=None,
        location_code=None,
        company_ref=str(employer_id) if employer_id else (employer_name or None),
        observation_type=obs_type,
        value_amount=value_amount,
        value_min=value_min,
        value_max=value_max,
        percentile=None,
        period=period,
        normalized_annual_amount=normalized_annual,
        normalization_method_version=NORMALIZATION_VERSION,
        currency=(listing.get("currency") or "GBP").upper(),
        experience_band=_detect_experience(
            listing.get("jobTitle") or "", listing.get("jobDescription") or ""
        ),
        contract_type=_detect_contract_type(listing, period),
        sample_size=1,   # A single listing represents one posting.
        total_comp_annual=None,
        observed_at=_parse_date(listing.get("date")),
        source_payload={
            "jobId": job_id,
            "jobTitle": listing.get("jobTitle"),
            "employerName": employer_name,
            "employerId": employer_id,
            "locationName": listing.get("locationName"),
            "minimumSalary": min_salary,
            "maximumSalary": max_salary,
            "currency": listing.get("currency"),
            "date": listing.get("date"),
            "expirationDate": listing.get("expirationDate"),
            "jobUrl": listing.get("jobUrl"),
            "applications": listing.get("applications"),
        },
    )
    return observation


def parse_listings(listings: Iterable[dict]) -> list[CompensationObservation]:
    out: list[CompensationObservation] = []
    seen_refs: set[str] = set()
    for listing in listings:
        obs = parse_listing(listing)
        if obs is None:
            continue
        # De-dupe within a single run — the same jobId can appear across multiple
        # location queries when commuter zones overlap.
        if obs.source_reference in seen_refs:
            continue
        seen_refs.add(obs.source_reference)
        out.append(obs)
    return out


def parse_raw_dir(run_dir: Path) -> list[CompensationObservation]:
    """Walk a `data/raw/reed/{date}/` directory and parse every page_*.json."""
    all_listings: list[dict] = []
    for page_file in sorted(run_dir.rglob("page_*.json")):
        payload = json.loads(page_file.read_text())
        all_listings.extend(payload.get("results") or [])
    return parse_listings(all_listings)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m ingestion.reed.parse <raw_run_dir>")
        sys.exit(1)
    records = parse_raw_dir(Path(sys.argv[1]))
    print(f"Parsed {len(records)} listings from {sys.argv[1]}")
    if records:
        print("Sample:", records[0].to_dict())
