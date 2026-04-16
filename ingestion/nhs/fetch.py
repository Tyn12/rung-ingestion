"""Fetch NHS Agenda for Change pay scales.

NHS Employers publishes annual pay-scale pages at stable-ish URLs:
    https://www.nhsemployers.org/articles/pay-scales-{year}{yy}

e.g. https://www.nhsemployers.org/articles/pay-scales-202425

Each page carries HTML tables for bands 1-9, each row being a spine point with
annual salary. We download the page HTML and hand it to parse.py.

Some years NHS Employers also publish PDFs. If the HTML scrape fails for a
given year, you can fall back to their PDF and run it through the pdf skill.
"""
from __future__ import annotations
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

RAW_ROOT = Path("data/raw/nhs")


def _year_slug(year_starting: int) -> str:
    """e.g. 2024 → '202425'."""
    yy = (year_starting + 1) % 100
    return f"{year_starting}{yy:02d}"


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _get_html(url: str) -> str:
    resp = requests.get(
        url,
        headers={"User-Agent": "Rung/0.1 (+ingestion)"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def fetch_afc_table(year_starting: int, output_root: Path = RAW_ROOT) -> Path:
    """Fetch the AfC pay scale page for the given pay year (April to March)."""
    slug = _year_slug(year_starting)
    url = f"https://www.nhsemployers.org/articles/pay-scales-{slug}"
    year_dir = output_root / str(year_starting)
    year_dir.mkdir(parents=True, exist_ok=True)
    out = year_dir / "page.html"
    print(f"[nhs:fetch] Downloading {url}")
    html = _get_html(url)
    out.write_text(html, encoding="utf-8")
    print(f"[nhs:fetch] Saved {out}")
    return out


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, required=True, help="Pay year starting (April).")
    fetch_afc_table(p.parse_args().year)
