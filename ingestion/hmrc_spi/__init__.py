"""HMRC Survey of Personal Incomes (SPI) ingestion."""
from .fetch import fetch_spi_xlsx, KNOWN_SPI_URLS
from .parse import parse_spi_xlsx, seed_percentiles
from .load import load

__all__ = [
    "fetch_spi_xlsx",
    "KNOWN_SPI_URLS",
    "parse_spi_xlsx",
    "seed_percentiles",
    "load",
]
