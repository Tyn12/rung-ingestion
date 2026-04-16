"""Stack Overflow Developer Survey ingestion (UK subset)."""
from .fetch import KNOWN_RELEASES, fetch_release
from .parse import parse_csv
from .load import load

__all__ = ["KNOWN_RELEASES", "fetch_release", "parse_csv", "load"]
