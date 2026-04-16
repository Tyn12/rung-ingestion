"""UCEA / UCU single pay spine ingestion (UK higher education)."""
from .fetch import fetch_spine
from .parse import parse_spine_xlsx, SeedSpine, seed_spine
from .load import load

__all__ = ["fetch_spine", "parse_spine_xlsx", "SeedSpine", "seed_spine", "load"]
