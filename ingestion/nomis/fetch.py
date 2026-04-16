"""Fetch raw ASHE data from the Nomis REST API (CSV format).

Nomis is run by Durham University on behalf of the UK's Office for National Statistics.
Registration for an API key is optional but recommended for large queries.

Key endpoints:
    https://www.nomisweb.co.uk/api/v01/dataset/{dataset_id}.data.csv
    https://www.nomisweb.co.uk/api/v01/dataset/{dataset_id}.def.sdmx.json  (metadata)

Primary dataset for Rung:
    NM_99_1  — ASHE Table 14 (2020 SOC), workplace-based, all percentiles.
               Newer SOC 2020 classification; preferred going forward.
    NM_30_1  — ASHE Table 14 (legacy 2010 SOC), workplace-based.
               Use for backfill / time-series before SOC 2020 rollout.

We fetch CSV format (not JSON) because the Nomis JSON endpoint returns SDMX-JSON
whose nested structure varies between datasets. CSV is flat, stable, and well
documented.

Each returned row is a single (geography, sex, item, pay, occupation, year)
data point. We fetch the full matrix for:
    - Geographies: all UK regions + countries
    - Sex:         full-time employees (code 7)
    - Item:        percentiles 10, 25, median, 75, 90 + mean
    - Pay:         gross weekly pay (code 7)
    - Occupation:  all occupation codes available in the dataset
    - Date:        last 5 years or 'latest'

Raw CSV lands under data/raw/nomis/{dataset_id}/.
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

# Items we want (Nomis item dimension codes).
# 2=median, 3=mean, 10=P10, 25=P25, 75=P75, 90=P90
PAY_ITEMS = [2, 3, 10, 25, 75, 90]

# Sex codes: 7 = full-time employees.
SEX_CODES = [7]

# Pay measure: 7 = gross weekly pay (£).
PAY_MEASURES = [7]

# Measures dimension: 20100 is the value itself.
MEASURES = [20100]


def _data_dir(dataset_id: str) -> Path:
    base = Path("data/raw/nomis") / dataset_id
    base.mkdir(parents=True, exist_ok=True)
    return base


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=30))
def _get(url: str, params: Optional[dict] = None) -> requests.Response:
    api_key = os.environ.get("NOMIS_API_KEY")
    if api_key:
        params = dict(params or {})
        params["uid"] = api_key
    resp = requests.get(
        url,
        params=params,
        headers={"User-Agent": "Rung/0.1 (+ingestion)"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp


def fetch_metadata(dataset_id: str = PRIMARY_DATASET) -> dict:
    """Fetch and cache the codelists for a Nomis dataset."""
    url = f"{NOMIS_BASE}/dataset/{dataset_id}.def.sdmx.json"
    resp = _get(url)
    data = resp.json()
    out = _data_dir(dataset_id) / f"metadata_{date.today().isoformat()}.json"
    out.write_text(json.dumps(data, indent=2))
    return data


def fetch_data(
    dataset_id: str = PRIMARY_DATASET,
    years: Optional[list[int]] = None,
    occupations: Optional[list[str]] = None,
) -> Path:
    """Fetch ASHE data as CSV for the given years and occupations.

    Tries multiple parameter strategies to handle differences between
    NM_99_1 (SOC 2020) and NM_30_1 (SOC 2010) dimension structures.

    Returns the path to the raw CSV dump.
    """
    geo_param = ",".join(UK_REGION_GEOGRAPHIES.keys())
    current_year = date.today().year

    # Build date parameter
    if years:
        date_param = ",".join(str(y) for y in years)
    else:
        date_param = "latest"

    url = f"{NOMIS_BASE}/dataset/{dataset_id}.data.csv"
    out = _data_dir(dataset_id) / f"data_{date.today().isoformat()}.csv"

    # Nomis datasets have inconsistent dimension names. We try multiple
    # parameter strategies, from most-specific to least-specific.
    occ_param = ",".join(occupations) if occupations else "0...9999"

    date_options = [date_param]
    if date_param == "latest":
        date_options.append(",".join(str(y) for y in range(current_year - 3, current_year + 1)))

    strategies = []
    for d in date_options:
        # Strategy 1: full parameters with occupation range
        strategies.append({
            "date": d, "geography": geo_param,
            "sex": ",".join(str(s) for s in SEX_CODES),
            "item": ",".join(str(i) for i in PAY_ITEMS),
            "pay": ",".join(str(p) for p in PAY_MEASURES),
            "measures": ",".join(str(m) for m in MEASURES),
            "occupation": occ_param,
            "select": "DATE_NAME,GEOGRAPHY_CODE,GEOGRAPHY_NAME,SEX_NAME,ITEM_NAME,ITEM_CODE,PAY_NAME,OCCUPATION_CODE,OCCUPATION_NAME,OBS_VALUE,OBS_STATUS_NAME",
        })
        # Strategy 2: without explicit occupation (let API return all)
        strategies.append({
            "date": d, "geography": geo_param,
            "sex": ",".join(str(s) for s in SEX_CODES),
            "item": ",".join(str(i) for i in PAY_ITEMS),
            "pay": ",".join(str(p) for p in PAY_MEASURES),
            "measures": ",".join(str(m) for m in MEASURES),
            "select": "DATE_NAME,GEOGRAPHY_CODE,GEOGRAPHY_NAME,SEX_NAME,ITEM_NAME,ITEM_CODE,PAY_NAME,OCCUPATION_CODE,OCCUPATION_NAME,OBS_VALUE,OBS_STATUS_NAME",
        })

    for i, params in enumerate(strategies, 1):
        desc = f"date={params['date']}, occ={'all' if 'occupation' not in params else params['occupation'][:30]}"
        print(f"[nomis] Strategy {i}/{len(strategies)}: {dataset_id} CSV ({desc})...")
        try:
            resp = _get(url, params=params)
        except Exception as e:
            print(f"[nomis] Strategy {i} failed: {e}")
            continue
        n_rows = resp.text.count("\n") - 1

        if n_rows > 0:
            out.write_text(resp.text, encoding="utf-8")
            print(f"[nomis] Success: {n_rows} rows written to {out}")
            return out
        else:
            print(f"[nomis] Strategy {i} returned 0 data rows.")

    # Last resort: save empty + print the CSV header for debugging
    out.write_text(resp.text, encoding="utf-8")
    header = resp.text[:500] if resp.text else "(empty response)"
    print(f"[nomis] WARNING: All strategies returned 0 rows for {dataset_id}.")
    print(f"[nomis] Response header: {header}")
    return out


if __name__ == "__main__":
    # Default: fetch metadata + latest year data, all SOC groupings.
    fetch_metadata()
    fetch_data()
