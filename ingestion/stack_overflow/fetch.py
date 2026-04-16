"""Fetch the Stack Overflow Developer Survey annual ZIP release.

Stack Overflow publishes the survey data at https://survey.stackoverflow.co/
each summer (typically May/June). The download is a ZIP containing:
    - survey_results_public.csv         (one row per respondent)
    - survey_results_schema.csv         (column descriptions)

Licence: CC BY-SA 4.0 (we credit Stack Overflow when exposing in-app).

Release URLs follow a stable but year-suffixed pattern:
    https://info.stackoverflowsolutions.com/rs/719-EMH-566/images/
    stack-overflow-developer-survey-{year}.zip

New releases may move — we maintain a small registry of known-good URLs and
allow override via --url. When a new year drops, add a line to KNOWN_RELEASES.
"""
from __future__ import annotations
import io
import os
import zipfile
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

RAW_ROOT = Path("data/raw/stack_overflow")

# Last-known-good release URLs. Update as new surveys are published.
KNOWN_RELEASES: dict[int, str] = {
    2024: "https://info.stackoverflowsolutions.com/rs/719-EMH-566/images/stack-overflow-developer-survey-2024.zip",
    2023: "https://info.stackoverflowsolutions.com/rs/719-EMH-566/images/stack-overflow-developer-survey-2023.zip",
    2022: "https://info.stackoverflowsolutions.com/rs/719-EMH-566/images/stack-overflow-developer-survey-2022.zip",
    2021: "https://info.stackoverflowsolutions.com/rs/719-EMH-566/images/stack-overflow-developer-survey-2021.zip",
}


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _get(url: str) -> bytes:
    headers = {"User-Agent": "Rung/0.1 (+ingestion)"}
    resp = requests.get(url, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.content


def fetch_release(
    year: int,
    url: Optional[str] = None,
    output_root: Path = RAW_ROOT,
) -> Path:
    """Download + unzip one survey release. Returns the path to the public CSV."""
    url = url or KNOWN_RELEASES.get(year)
    if not url:
        raise ValueError(
            f"No known release URL for {year}. Pass --url explicitly or add to KNOWN_RELEASES."
        )

    year_dir = output_root / str(year)
    year_dir.mkdir(parents=True, exist_ok=True)

    zip_path = year_dir / f"survey_{year}.zip"
    if not zip_path.exists():
        print(f"[so:fetch] Downloading {url}")
        zip_path.write_bytes(_get(url))
    else:
        print(f"[so:fetch] Using cached {zip_path}")

    # Extract just the public results CSV (ignore schema + PDF report).
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name.endswith("survey_results_public.csv"):
                target = year_dir / "survey_results_public.csv"
                target.write_bytes(z.read(name))
                print(f"[so:fetch] Extracted {target}")
                return target
    raise RuntimeError(f"survey_results_public.csv not found in {zip_path}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--url", default=None)
    a = p.parse_args()
    fetch_release(a.year, url=a.url)
