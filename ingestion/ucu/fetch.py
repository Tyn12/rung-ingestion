"""Fetch UCEA single pay spine data.

UCEA publishes the 51-point Single Pay Spine each August at:
    https://www.ucea.ac.uk/library/publications/national-pay-negotiations/

The spine itself lives in a PDF or Excel; the URL changes each year as the
circular gets a new number. We maintain a small registry of known URLs and
allow manual override via --url.

If no URL is supplied and the year isn't in the registry, fetch.py raises,
and the caller can fall back to the hand-curated SeedSpine in parse.py so the
pipeline always has *something* valid to load.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

RAW_ROOT = Path("data/raw/ucu")

# Known circular URLs — add newer ones here as UCEA publishes them.
KNOWN_SPINES: dict[int, str] = {
    # Example entries; update these each August with the actual circular URL.
    # 2024: "https://www.ucea.ac.uk/documents/...xlsx",
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


def fetch_spine(
    year_starting: int,
    url: Optional[str] = None,
    output_root: Path = RAW_ROOT,
) -> Path:
    url = url or KNOWN_SPINES.get(year_starting)
    if not url:
        raise ValueError(
            f"No known UCEA spine URL for {year_starting}. "
            "Check https://www.ucea.ac.uk/ and pass --url, "
            "or skip fetch and rely on the seeded spine table."
        )
    year_dir = output_root / str(year_starting)
    year_dir.mkdir(parents=True, exist_ok=True)
    ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
    dest = year_dir / f"spine.{ext}"
    print(f"[ucu:fetch] Downloading {url}")
    dest.write_bytes(_get(url))
    return dest


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--url", default=None)
    a = p.parse_args()
    fetch_spine(a.year, url=a.url)
