"""Fetch HMRC Survey of Personal Incomes (SPI) distribution tables.

HMRC publishes SPI about 2-3 years after the relevant tax year. The key
reference tables for us are:
    Table 3.1  — Distribution of median/percentiles of total income
    Table 3.1a — Distribution of percentiles of earned income
    Table 3.4  — Income distribution by age
    Table 3.5  — Income distribution by gender
    Table 3.6  — Income distribution by region

Each tax year has a landing page like:
    https://www.gov.uk/government/statistics/personal-incomes-statistics-YYYY-to-YYYY

With XLSX attachments on assets.publishing.service.gov.uk. Update
KNOWN_SPI_URLS when a new year lands.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

RAW_ROOT = Path("data/raw/hmrc_spi")

# tax_year_ending → URL of Table 3.1 (or similar percentile table).
# e.g. 2022 means tax year 2021-22.
KNOWN_SPI_URLS: dict[int, str] = {
    # 2022: "https://assets.publishing.service.gov.uk/.../table-3-1.xlsx",
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


def fetch_spi_xlsx(
    tax_year_ending: int,
    url: Optional[str] = None,
    output_root: Path = RAW_ROOT,
) -> Path:
    url = url or KNOWN_SPI_URLS.get(tax_year_ending)
    if not url:
        raise ValueError(
            f"No known HMRC SPI URL for tax year ending {tax_year_ending}. "
            "Check gov.uk for 'Personal Incomes Statistics' and pass --url, "
            "or rely on the seeded percentile table."
        )
    year_dir = output_root / str(tax_year_ending)
    year_dir.mkdir(parents=True, exist_ok=True)
    ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
    dest = year_dir / f"spi.{ext}"
    print(f"[hmrc_spi:fetch] Downloading {url}")
    dest.write_bytes(_get(url))
    return dest


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--tax-year-ending", type=int, required=True)
    p.add_argument("--url", default=None)
    a = p.parse_args()
    fetch_spi_xlsx(a.tax_year_ending, url=a.url)
