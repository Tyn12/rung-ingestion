"""Fetch Civil Service pay band data.

The Cabinet Office publishes annual "Civil Service Pay Remit Guidance" plus
"Senior Civil Service Pay Framework" documents. URLs change each year:

    https://www.gov.uk/government/publications/civil-service-pay-remit-guidance-YYYY-YY
    https://www.gov.uk/government/publications/senior-civil-service-pay-framework

Each department also publishes its own pay scales (HMRC, DWP, MoJ, etc.).
Rather than scraping 20+ department pages, we rely on the SCS framework for
the top of the ladder and a hand-curated baseline for delegated grades
(AA → G6), updated annually via KNOWN_SPINES in parse.seed_bands.

Passing --url lets ops point at a specific CSV/XLSX of current-year ranges.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

RAW_ROOT = Path("data/raw/civil_service")

# Known per-year published spreadsheets (e.g. gov.uk "SCS pay framework").
KNOWN_SPINES: dict[int, str] = {
    # 2024: "https://assets.publishing.service.gov.uk/.../scs-pay-ranges-2024.xlsx",
}


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _get(url: str) -> bytes:
    resp = requests.get(
        url,
        headers={"User-Agent": "Rung/0.1 (+ingestion)"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def fetch_bands(
    year_starting: int,
    url: Optional[str] = None,
    output_root: Path = RAW_ROOT,
) -> Path:
    url = url or KNOWN_SPINES.get(year_starting)
    if not url:
        raise ValueError(
            f"No known Civil Service pay ranges URL for {year_starting}. "
            "Check gov.uk SCS pay framework / pay remit guidance, and pass --url. "
            "Or fall back to the seed baseline."
        )
    year_dir = output_root / str(year_starting)
    year_dir.mkdir(parents=True, exist_ok=True)
    ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
    dest = year_dir / f"bands.{ext}"
    print(f"[civil_service:fetch] Downloading {url}")
    dest.write_bytes(_get(url))
    return dest


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--url", default=None)
    a = p.parse_args()
    fetch_bands(a.year, url=a.url)
