"""Fetch resources from the London Datastore.

The London Datastore moved away from standard CKAN API paths to a custom
URL scheme using short dataset codes (e.g. ``vq846``). Direct download
URLs are the most reliable method:

    https://data.london.gov.uk/download/{short_code}/{resource_uuid}/{filename}

We maintain known direct download URLs per dataset and fall back to CKAN
API discovery if those fail. GLA typically revises the Earnings dataset
each November after ASHE results land.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

RAW_ROOT = Path("data/raw/london_datastore")


@dataclass(frozen=True)
class LondonDataset:
    slug: str                  # human-readable identifier
    code: str                  # short source_reference prefix
    axis: str                  # "workplace" | "residence" | "gender_pay_gap"
    direct_urls: tuple[str, ...] = field(default_factory=tuple)


# Direct download URLs discovered from the London Datastore website.
# These use the /download/{short_code}/{uuid}/{filename} pattern.
# When GLA re-publishes, the UUID changes but the short_code stays stable.
LONDON_DATASETS: tuple[LondonDataset, ...] = (
    LondonDataset(
        slug="earnings-workplace-borough",
        code="EARN_WORKPLACE",
        axis="workplace",
        direct_urls=(
            "https://data.london.gov.uk/download/vq846/d3bfabd0-33cd-496c-8ab8-6edeb2227dc0/earnings-workplace-borough.xls",
        ),
    ),
    LondonDataset(
        slug="earnings-place-residence-borough",
        code="EARN_RESIDENCE",
        axis="residence",
        direct_urls=(),  # TODO: add URL once discovered
    ),
    LondonDataset(
        slug="gender-pay-gaps",
        code="GPG_LONDON",
        axis="gender_pay_gap",
        direct_urls=(),  # TODO: add URL once discovered
    ),
)


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _get_bytes(url: str) -> bytes:
    resp = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "*/*",
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.content


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _get_json(url: str) -> dict:
    resp = requests.get(
        url,
        headers={"User-Agent": "Rung/0.1 (+ingestion)"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _try_direct_download(dataset: LondonDataset, output_root: Path) -> Optional[Path]:
    """Try known direct download URLs."""
    for url in dataset.direct_urls:
        try:
            print(f"[london_datastore:fetch] {dataset.code} trying direct URL: {url}")
            data = _get_bytes(url)
            # Determine format from URL
            fname = url.rsplit("/", 1)[-1]
            fmt = fname.rsplit(".", 1)[-1].lower() if "." in fname else "xls"
            out_dir = output_root / dataset.slug
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = out_dir / f"latest.{fmt}"
            dest.write_bytes(data)
            print(f"[london_datastore:fetch] {dataset.code} ← {url} ({len(data):,} bytes)")
            return dest
        except Exception as e:  # noqa: BLE001
            print(f"[london_datastore:fetch] Direct download failed: {e}")
            continue
    return None


def _try_ckan_api(dataset: LondonDataset, output_root: Path) -> Optional[Path]:
    """Fall back to CKAN API discovery."""
    for base in ("https://data.london.gov.uk/api/3/action", "https://data.london.gov.uk/api/action"):
        url = f"{base}/package_show?id={dataset.slug}"
        try:
            payload = _get_json(url)
            if not payload.get("success"):
                continue
            resources = payload["result"].get("resources", []) or []
            # Prefer XLS/XLSX, then CSV
            for fmt_set in ({"xlsx", "xls"}, {"csv"}):
                for r in sorted(resources, key=lambda r: r.get("last_modified") or "", reverse=True):
                    if (r.get("format") or "").lower() in fmt_set and r.get("url"):
                        dl_url = r["url"]
                        fmt = (r.get("format") or "xls").lower()
                        out_dir = output_root / dataset.slug
                        out_dir.mkdir(parents=True, exist_ok=True)
                        dest = out_dir / f"latest.{fmt}"
                        print(f"[london_datastore:fetch] {dataset.code} ← {dl_url} (via CKAN)")
                        dest.write_bytes(_get_bytes(dl_url))
                        return dest
        except Exception as e:  # noqa: BLE001
            print(f"[london_datastore:fetch] CKAN API {base} failed for {dataset.slug}: {e}")
            continue
    return None


def fetch_latest_resource(
    dataset: LondonDataset,
    output_root: Path = RAW_ROOT,
) -> Path:
    """Download the latest resource for a London Datastore dataset.

    Tries direct download URLs first, then CKAN API discovery.
    """
    # Try direct URLs first (most reliable)
    path = _try_direct_download(dataset, output_root)
    if path:
        return path

    # Fall back to CKAN API
    path = _try_ckan_api(dataset, output_root)
    if path:
        return path

    raise ValueError(
        f"Could not fetch {dataset.code} ({dataset.slug}). "
        f"Neither direct URLs nor CKAN API returned data. "
        f"Try manual download with --from-file."
    )


if __name__ == "__main__":
    for ds in LONDON_DATASETS:
        try:
            print(fetch_latest_resource(ds))
        except Exception as e:  # noqa: BLE001
            print(f"{ds.slug}: {e}")
