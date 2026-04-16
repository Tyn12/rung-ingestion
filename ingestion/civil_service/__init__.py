"""UK Civil Service pay bands ingestion (Cabinet Office / SCS Pay Ranges)."""
from .fetch import fetch_bands
from .parse import parse_bands, seed_bands, CivilServiceBand
from .load import load

__all__ = ["fetch_bands", "parse_bands", "seed_bands", "CivilServiceBand", "load"]
