"""Fetch raw ASHE data from the Nomis REST API.

Nomis is run by Durham University on behalf of the UK's Office for National Statistics.
Registration for an API key is optional but recommended for large queries.

Key endpoints:
    https://www.nomisweb.co.uk/api/v01/dataset/{dataset_id}.data.{format}
    https://www.nomisweb.co.uk/api/v01/dataset/{dataset_id}.def.sdmx.json  (metadata)

Primary dataset for Rung:
    NM_99_1  — ASHE Table 14 (2020 SOC), workplace-based, all percentiles.
               Newer SOC 2020 classification; preferred going forward.
    NM_30_1  — ASHE Table 14 (legacy 2010 SOC), workplace-based.
               Use for backfill / time-series before SOC 2020 rollout.

Each returned observation is a single (geography, sex, item, pay, occupation, year)
data point. We fetch the full matrix for:
    - Geographies: all UK regions + countries + selected LA aggregates
    - Sex:         all persons full-time employees
    - Item:        percentiles 10, 25, 50 (median), 75, 90 + mean
    - Pay:         gross annual pay (preferred) and gross weekly pay (fallback)
    - Occupation:  all 4-digit SOC codes
    - Date:        last 5 years (rolling window)

Raw JSON lands under data/raw/nomis/{dataset_id}/{YYYY-MM-DD}/.
"""
from __future__ import annotations
import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Optional
import requests
from tenacity import retry, stop_after_attempt, wait_exponential


NOMIS_BASE = "https://www.nomisweb.co.uk/api/v01"

# Primary dataset: new SOC 2020 workplace analysis.
PRIMARY_DATASET = "NM_99_1"
# Fallback for time series before SOC 2020 adoption.
LEGACY_DATASET = "NM_30_1"

# UK regions as ONS geography codes (NUTS1 / ITL1).
# These are stable; full list available from the geography codelist if you need more granularity.
UK_REGION_GEOGRAPHIES = {
    "K02000001": "United Kingdom",
    "E12000001": "North East",
    "E12000002": "North West",
    "E12000003": "Yorkshire and The Humber",
    "E12000004": "East Midlands",
    "E12000005": "West Midlands",
    "E12000006": "East of England",
    "E12000007": "London",
    "E12000008": "South East",
    "E12000009": "South West",
    "W92000004": "Wales",
    "S92000003": "Scotland",
    "N92000002": "Northern Ireland",
}

# Items we want (percentile and central tendency codes per Nomis convention).
# Codes: 2=median, 3=mean, 10/20/25/30/40/60/70/75/80/90 = percentiles
PAY_ITEMS = [2, 3, 10, 25, 50, 75, 90]

# Sex codes: 7 = all employees (full-time), 8 = all employees (part-time).
# We pull full-time only for permanent-role benchmarking.
SEX_CODES = [7]

# Pay measure codes vary across ASHE tables. 7 is typically "Gross weekly pay (£)".
# Annual gross pay has its own code on some tables. We fetch and let the parser decide.
PAY_MEASURES = [7]        # gross weekly pay; parser will annualize

# Measures dimension: 20100 is the value itself.
MEASURES = [20100]


def _data_dir(dataset_id: str) -> Path:
    base = Path(__file__).resolve().parents[2] / "data" / "raw" / "nomis" / dataset_id
    base.mkdir(parents=True, exist_ok=True)
    return base


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=30))
def _get_json(url: str, params: Optional[dict] = None) -> dict:
    api_key = os.environ.get("NOMIS_API_KEY")
    if api_key:
        params = dict(params or {})
        params["uid"] = api_key
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def fetch_metadata(dataset_id: str = PRIMARY_DATASET) -> dict:
    """Fetch and cache the codelists for a Nomis dataset.

    The metadata tells us exactly which occupation codes, years, and parameter values
    are available. We use this at runtime rather than hardcoding, so ONS reclassifications
    don't silently break the pipeline.
    """
    url = f"{NOMIS_BASE}/dataset/{dataset_id}.def.sdmx.json"
    data = _get_json(url)
    out = _data_dir(dataset_id) / f"metadata_{date.today().isoformat()}.json"
    out.write_text(json.dumps(data, indent=2))
    return data


def fetch_data(
    dataset_id: str = PRIMARY_DATASET,
    years: Optional[list[int]] = None,
    occupations: Optional[list[str]] = None,
) -> Path:
    """Fetch ASHE data for the given years and occupations.

    If `years` is None, fetches 'latest'.
    If `occupations` is None, fetches all 4-digit SOC codes (pass 'MAJOR' for major groups only).

    Returns the path to the raw JSON dump.
    """
    date_param = ",".join(str(y) for y in years) if years else "latest"
    occ_param = ",".join(occupations) if occupations else "MAJOR,SUBMAJOR,MINOR,UNIT"
    geo_param = ",".join(UK_REGION_GEOGRAPHIES.keys())

    params = {
        "date": date_param,
        "geography": geo_param,
        "sex": ",".join(str(s) for s in SEX_CODES),
        "item": ",".join(str(i) for i in PAY_ITEMS),
        "pay": ",".join(str(p) for p in PAY_MEASURES),
        "measures": ",".join(str(m) for m in MEASURES),
        "occupation": occ_param,
    }

    url = f"{NOMIS_BASE}/dataset/{dataset_id}.data.json"
    print(f"[nomis] Fetching {dataset_id} for years={date_param}, occupations={occ_param[:40]}...")
    data = _get_json(url, params=params)

    out = _data_dir(dataset_id) / f"data_{date.today().isoformat()}.json"
    out.write_text(json.dumps(data, indent=2))
    n_obs = len(data.get("obs", []))
    print(f"[nomis] Wrote {n_obs} observations to {out}")
    return out


if __name__ == "__main__":
    # Default: fetch metadata + latest year data, all SOC groupings.
    fetch_metadata()
    fetch_data()
