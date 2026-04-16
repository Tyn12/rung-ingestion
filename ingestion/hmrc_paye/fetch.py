"""Fetch the monthly ONS / HMRC Earnings from PAYE RTI datasets.

Source
------
ONS publishes monthly PAYE-RTI statistics covering:
    - Median monthly pay by industry (SIC)
    - Median monthly pay by age band
    - Median monthly pay by geography
    - Employees by industry / region / age

The data XLSX files live on dedicated *dataset* pages that use the stable
``/current/`` suffix — so the URL always resolves to the latest edition:

    .../datasets/realtimeinformationstatisticsreferencetable/current
    .../datasets/realtimeinformationstatisticsreferencetableseasonallyadjusted/current

We scrape each dataset page for XLSX download links and pull them all. This is
more resilient than scraping the bulletin landing page, which no longer
directly links the data workbooks.

Rate limiting
-------------
ONS static file hosting is effectively unlimited, but we politely throttle to
1 req/sec to avoid annoying anyone. Tenacity retries on transient 5xx.
"""
from __future__ import annotations
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

_ONS_BASE = "https://www.ons.gov.uk"
_EARNS_PATH = "/employmentandlabourmarket/peopleinwork/earningsandworkinghours/datasets"

# Stable /current/ dataset pages whose XLSX links we scrape.
DATASET_PAGES: list[str] = [
    f"{_ONS_BASE}{_EARNS_PATH}/realtimeinformationstatisticsreferencetablenonseasonallyadjusted/current",
    f"{_ONS_BASE}{_EARNS_PATH}/realtimeinformationstatisticsreferencetableseasonallyadjusted/current",
]

RAW_ROOT = Path("data/raw/hmrc_paye")

# Minimum gap between HTTP calls.
MIN_GAP = 1.0
_last: float = 0.0


def _throttle() -> None:
    global _last
    delta = time.monotonic() - _last
    if delta < MIN_GAP:
        time.sleep(MIN_GAP - delta)
    _last = time.monotonic()


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _get(url: str, **kwargs) -> requests.Response:
    _throttle()
    resp = requests.get(url, timeout=60, **kwargs)
    if 500 <= resp.status_code < 600:
        raise requests.ConnectionError(f"ONS 5xx: {resp.status_code}")
    resp.raise_for_status()
    return resp


def _discover_xlsx_links(html: str) -> list[str]:
    """Find every XLSX link in an HTML page.

    ONS dataset pages use two patterns:
        href="/file?uri=/long/path/file.xlsx"
        href="https://download.ons.gov.uk/downloads/datasets/.../file.xlsx"
    """
    matches = re.findall(r'href="([^"]*\.xlsx[^"]*)"', html, flags=re.IGNORECASE)
    return list(dict.fromkeys(matches))   # de-dupe, preserve order


_SKIP_PATTERNS = re.compile(
    r"example|methodology|guide|accessible|template",
    re.IGNORECASE,
)


def _is_example_file(url: str) -> bool:
    """Filter out methodology/example files that aren't real data."""
    # Check the filename portion only (after last /)
    filename = url.rsplit("/", 1)[-1].split("?")[0].lower()
    return bool(_SKIP_PATTERNS.search(filename))


def fetch_latest(output_root: Path = RAW_ROOT, today: Optional[date] = None) -> list[Path]:
    """Download PAYE RTI XLSX files from ONS dataset pages."""
    today = today or date.today()
    out_dir = output_root / today.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    all_urls: list[str] = []
    for page_url in DATASET_PAGES:
        try:
            resp = _get(page_url, headers={"User-Agent": "Rung/0.1 (+ingestion)"})
            xlsx_rels = _discover_xlsx_links(resp.text)
            for rel in xlsx_rels:
                full = rel if rel.startswith("http") else urljoin(_ONS_BASE, rel)
                if not _is_example_file(full):
                    all_urls.append(full)
        except Exception as e:  # noqa: BLE001
            print(f"[hmrc_paye:fetch] Warning: could not scrape {page_url}: {e}")

    # De-duplicate across pages (same XLSX can appear on both SA and NSA pages).
    all_urls = list(dict.fromkeys(all_urls))

    if not all_urls:
        raise RuntimeError(
            "No data XLSX links discovered on ONS PAYE RTI dataset pages. "
            "Dataset page URLs may have changed — check DATASET_PAGES."
        )

    files: list[Path] = []
    for url in all_urls:
        fname = url.rsplit("/", 1)[-1].split("?")[0]
        dest = out_dir / fname
        print(f"[hmrc_paye:fetch] Downloading {url}")
        resp = _get(url)
        dest.write_bytes(resp.content)
        files.append(dest)

    print(f"[hmrc_paye:fetch] Saved {len(files)} workbooks to {out_dir}")
    return files


if __name__ == "__main__":
    fetch_latest()
