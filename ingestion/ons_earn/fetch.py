"""Fetch ONS Average Weekly Earnings datasets.

ONS publishes three sibling datasets monthly, all with stable /current/ URLs:
    EARN01 — headline AWE, whole economy
    EARN02 — AWE by industry sector
    EARN03 — AWE by region

The "/current/" path always points at the latest revision, so we don't need
version juggling. New monthly values appear usually around the 3rd Tuesday.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

RAW_ROOT = Path("data/raw/ons_earn")


@dataclass(frozen=True)
class EarnDataset:
    code: str
    label: str
    url: str
    axis: str  # "overall" | "industry" | "region"


EARN_DATASETS: tuple[EarnDataset, ...] = (
    EarnDataset(
        code="EARN01",
        label="Average weekly earnings, whole economy",
        url="https://www.ons.gov.uk/file?uri=/employmentandlabourmarket/peopleinwork/earningsandworkingtime/datasets/averageweeklyearningsearn01/current/earn01.xlsx",
        axis="overall",
    ),
    EarnDataset(
        code="EARN02",
        label="Average weekly earnings by industry",
        url="https://www.ons.gov.uk/file?uri=/employmentandlabourmarket/peopleinwork/earningsandworkingtime/datasets/averageweeklyearningsbyindustryearn02/current/earn02.xlsx",
        axis="industry",
    ),
    EarnDataset(
        code="EARN03",
        label="Average weekly earnings by region",
        url="https://www.ons.gov.uk/file?uri=/employmentandlabourmarket/peopleinwork/earningsandworkingtime/datasets/averageweeklyearningsbyregionearn03/current/earn03.xlsx",
        axis="region",
    ),
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


def fetch_earn_xlsx(dataset: EarnDataset, output_root: Path = RAW_ROOT) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    dest = output_root / f"{dataset.code.lower()}.xlsx"
    print(f"[ons_earn:fetch] {dataset.code} ← {dataset.url}")
    dest.write_bytes(_get(dataset.url))
    return dest


if __name__ == "__main__":
    for ds in EARN_DATASETS:
        print(fetch_earn_xlsx(ds))
