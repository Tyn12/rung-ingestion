"""Local Government Transparency Code senior salaries ingestion."""
from .fetch import fetch_council_csv, COUNCIL_REGISTRY, Council
from .parse import parse_senior_salaries_csv
from .load import load

__all__ = [
    "fetch_council_csv",
    "COUNCIL_REGISTRY",
    "Council",
    "parse_senior_salaries_csv",
    "load",
]
