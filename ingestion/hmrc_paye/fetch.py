"""Fetch the monthly ONS / HMRC Earnings from PAYE RTI bulletin.

Source
------
ONS publishes a monthly bulletin with PAYE-derived earnings aggregates. The page
hosts downloadable XLSX workbooks covering:

    - Median monthly pay by industry (SIC)
    - Median monthly pay by age band
    - Median monthly pay by geography
    - Employees by industry / region / age

Landing page:
    https://www.ons.gov.uk/employmentandlabourmarket/peopleinwork/
    earningsandworkinghours/bulletins/
    earningsandemploymentfrompayasyouearnrealtimeinformationuk/latest

Download links change each month (they embed the publication date). We scrape
the landing page to discover the current release's XLSX files rather than
hard-coding paths that go stale.

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

BULLETIN_LANDING_URL = (
    "https://www.ons.gov.uk/employmentandlabourmarket/peopleinwork/"
    "earningsandworkinghours/bulletins/"
    "earningsandemploymentfrompayasyouearnrealtimeinformationuk/latest"
)
ONS_ROOT = "https://www.ons.gov.uk"
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


def _discover_xlsx_links(landing_html: str) -> list[str]:
    """Find every XLSX link on the bulletin landing page."""
    # ONS HTML is stable enough that a regex beats pulling a full DOM parser
    # into the dependency tree. We capture both absolute and relative hrefs.
    matches = re.findall(r'href="([^"]+\.xlsx)"', landing_html, flags=re.IGNORECASE)
    return list(dict.fromkeys(matches))   # de-dupe, preserve order


def fetch_latest(output_root: Path = RAW_ROOT, today: Optional[date] = None) -> list[Path]:
    """Download every XLSX on the latest bulletin, save under data/raw/hmrc_paye/{date}/."""
    today = today or date.today()
    out_dir = output_root / today.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    landing = _get(BULLETIN_LANDING_URL, headers={"User-Agent": "Rung/0.1 (+ingestion)"})
    xlsx_rels = _discover_xlsx_links(landing.text)
    if not xlsx_rels:
        raise RuntimeError("No XLSX links discovered on the ONS bulletin page.")

    # Persist the raw HTML so we can audit what we saw at scrape time.
    (out_dir / "_landing.html").write_text(landing.text, encoding="utf-8")

    files: list[Path] = []
    for rel in xlsx_rels:
        url = rel if rel.startswith("http") else urljoin(ONS_ROOT, rel)
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
