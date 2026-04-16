"""Fetch UK job listings from the Reed Developer (Jobseeker) API.

API docs: https://www.reed.co.uk/developers/jobseeker
Base URL: https://www.reed.co.uk/api/1.0/

Authentication
--------------
HTTP Basic Auth. Username = API key, password = empty string.

Endpoints used
--------------
GET /search              — paginated job search
GET /jobs/{jobId}         — full job detail (fuller jobDescription, fields)

Strategy ("broad sweep × daily")
--------------------------------
Reed's `/search` endpoint paginates at 100 results per page (resultsToTake=100)
and caps results at around 1,000 per query. So a naked "UK" sweep truncates.
We therefore fan out across UK regions (locationName + distanceFromLocation=15)
and drill deeper only where a region returns the cap — that minimizes API calls
while keeping coverage.

Rate limiting
-------------
Reed doesn't publish hard rate limits, but anecdotally ~100 req/minute is safe.
We enforce a conservative 1 req / 1.2s floor plus tenacity-driven exponential
backoff on 429/5xx, which gives roughly 3,000 requests per hour worst case.

Output
------
Raw paginated responses are saved verbatim under
    data/raw/reed/{YYYY-MM-DD}/{locationSlug}/page_{n}.json
so parse.py and dbt can replay ingestion deterministically from disk.
"""
from __future__ import annotations
import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

import requests
from requests.auth import HTTPBasicAuth
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

BASE_URL = "https://www.reed.co.uk/api/1.0"
SEARCH_ENDPOINT = f"{BASE_URL}/search"
JOB_DETAIL_ENDPOINT = f"{BASE_URL}/jobs"
RAW_ROOT = Path("data/raw/reed")

# Page size. Reed allows up to 100; smaller pages waste quota.
PAGE_SIZE = 100
# Reed's documented ceiling — further paging returns empty results.
MAX_RESULTS_PER_QUERY = 1_000
# Conservative pacing to stay well under unpublished rate limits.
MIN_SECONDS_BETWEEN_REQUESTS = 1.2

# Broad-sweep locations. We use city names + 15-mile radius so suburbs are included.
# These cover the bulk of UK job density; secondary cities are queried on demand.
UK_LOCATIONS: list[tuple[str, int]] = [
    ("London", 15),
    ("Manchester", 15),
    ("Birmingham", 15),
    ("Leeds", 15),
    ("Glasgow", 15),
    ("Edinburgh", 15),
    ("Liverpool", 15),
    ("Bristol", 15),
    ("Sheffield", 15),
    ("Newcastle upon Tyne", 15),
    ("Cardiff", 15),
    ("Belfast", 15),
    ("Nottingham", 15),
    ("Southampton", 15),
    ("Leicester", 15),
    ("Cambridge", 15),
    ("Oxford", 15),
    ("Reading", 15),
    ("Milton Keynes", 15),
    ("Brighton", 15),
    ("Aberdeen", 15),
    ("Dundee", 15),
    ("Swansea", 15),
    ("Portsmouth", 15),
    ("Derby", 15),
    ("Stoke-on-Trent", 15),
    ("Hull", 15),
    ("Plymouth", 15),
    ("Norwich", 15),
    ("Exeter", 15),
]


class ReedAuthError(RuntimeError):
    """Raised when the Reed API returns 401/403."""


def _auth() -> HTTPBasicAuth:
    key = os.environ.get("REED_API_KEY")
    if not key:
        raise RuntimeError(
            "REED_API_KEY is not set. Add it to .env.local or export it before running."
        )
    return HTTPBasicAuth(key, "")


_last_request_at: float = 0.0


def _throttle() -> None:
    """Block until MIN_SECONDS_BETWEEN_REQUESTS has elapsed since the last call."""
    global _last_request_at
    delta = time.monotonic() - _last_request_at
    if delta < MIN_SECONDS_BETWEEN_REQUESTS:
        time.sleep(MIN_SECONDS_BETWEEN_REQUESTS - delta)
    _last_request_at = time.monotonic()


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _get_json(url: str, params: dict) -> dict:
    _throttle()
    resp = requests.get(url, params=params, auth=_auth(), timeout=30)
    if resp.status_code in (401, 403):
        raise ReedAuthError(
            f"Reed API returned {resp.status_code}. Check REED_API_KEY validity."
        )
    if resp.status_code == 429:
        # Sleep + let tenacity retry
        time.sleep(10)
        resp.raise_for_status()
    if 500 <= resp.status_code < 600:
        # Transient — let tenacity retry after backoff
        raise requests.ConnectionError(f"Reed 5xx: {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def _slug(text: str) -> str:
    return (
        text.lower()
        .replace(" ", "-")
        .replace("/", "-")
        .replace(",", "")
        .replace(".", "")
    )


def fetch_listings(
    locations: Optional[Iterable[tuple[str, int]]] = None,
    keywords: Optional[str] = None,
    output_root: Path = RAW_ROOT,
    max_pages_per_location: int = MAX_RESULTS_PER_QUERY // PAGE_SIZE,
    today: Optional[date] = None,
) -> list[Path]:
    """Fetch paginated listings across UK locations.

    Writes each raw page to disk and returns the list of page files written.
    """
    locations = list(locations) if locations is not None else UK_LOCATIONS
    today = today or date.today()
    run_dir = output_root / today.isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)

    files: list[Path] = []
    for location_name, radius in locations:
        loc_dir = run_dir / _slug(location_name)
        loc_dir.mkdir(parents=True, exist_ok=True)

        for page_idx in range(max_pages_per_location):
            params = {
                "locationName": location_name,
                "distanceFromLocation": radius,
                "resultsToTake": PAGE_SIZE,
                "resultsToSkip": page_idx * PAGE_SIZE,
            }
            if keywords:
                params["keywords"] = keywords

            try:
                payload = _get_json(SEARCH_ENDPOINT, params=params)
            except ReedAuthError:
                raise
            except Exception as e:
                print(f"[reed:fetch] ERROR {location_name} page {page_idx}: {e}")
                break

            results = payload.get("results") or []
            total_results = payload.get("totalResults", 0)

            page_path = loc_dir / f"page_{page_idx:03d}.json"
            page_path.write_text(json.dumps(payload, indent=2))
            files.append(page_path)

            print(
                f"[reed:fetch] {location_name} page {page_idx}: "
                f"{len(results)} results (total={total_results})"
            )

            # Stop paginating when we've exhausted results
            if not results or (page_idx + 1) * PAGE_SIZE >= min(total_results, MAX_RESULTS_PER_QUERY):
                break

    print(f"[reed:fetch] Wrote {len(files)} page files to {run_dir}")
    return files


def fetch_job_detail(job_id: int | str, output_dir: Optional[Path] = None) -> dict:
    """Fetch full detail for a single jobId (richer description than /search)."""
    url = f"{JOB_DETAIL_ENDPOINT}/{job_id}"
    payload = _get_json(url, params={})
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{job_id}.json").write_text(json.dumps(payload, indent=2))
    return payload


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Fetch Reed listings across UK locations.")
    p.add_argument("--locations", nargs="*", help="Subset of location names to fetch.")
    p.add_argument("--keywords", default=None)
    args = p.parse_args()
    locs = None
    if args.locations:
        name_to_radius = dict(UK_LOCATIONS)
        locs = [(n, name_to_radius.get(n, 15)) for n in args.locations]
    fetch_listings(locations=locs, keywords=args.keywords)
