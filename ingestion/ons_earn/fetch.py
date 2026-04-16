"""Fetch ONS Average Weekly Earnings datasets.

ONS publishes three sibling datasets monthly:
    EARN01 — headline AWE, whole economy (SA)
    EARN02 — AWE by sector (NSA)
    EARN03 — AWE by industry (NSA)

Each dataset has a stable landing page at /datasets/.../current that
always resolves to the latest edition. However, the *filename* of the
XLSX download changes each month (e.g. earn01mar2026.xlsx). We scrape
the dataset page to discover the current XLSX link rather than
hard-coding filenames that go stale.
"""
from __future__ import annotations
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

RAW_ROOT = Path("data/raw/ons_earn")
_ONS_ROOT = "https://www.ons.gov.uk"

# Minimum gap between HTTP calls (polite crawling).
_MIN_GAP = 1.0
_last: float = 0.0


@dataclass(frozen=True)
class EarnDataset:
    code: str
    label: str
    page_url: str          # dataset landing page (/current)
    axis: str              # "overall" | "industry"


# Dataset landing pages — these are stable and always show the latest edition.
_EARNS_PATH = "/employmentandlabourmarket/peopleinwork/earningsandworkinghours/datasets"

EARN_DATASETS: tuple[EarnDataset, ...] = (
    EarnDataset(
        code="EARN01",
        label="Average weekly earnings – headline (SA)",
        page_url=f"{_ONS_ROOT}{_EARNS_PATH}/averageweeklyearningsearn01/current",
        axis="overall",
    ),
    EarnDataset(
        code="EARN02",
        label="Average weekly earnings by sector (NSA)",
        page_url=f"{_ONS_ROOT}{_EARNS_PATH}/averageweeklyearningsbysectorearn02/current",
        axis="industry",
    ),
    EarnDataset(
        code="EARN03",
        label="Average weekly earnings by industry (NSA)",
        page_url=f"{_ONS_ROOT}{_EARNS_PATH}/averageweeklyearningsbyindustryearn03/current",
        axis="industry",
    ),
)


def _throttle() -> None:
    global _last
    delta = time.monotonic() - _last
    if delta < _MIN_GAP:
        time.sleep(_MIN_GAP - delta)
    _last = time.monotonic()


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _get(url: str) -> requests.Response:
    _throttle()
    resp = requests.get(
        url,
        headers={"User-Agent": "Rung/0.1 (+ingestion)"},
        timeout=60,
    )
    if 500 <= resp.status_code < 600:
        raise requests.ConnectionError(f"ONS 5xx: {resp.status_code}")
    resp.raise_for_status()
    return resp


def _discover_xlsx_url(page_url: str) -> str:
    """Scrape an ONS dataset page for the XLSX download link."""
    resp = _get(page_url)
    # ONS pages have download links like href="/file?uri=/.../earn01mar2026.xlsx"
    matches = re.findall(r'href="([^"]*\.xlsx[^"]*)"', resp.text, re.IGNORECASE)
    if not matches:
        raise RuntimeError(
            f"No XLSX download link found on {page_url}. "
            f"ONS may have changed the page layout."
        )
    # Take the first XLSX link — typically the main data file.
    rel = matches[0]
    return rel if rel.startswith("http") else urljoin(_ONS_ROOT, rel)


def fetch_earn_xlsx(dataset: EarnDataset, output_root: Path = RAW_ROOT) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    dest = output_root / f"{dataset.code.lower()}.xlsx"

    xlsx_url = _discover_xlsx_url(dataset.page_url)
    print(f"[ons_earn:fetch] {dataset.code} ← {xlsx_url}")
    resp = _get(xlsx_url)
    dest.write_bytes(resp.content)
    return dest


if __name__ == "__main__":
    for ds in EARN_DATASETS:
        print(fetch_earn_xlsx(ds))
