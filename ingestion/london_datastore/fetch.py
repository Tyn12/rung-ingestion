"""Fetch resources from the London Datastore (CKAN-backed).

We use the CKAN ``package_show`` endpoint to discover the latest XLSX
resource per dataset, then download it. That way we don't have to track
resource IDs each time GLA re-publishes.

    https://data.london.gov.uk/api/3/action/package_show?id={slug}

GLA typically revises the Earnings dataset each November after ASHE
results land. Other datasets update on their own cadence; we query them
independently.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

RAW_ROOT = Path("data/raw/london_datastore")


@dataclass(frozen=True)
class LondonDataset:
    slug: str                  # CKAN dataset id (e.g. "earnings-workplace-borough")
    code: str                  # short source_reference prefix
    axis: str                  # "workplace" | "residence" | "gender_pay_gap"


LONDON_DATASETS: tuple[LondonDataset, ...] = (
    LondonDataset(
        slug="earnings-workplace-borough",
        code="EARN_WORKPLACE",
        axis="workplace",
    ),
    LondonDataset(
        slug="earnings-place-residence-borough",
        code="EARN_RESIDENCE",
        axis="residence",
    ),
    LondonDataset(
        slug="gender-pay-gaps",
        code="GPG_LONDON",
        axis="gender_pay_gap",
    ),
)


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _get_json(url: str) -> dict:
    resp = requests.get(
        url,
        headers={"User-Agent": "Rung/0.1 (+ingestion)"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _get_bytes(url: str) -> bytes:
    resp = requests.get(
        url,
        headers={"User-Agent": "Rung/0.1 (+ingestion)"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.content


import re

_CKAN_BASES = [
    "https://data.london.gov.uk/api/3/action",
    "https://data.london.gov.uk/api/action",
]

_DATASET_PAGE = "https://data.london.gov.uk/dataset"


def _latest_xlsx_resource(slug: str) -> Optional[dict]:
    """Find the latest XLSX resource for a London Datastore dataset.

    Tries CKAN package_show API first, then falls back to scraping the
    dataset HTML page for direct download links.
    """
    # --- Attempt 1: CKAN API ---
    payload: Optional[dict] = None
    last_err: Optional[Exception] = None
    for base in _CKAN_BASES:
        url = f"{base}/package_show?id={slug}"
        try:
            payload = _get_json(url)
            if payload.get("success"):
                break
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue

    if payload and payload.get("success"):
        resources = payload["result"].get("resources", []) or []

        def _sort_key(r: dict) -> tuple:
            return (r.get("last_modified") or r.get("created") or "", r.get("name") or "")

        xlsx = sorted(
            [r for r in resources if (r.get("format") or "").lower() in {"xlsx", "xls"}],
            key=_sort_key,
            reverse=True,
        )
        csv_res = sorted(
            [r for r in resources if (r.get("format") or "").lower() == "csv"],
            key=_sort_key,
            reverse=True,
        )
        result = (xlsx or csv_res or resources or [None])[0]
        if result:
            return result

    # --- Attempt 2: Scrape dataset page for download links ---
    print(f"[london_datastore:fetch] CKAN API failed for {slug}, trying HTML scrape")
    page_url = f"{_DATASET_PAGE}/{slug}"
    try:
        resp = requests.get(
            page_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=30,
        )
        resp.raise_for_status()
        # Look for XLSX/CSV download links
        xlsx_links = re.findall(
            r'href="([^"]+\.xlsx[^"]*)"', resp.text, flags=re.IGNORECASE
        )
        csv_links = re.findall(
            r'href="([^"]+\.csv[^"]*)"', resp.text, flags=re.IGNORECASE
        )
        link = (xlsx_links or csv_links or [None])[0]
        if link:
            fmt = "xlsx" if xlsx_links else "csv"
            # Make absolute if relative
            if link.startswith("/"):
                link = f"https://data.london.gov.uk{link}"
            return {"url": link, "format": fmt, "name": slug}
    except Exception as e:  # noqa: BLE001
        print(f"[london_datastore:fetch] HTML scrape also failed for {slug}: {e}")
        if last_err:
            raise last_err
        raise

    if last_err:
        raise last_err
    raise ValueError(f"No downloadable resource found for {slug}")


def fetch_latest_resource(
    dataset: LondonDataset,
    output_root: Path = RAW_ROOT,
) -> Path:
    resource = _latest_xlsx_resource(dataset.slug)
    if not resource:
        raise ValueError(f"No downloadable resource for {dataset.slug}")
    url = resource.get("url")
    if not url:
        raise ValueError(f"Resource for {dataset.slug} has no URL: {resource}")

    out_dir = output_root / dataset.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = (resource.get("format") or url.rsplit(".", 1)[-1]).lower().strip(".")
    dest = out_dir / f"latest.{fmt}"
    print(f"[london_datastore:fetch] {dataset.code} ← {url}")
    dest.write_bytes(_get_bytes(url))
    return dest


if __name__ == "__main__":
    for ds in LONDON_DATASETS:
        try:
            print(fetch_latest_resource(ds))
        except Exception as e:  # noqa: BLE001
            print(f"{ds.slug}: {e}")
