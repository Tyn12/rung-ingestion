"""Parse NHS Agenda for Change HTML into CompensationObservation rows.

NHS Employers publishes pay scales on Drupal pages using an accordion layout.
Each accordion section (one per band) contains a <table> whose:
    - first row holds step-point labels ("Entry step point", etc.)
    - second row holds the salary figures (£xx,xxx format)

There is no <thead> element. Band names (e.g. "Band 5", "Band 8a") appear in
the accordion heading, typically an <h3> or <button> with class
"c-accordion__heading".

We emit one CompensationObservation per (band, step) × year.
"""
from __future__ import annotations
import re
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from shared.models import (
    CompensationObservation,
    ContractType,
    ExperienceBand,
    ObservationType,
    Period,
)
from shared.normalization import NORMALIZATION_VERSION

# Matches "Band 1", "Band 5", "Band 8a", "Band 8d", "Band 9" etc.
BAND_RE = re.compile(r"[Bb]and\s+(\d[a-d]?)", re.IGNORECASE)
MONEY_RE = re.compile(r"£\s*([\d,]+)")

_STEP_LABELS = {
    "entry": "Entry",
    "intermediate": "2 years",
    "top": "Top",
}


def _normalise_step(raw: str) -> str:
    """Map column header text to a canonical step label."""
    low = raw.lower().strip()
    for key, label in _STEP_LABELS.items():
        if key in low:
            return label
    # Fallback: use the raw text, trimmed
    return raw.strip()[:60] or "Unknown"


def _find_band_for_table(table: Tag) -> str | None:
    """Walk backwards from a <table> to find its band heading.

    Checks (in order):
        1. Accordion heading (h3/button with class containing 'accordion')
        2. Any preceding heading element (h1-h4, caption)
    """
    # Strategy 1: walk up through parent divs looking for accordion structure
    parent = table.parent
    while parent and parent.name != "body":
        if parent.get("class") and any("accordion" in c for c in parent.get("class", [])):
            # Look for heading inside or near this accordion section
            heading = parent.find(class_=re.compile(r"accordion.*head", re.IGNORECASE))
            if heading:
                m = BAND_RE.search(heading.get_text(" ", strip=True))
                if m:
                    return f"Band {m.group(1)}"
            # Also check direct children headings
            for tag_name in ["h2", "h3", "h4", "button"]:
                h = parent.find(tag_name)
                if h:
                    m = BAND_RE.search(h.get_text(" ", strip=True))
                    if m:
                        return f"Band {m.group(1)}"
        parent = parent.parent

    # Strategy 2: walk backwards through preceding siblings/elements
    el = table
    for _ in range(20):  # limit search depth
        el = el.find_previous(["h1", "h2", "h3", "h4", "caption", "button", "strong"])
        if el is None:
            break
        text = el.get_text(" ", strip=True)
        m = BAND_RE.search(text)
        if m:
            return f"Band {m.group(1)}"

    return None


def _parse_money(cell_text: str) -> float | None:
    m = MONEY_RE.search(cell_text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


_BAND_EXPERIENCE = {
    "Band 1": ExperienceBand.JUNIOR,
    "Band 2": ExperienceBand.JUNIOR,
    "Band 3": ExperienceBand.JUNIOR,
    "Band 4": ExperienceBand.MID,
    "Band 5": ExperienceBand.MID,
    "Band 6": ExperienceBand.SENIOR,
    "Band 7": ExperienceBand.SENIOR,
    "Band 8a": ExperienceBand.LEAD,
    "Band 8b": ExperienceBand.LEAD,
    "Band 8c": ExperienceBand.PRINCIPAL,
    "Band 8d": ExperienceBand.PRINCIPAL,
    "Band 9": ExperienceBand.DIRECTOR,
}


def _band_to_experience(band_name: str) -> ExperienceBand:
    return _BAND_EXPERIENCE.get(band_name, ExperienceBand.UNKNOWN)


def parse_afc_html(html: str, year_starting: int) -> list[CompensationObservation]:
    soup = BeautifulSoup(html, "lxml")
    observed_at = date(year_starting, 4, 1)
    out: list[CompensationObservation] = []

    for table in soup.find_all("table"):
        band = _find_band_for_table(table)
        if not band:
            continue

        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # First row = step-point labels (may be <th> or <td>)
        header_cells = rows[0].find_all(["th", "td"])
        step_labels = [_normalise_step(c.get_text(" ", strip=True)) for c in header_cells]

        # Remaining rows = salary data
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            for i, cell in enumerate(cells):
                salary = _parse_money(cell.get_text(" ", strip=True))
                if salary is None or salary < 10_000:
                    continue
                label = step_labels[i] if i < len(step_labels) else f"Point {i}"

                # Skip if this cell text is actually the band name, not a salary
                cell_text = cell.get_text(" ", strip=True)
                if BAND_RE.search(cell_text) and salary < 15_000:
                    continue

                out.append(CompensationObservation(
                    source_id="nhs_afc",
                    source_reference=f"nhs_afc:{year_starting}:{band}:{label}",
                    occupation_code=None,
                    location_code="K02000001",
                    company_ref="NHS",
                    observation_type=ObservationType.POINT,
                    value_amount=salary,
                    value_min=None,
                    value_max=None,
                    percentile=None,
                    period=Period.ANNUAL,
                    normalized_annual_amount=salary,
                    normalization_method_version=NORMALIZATION_VERSION,
                    currency="GBP",
                    experience_band=_band_to_experience(band),
                    contract_type=ContractType.PERMANENT,
                    sample_size=None,
                    total_comp_annual=None,
                    observed_at=observed_at,
                    source_payload={
                        "band": band,
                        "spine_label": label,
                        "year_starting": year_starting,
                        "scheme": "Agenda for Change",
                    },
                ))

    print(f"[nhs:parse] Found {len(out)} spine points across {len(set(o.source_payload['band'] for o in out))} bands")
    return out


def parse_file(path: Path, year_starting: int) -> list[CompensationObservation]:
    return parse_afc_html(path.read_text(encoding="utf-8"), year_starting)


if __name__ == "__main__":
    import sys
    year = int(sys.argv[1])
    path = Path(sys.argv[2])
    records = parse_file(path, year)
    print(f"{len(records)} spine points parsed for year {year}")
