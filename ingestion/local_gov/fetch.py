"""Fetch Local Government Transparency Code (LGTC) senior salary CSVs.

The 2015 Local Government Transparency Code requires councils in England to
publish, annually, the salary of any officer earning >£50k (post title, job
grade, salary, total remuneration, etc.). Wales/Scotland/NI have their own
regimes but most publish similar disclosures.

There's no central national endpoint — each council hosts its own CSV/XLSX
on its open-data pages. We maintain a registry and let ops extend it over
time. The format is (loosely) standardised by DLUHC guidance; our parser is
intentionally forgiving.

This module deliberately ships a small starter registry with placeholder
URLs. A real run needs URLs filled in via KNOWN_SPINES-style PRs, or
passing --council-url on the CLI.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

RAW_ROOT = Path("data/raw/local_gov")


@dataclass(frozen=True)
class Council:
    code: str                       # short slug used in source_reference
    name: str
    gss_code: Optional[str]         # ONS GSS code for the authority (E06/E07/E08/E09)
    url: Optional[str]              # CSV/XLSX senior-salaries URL


# Starter registry: 12 major UK councils and combined authorities.
# URLs are left blank — populate via PR or env override before first real run.
COUNCIL_REGISTRY: tuple[Council, ...] = (
    Council("gla",           "Greater London Authority",     "E12000007", None),
    Council("tfl",           "Transport for London",         "E12000007", None),
    Council("birmingham",    "Birmingham City Council",      "E08000025", None),
    Council("manchester",    "Manchester City Council",      "E08000003", None),
    Council("leeds",         "Leeds City Council",           "E08000035", None),
    Council("liverpool",     "Liverpool City Council",       "E08000012", None),
    Council("newcastle",     "Newcastle upon Tyne City Council", "E08000021", None),
    Council("sheffield",     "Sheffield City Council",       "E08000019", None),
    Council("bristol",       "Bristol City Council",         "E06000023", None),
    Council("cardiff",       "Cardiff Council",              "W06000015", None),
    Council("edinburgh",     "City of Edinburgh Council",    "S12000036", None),
    Council("glasgow",       "Glasgow City Council",         "S12000049", None),
)


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


def fetch_council_csv(
    council: Council,
    url_override: Optional[str] = None,
    output_root: Path = RAW_ROOT,
) -> Path:
    url = url_override or council.url
    if not url:
        raise ValueError(
            f"No URL registered for council '{council.code}'. "
            "Add one to COUNCIL_REGISTRY or pass --council-url."
        )
    council_dir = output_root / council.code
    council_dir.mkdir(parents=True, exist_ok=True)
    ext = url.rsplit(".", 1)[-1].split("?")[0].lower() or "csv"
    dest = council_dir / f"senior_salaries.{ext}"
    print(f"[local_gov:fetch] {council.code} ← {url}")
    dest.write_bytes(_get(url))
    return dest


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--council", required=True)
    p.add_argument("--url", default=None)
    a = p.parse_args()
    council = next(c for c in COUNCIL_REGISTRY if c.code == a.council)
    print(fetch_council_csv(council, url_override=a.url))
