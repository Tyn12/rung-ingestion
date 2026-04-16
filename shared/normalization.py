"""Shared normalization utilities used by every ingestion pipeline."""
from __future__ import annotations
from typing import Optional


# UK working-days assumption for annualizing contractor day rates.
# 52 weeks * 5 days = 260. Subtract 28 stat holiday (20 annual + 8 bank) = 232.
# Industry convention is typically 220-230. We use 220 and tag the method version.
DAILY_TO_ANNUAL_DAYS = 220
HOURLY_TO_ANNUAL_HOURS = 37.5 * 52   # 1950, full-time UK convention
WEEKLY_TO_ANNUAL_WEEKS = 52

NORMALIZATION_VERSION = "v1_2026_04"


def normalize_to_annual(value: float, period: str) -> Optional[float]:
    """Convert a pay figure to annual GBP using documented UK conventions.

    period: one of 'annual', 'daily', 'hourly', 'weekly'
    """
    if value is None:
        return None
    p = period.lower()
    if p == "annual":
        return float(value)
    if p == "daily":
        return float(value) * DAILY_TO_ANNUAL_DAYS
    if p == "hourly":
        return float(value) * HOURLY_TO_ANNUAL_HOURS
    if p == "weekly":
        return float(value) * WEEKLY_TO_ANNUAL_WEEKS
    raise ValueError(f"Unknown period: {period}")


def clean_numeric(s) -> Optional[float]:
    """Strip commas, pound signs, whitespace; return float or None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace(",", "").replace("£", "").replace("$", "")
    if t in ("", "x", "-", "..", "n/a", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None
