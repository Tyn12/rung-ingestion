"""NHS Agenda for Change pay scales ingestion."""
from .fetch import fetch_afc_table
from .parse import parse_afc_html
from .load import load

__all__ = ["fetch_afc_table", "parse_afc_html", "load"]
