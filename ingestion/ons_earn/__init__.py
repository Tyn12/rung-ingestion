"""ONS Average Weekly Earnings (EARN01 / EARN02 / EARN03) ingestion."""
from .fetch import fetch_earn_xlsx, EARN_DATASETS
from .parse import parse_earn_file
from .load import load

__all__ = ["fetch_earn_xlsx", "EARN_DATASETS", "parse_earn_file", "load"]
