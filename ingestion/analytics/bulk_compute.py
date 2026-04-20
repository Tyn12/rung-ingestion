"""Bulk pre-computation engine for dashboard analytics.

Replaces ~300K individual SQL queries with ~10 bulk queries, then assembles
per-profile JSON payloads from in-memory lookup tables.

Same output schema as compute.py — the dashboard frontend sees no difference.

Strategy:
  1. Run a handful of GROUP BY queries at different aggregation levels
     (occ+loc+band, occ+loc, occ, global, etc.) to pre-compute ALL
     percentiles, trends, distributions, and metadata.
  2. Store results in dicts keyed by (occ_or_None, loc_or_None, band_or_None).
  3. For each of the ~16K profile keys, assemble the full JSONB payload
     by looking up pre-computed values — pure Python, zero SQL.
  4. Batch upsert all payloads in one execute_values call.

This reduces refresh time from hours to minutes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from .compute import (
    BAND_ORDER,
    MIN_SAMPLE_SIZE,
    PCTILE_POINTS,
    ProfileKey,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reusable SQL fragment for percentile aggregation
# ---------------------------------------------------------------------------
_PCTILE_AGG = """
    COUNT(*) AS sample_size,
    ROUND(AVG(normalized_annual_amount)::NUMERIC, 0) AS mean,
    ROUND(MIN(normalized_annual_amount)::NUMERIC, 0) AS min_salary,
    ROUND(MAX(normalized_annual_amount)::NUMERIC, 0) AS max_salary,
    ROUND(PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p10,
    ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p25,
    ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p50,
    ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p75,
    ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p90
"""

_BASE_WHERE = (
    "WHERE normalized_annual_amount IS NOT NULL "
    "AND normalized_annual_amount > 0"
)


# ---------------------------------------------------------------------------
# Pre-computed data container
# ---------------------------------------------------------------------------
@dataclass
class PrecomputedData:
    """All bulk-queried data, indexed for fast lookup."""

    # Percentile cache: (occ|None, loc|None, band|None) -> percentile dict
    percentiles: dict = field(default_factory=dict)

    # Trends: (occ|None, loc|None) -> [{year, p50, sample_size}, ...]
    trends: dict = field(default_factory=dict)

    # Distribution: (occ|None, loc|None) -> {bin_width, bins, total_observations}
    distributions: dict = field(default_factory=dict)

    # Metadata: (occ|None, loc|None) -> {sources_used, avg_confidence_weight, ...}
    metadata: dict = field(default_factory=dict)

    # YoY growth: (occ|None, loc|None) -> float | None
    yoy_growth: dict = field(default_factory=dict)

    # Dimension lookups
    occ_labels: dict = field(default_factory=dict)   # occ_code -> label
    loc_labels: dict = field(default_factory=dict)   # loc_code -> label
    loc_london: dict = field(default_factory=dict)   # loc_code -> bool
    col_indices: dict = field(default_factory=dict)  # loc_code -> float

    # Regions with data per occupation: occ_code|None -> [region_info, ...]
    occ_regions: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(val) -> Optional[int]:
    """Convert Decimal/float to int, returning None for NULL."""
    if val is None:
        return None
    return int(val)


def _format_pctile(row) -> dict:
    """Convert a query result row into a percentile dict."""
    ss = row["sample_size"] or 0
    result = {
        "p10": _safe_int(row["p10"]),
        "p25": _safe_int(row["p25"]),
        "p50": _safe_int(row["p50"]),
        "p75": _safe_int(row["p75"]),
        "p90": _safe_int(row["p90"]),
        "mean": _safe_int(row["mean"]),
        "min_salary": _safe_int(row["min_salary"]),
        "max_salary": _safe_int(row["max_salary"]),
        "sample_size": ss,
    }
    if 0 < ss < MIN_SAMPLE_SIZE:
        result["low_confidence"] = True
    return result


_EMPTY_PCTILE: dict = {"sample_size": 0}


def _format_metadata(row) -> dict:
    """Convert a metadata query row into a metadata dict."""
    return {
        "sources_used": list(row["sources"]) if row["sources"] else [],
        "source_count": row["source_count"] or 0,
        "avg_confidence_weight": (
            round(float(row["avg_weight"]), 2) if row["avg_weight"] else 0.0
        ),
        "data_window_start": (
            row["earliest"].isoformat() if row.get("earliest") else None
        ),
        "data_window_end": (
            row["latest"].isoformat() if row.get("latest") else None
        ),
        "computed_at": datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# Bulk query loaders
# ---------------------------------------------------------------------------

def bulk_precompute(conn) -> PrecomputedData:
    """Run all bulk queries and build lookup tables.

    This is the expensive step — but it's ~10 SQL queries instead of ~300K.
    """
    data = PrecomputedData()

    logger.info("Loading dimension tables...")
    _load_dims(conn, data)

    logger.info("Computing bulk percentiles (~7 queries)...")
    _load_percentiles(conn, data)

    logger.info("Computing bulk trends...")
    _load_trends(conn, data)

    logger.info("Computing bulk distributions...")
    _load_distributions(conn, data)

    logger.info("Computing bulk metadata...")
    _load_metadata(conn, data)

    logger.info("Deriving YoY growth from trends...")
    _derive_yoy_growth(data)

    logger.info(
        "Pre-computation complete: %d percentile entries, "
        "%d trend series, %d distribution histograms, %d metadata entries",
        len(data.percentiles),
        len(data.trends),
        len(data.distributions),
        len(data.metadata),
    )
    return data


def _load_dims(conn, data: PrecomputedData):
    """Load dimension tables for label lookups."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT occupation_code, label FROM dim_occupation")
        for row in cur:
            data.occ_labels[row["occupation_code"]] = row["label"]

        cur.execute("SELECT location_code, label, is_london FROM dim_location")
        for row in cur:
            data.loc_labels[row["location_code"]] = row["label"]
            data.loc_london[row["location_code"]] = bool(row["is_london"])

        cur.execute("SELECT location_code, col_index FROM dim_cost_of_living")
        for row in cur:
            data.col_indices[row["location_code"]] = float(row["col_index"])

    logger.info(
        "  Loaded %d occupations, %d locations, %d COL indices",
        len(data.occ_labels), len(data.loc_labels), len(data.col_indices),
    )


def _load_percentiles(conn, data: PrecomputedData):
    """Pre-compute percentiles at every aggregation level needed.

    Levels (7 queries):
      1. (occ, loc, band) — fine-grained
      2. (occ, loc)       — all bands for occ+loc
      3. (occ, band)      — national by band  (career ladder for national profiles)
      4. (occ)            — national, all bands (national benchmark)
      5. (loc)            — all occupations by region
      6. (loc, band)      — all occupations by region+band
      7. ()               — global
    Plus: occ->regions index for regional comparison assembly.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

        # --- Level 1: (occ, loc, band) ---
        logger.info("  Percentiles: (occ, loc, band)...")
        cur.execute(f"""
            SELECT occupation_code, location_code, experience_band,
                   {_PCTILE_AGG}
            FROM compensation_observations
            {_BASE_WHERE}
              AND occupation_code IS NOT NULL
              AND location_code IS NOT NULL
            GROUP BY occupation_code, location_code, experience_band
        """)
        count = 0
        for row in cur:
            key = (row["occupation_code"], row["location_code"], row["experience_band"])
            data.percentiles[key] = _format_pctile(row)
            count += 1
        logger.info("    -> %d entries", count)

        # --- Level 2: (occ, loc) ---
        logger.info("  Percentiles: (occ, loc)...")
        cur.execute(f"""
            SELECT occupation_code, location_code,
                   {_PCTILE_AGG}
            FROM compensation_observations
            {_BASE_WHERE}
              AND occupation_code IS NOT NULL
              AND location_code IS NOT NULL
            GROUP BY occupation_code, location_code
        """)
        count = 0
        for row in cur:
            key = (row["occupation_code"], row["location_code"], None)
            data.percentiles[key] = _format_pctile(row)
            count += 1
        logger.info("    -> %d entries", count)

        # --- Level 3: (occ, band) — national by band ---
        logger.info("  Percentiles: (occ, band)...")
        cur.execute(f"""
            SELECT occupation_code, experience_band,
                   {_PCTILE_AGG}
            FROM compensation_observations
            {_BASE_WHERE}
              AND occupation_code IS NOT NULL
            GROUP BY occupation_code, experience_band
        """)
        count = 0
        for row in cur:
            key = (row["occupation_code"], None, row["experience_band"])
            data.percentiles[key] = _format_pctile(row)
            count += 1
        logger.info("    -> %d entries", count)

        # --- Level 4: (occ) — national, all bands ---
        logger.info("  Percentiles: (occ)...")
        cur.execute(f"""
            SELECT occupation_code,
                   {_PCTILE_AGG}
            FROM compensation_observations
            {_BASE_WHERE}
              AND occupation_code IS NOT NULL
            GROUP BY occupation_code
        """)
        count = 0
        for row in cur:
            key = (row["occupation_code"], None, None)
            data.percentiles[key] = _format_pctile(row)
            count += 1
        logger.info("    -> %d entries", count)

        # --- Level 5: (loc) — all occupations by region ---
        logger.info("  Percentiles: (loc)...")
        cur.execute(f"""
            SELECT location_code,
                   {_PCTILE_AGG}
            FROM compensation_observations
            {_BASE_WHERE}
              AND location_code IS NOT NULL
            GROUP BY location_code
        """)
        count = 0
        for row in cur:
            key = (None, row["location_code"], None)
            data.percentiles[key] = _format_pctile(row)
            count += 1
        logger.info("    -> %d entries", count)

        # --- Level 6: (loc, band) — all occupations by region+band ---
        logger.info("  Percentiles: (loc, band)...")
        cur.execute(f"""
            SELECT location_code, experience_band,
                   {_PCTILE_AGG}
            FROM compensation_observations
            {_BASE_WHERE}
              AND location_code IS NOT NULL
            GROUP BY location_code, experience_band
        """)
        count = 0
        for row in cur:
            key = (None, row["location_code"], row["experience_band"])
            data.percentiles[key] = _format_pctile(row)
            count += 1
        logger.info("    -> %d entries", count)

        # --- Level 7: () — global ---
        logger.info("  Percentiles: global...")
        cur.execute(f"""
            SELECT {_PCTILE_AGG}
            FROM compensation_observations
            {_BASE_WHERE}
        """)
        row = cur.fetchone()
        if row:
            data.percentiles[(None, None, None)] = _format_pctile(row)

        # --- occ -> regions index (for regional comparison assembly) ---
        logger.info("  Building occ->regions index...")
        cur.execute(f"""
            SELECT DISTINCT co.occupation_code, co.location_code,
                   dl.label AS location_label,
                   dl.is_london,
                   COALESCE(col.col_index, 1.000) AS col_index
            FROM compensation_observations co
            JOIN dim_location dl ON dl.location_code = co.location_code
            LEFT JOIN dim_cost_of_living col
                 ON col.location_code = co.location_code
            WHERE co.location_code IS NOT NULL
              AND co.occupation_code IS NOT NULL
              AND co.normalized_annual_amount > 0
            ORDER BY co.occupation_code, dl.label
        """)
        for row in cur:
            occ = row["occupation_code"]
            if occ not in data.occ_regions:
                data.occ_regions[occ] = []
            data.occ_regions[occ].append({
                "location_code": row["location_code"],
                "label": row["location_label"],
                "is_london": bool(row["is_london"]),
                "col_index": float(row["col_index"]),
            })

        # Also build the _all-occupations region index
        cur.execute(f"""
            SELECT DISTINCT co.location_code,
                   dl.label AS location_label,
                   dl.is_london,
                   COALESCE(col.col_index, 1.000) AS col_index
            FROM compensation_observations co
            JOIN dim_location dl ON dl.location_code = co.location_code
            LEFT JOIN dim_cost_of_living col
                 ON col.location_code = co.location_code
            WHERE co.location_code IS NOT NULL
              AND co.normalized_annual_amount > 0
            ORDER BY dl.label
        """)
        data.occ_regions[None] = []
        for row in cur:
            data.occ_regions[None].append({
                "location_code": row["location_code"],
                "label": row["location_label"],
                "is_london": bool(row["is_london"]),
                "col_index": float(row["col_index"]),
            })

    logger.info("  Total percentile cache entries: %d", len(data.percentiles))


def _load_trends(conn, data: PrecomputedData):
    """Pre-compute year-over-year medians for all (occ, loc) combos."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

        # (occ, loc) level
        logger.info("  Trends: (occ, loc)...")
        cur.execute(f"""
            SELECT occupation_code, location_code,
                   observed_year AS year,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (
                       ORDER BY normalized_annual_amount
                   ) AS p50,
                   COUNT(*) AS sample_size
            FROM compensation_observations
            {_BASE_WHERE}
              AND occupation_code IS NOT NULL
              AND location_code IS NOT NULL
            GROUP BY occupation_code, location_code, observed_year
            ORDER BY occupation_code, location_code, observed_year
        """)
        for row in cur:
            key = (row["occupation_code"], row["location_code"])
            if key not in data.trends:
                data.trends[key] = []
            data.trends[key].append({
                "year": row["year"],
                "p50": round(float(row["p50"])) if row["p50"] else None,
                "sample_size": row["sample_size"],
            })

        # (occ) national level
        logger.info("  Trends: (occ)...")
        cur.execute(f"""
            SELECT occupation_code,
                   observed_year AS year,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (
                       ORDER BY normalized_annual_amount
                   ) AS p50,
                   COUNT(*) AS sample_size
            FROM compensation_observations
            {_BASE_WHERE}
              AND occupation_code IS NOT NULL
            GROUP BY occupation_code, observed_year
            ORDER BY occupation_code, observed_year
        """)
        for row in cur:
            key = (row["occupation_code"], None)
            if key not in data.trends:
                data.trends[key] = []
            data.trends[key].append({
                "year": row["year"],
                "p50": round(float(row["p50"])) if row["p50"] else None,
                "sample_size": row["sample_size"],
            })

        # (loc) — all occupations by region
        logger.info("  Trends: (loc)...")
        cur.execute(f"""
            SELECT location_code,
                   observed_year AS year,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (
                       ORDER BY normalized_annual_amount
                   ) AS p50,
                   COUNT(*) AS sample_size
            FROM compensation_observations
            {_BASE_WHERE}
              AND location_code IS NOT NULL
            GROUP BY location_code, observed_year
            ORDER BY location_code, observed_year
        """)
        for row in cur:
            key = (None, row["location_code"])
            if key not in data.trends:
                data.trends[key] = []
            data.trends[key].append({
                "year": row["year"],
                "p50": round(float(row["p50"])) if row["p50"] else None,
                "sample_size": row["sample_size"],
            })

        # Global
        logger.info("  Trends: global...")
        cur.execute(f"""
            SELECT observed_year AS year,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (
                       ORDER BY normalized_annual_amount
                   ) AS p50,
                   COUNT(*) AS sample_size
            FROM compensation_observations
            {_BASE_WHERE}
            GROUP BY observed_year
            ORDER BY observed_year
        """)
        data.trends[(None, None)] = []
        for row in cur:
            data.trends[(None, None)].append({
                "year": row["year"],
                "p50": round(float(row["p50"])) if row["p50"] else None,
                "sample_size": row["sample_size"],
            })

    logger.info("  Total trend series: %d", len(data.trends))


def _load_distributions(conn, data: PrecomputedData):
    """Pre-compute salary distribution histograms."""
    bin_width = 5000

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

        # (occ, loc) level
        logger.info("  Distributions: (occ, loc)...")
        cur.execute(f"""
            SELECT occupation_code, location_code,
                   (FLOOR(normalized_annual_amount / {bin_width})
                    * {bin_width})::INTEGER AS bin_floor,
                   COUNT(*) AS count
            FROM compensation_observations
            {_BASE_WHERE}
              AND occupation_code IS NOT NULL
              AND location_code IS NOT NULL
            GROUP BY occupation_code, location_code, bin_floor
            ORDER BY occupation_code, location_code, bin_floor
        """)
        current_key = None
        current_bins: list[dict] = []
        for row in cur:
            key = (row["occupation_code"], row["location_code"])
            if key != current_key:
                if current_key is not None:
                    total = sum(b["count"] for b in current_bins)
                    data.distributions[current_key] = {
                        "bin_width": bin_width,
                        "bins": current_bins,
                        "total_observations": total,
                    }
                current_key = key
                current_bins = []
            current_bins.append({
                "floor": row["bin_floor"],
                "count": row["count"],
            })
        if current_key is not None:
            total = sum(b["count"] for b in current_bins)
            data.distributions[current_key] = {
                "bin_width": bin_width,
                "bins": current_bins,
                "total_observations": total,
            }

        # (occ) national level
        logger.info("  Distributions: (occ)...")
        cur.execute(f"""
            SELECT occupation_code,
                   (FLOOR(normalized_annual_amount / {bin_width})
                    * {bin_width})::INTEGER AS bin_floor,
                   COUNT(*) AS count
            FROM compensation_observations
            {_BASE_WHERE}
              AND occupation_code IS NOT NULL
            GROUP BY occupation_code, bin_floor
            ORDER BY occupation_code, bin_floor
        """)
        current_key = None
        current_bins = []
        for row in cur:
            key = (row["occupation_code"], None)
            if key != current_key:
                if current_key is not None:
                    total = sum(b["count"] for b in current_bins)
                    data.distributions[current_key] = {
                        "bin_width": bin_width,
                        "bins": current_bins,
                        "total_observations": total,
                    }
                current_key = key
                current_bins = []
            current_bins.append({
                "floor": row["bin_floor"],
                "count": row["count"],
            })
        if current_key is not None:
            total = sum(b["count"] for b in current_bins)
            data.distributions[current_key] = {
                "bin_width": bin_width,
                "bins": current_bins,
                "total_observations": total,
            }

        # (loc) — all occupations by region
        logger.info("  Distributions: (loc)...")
        cur.execute(f"""
            SELECT location_code,
                   (FLOOR(normalized_annual_amount / {bin_width})
                    * {bin_width})::INTEGER AS bin_floor,
                   COUNT(*) AS count
            FROM compensation_observations
            {_BASE_WHERE}
              AND location_code IS NOT NULL
            GROUP BY location_code, bin_floor
            ORDER BY location_code, bin_floor
        """)
        current_key = None
        current_bins = []
        for row in cur:
            key = (None, row["location_code"])
            if key != current_key:
                if current_key is not None:
                    total = sum(b["count"] for b in current_bins)
                    data.distributions[current_key] = {
                        "bin_width": bin_width,
                        "bins": current_bins,
                        "total_observations": total,
                    }
                current_key = key
                current_bins = []
            current_bins.append({
                "floor": row["bin_floor"],
                "count": row["count"],
            })
        if current_key is not None:
            total = sum(b["count"] for b in current_bins)
            data.distributions[current_key] = {
                "bin_width": bin_width,
                "bins": current_bins,
                "total_observations": total,
            }

        # Global
        logger.info("  Distributions: global...")
        cur.execute(f"""
            SELECT (FLOOR(normalized_annual_amount / {bin_width})
                    * {bin_width})::INTEGER AS bin_floor,
                   COUNT(*) AS count
            FROM compensation_observations
            {_BASE_WHERE}
            GROUP BY bin_floor
            ORDER BY bin_floor
        """)
        bins = [{"floor": row["bin_floor"], "count": row["count"]}
                for row in cur]
        total = sum(b["count"] for b in bins)
        data.distributions[(None, None)] = {
            "bin_width": bin_width,
            "bins": bins,
            "total_observations": total,
        }

    logger.info("  Total distribution histograms: %d", len(data.distributions))


def _load_metadata(conn, data: PrecomputedData):
    """Pre-compute source metadata for all (occ, loc) combos."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

        # (occ, loc)
        logger.info("  Metadata: (occ, loc)...")
        cur.execute("""
            SELECT co.occupation_code, co.location_code,
                   ARRAY_AGG(DISTINCT ds.display_name) AS sources,
                   COUNT(DISTINCT ds.source_id) AS source_count,
                   AVG(ds.confidence_weight) AS avg_weight,
                   MIN(co.observed_at) AS earliest,
                   MAX(co.observed_at) AS latest
            FROM compensation_observations co
            JOIN dim_source ds ON ds.source_id = co.source_id
            WHERE co.normalized_annual_amount IS NOT NULL
              AND co.normalized_annual_amount > 0
              AND co.occupation_code IS NOT NULL
              AND co.location_code IS NOT NULL
            GROUP BY co.occupation_code, co.location_code
        """)
        for row in cur:
            key = (row["occupation_code"], row["location_code"])
            data.metadata[key] = _format_metadata(row)

        # (occ) national
        logger.info("  Metadata: (occ)...")
        cur.execute("""
            SELECT co.occupation_code,
                   ARRAY_AGG(DISTINCT ds.display_name) AS sources,
                   COUNT(DISTINCT ds.source_id) AS source_count,
                   AVG(ds.confidence_weight) AS avg_weight,
                   MIN(co.observed_at) AS earliest,
                   MAX(co.observed_at) AS latest
            FROM compensation_observations co
            JOIN dim_source ds ON ds.source_id = co.source_id
            WHERE co.normalized_annual_amount IS NOT NULL
              AND co.normalized_annual_amount > 0
              AND co.occupation_code IS NOT NULL
            GROUP BY co.occupation_code
        """)
        for row in cur:
            key = (row["occupation_code"], None)
            data.metadata[key] = _format_metadata(row)

        # (loc) — all occupations by region
        logger.info("  Metadata: (loc)...")
        cur.execute("""
            SELECT co.location_code,
                   ARRAY_AGG(DISTINCT ds.display_name) AS sources,
                   COUNT(DISTINCT ds.source_id) AS source_count,
                   AVG(ds.confidence_weight) AS avg_weight,
                   MIN(co.observed_at) AS earliest,
                   MAX(co.observed_at) AS latest
            FROM compensation_observations co
            JOIN dim_source ds ON ds.source_id = co.source_id
            WHERE co.normalized_annual_amount IS NOT NULL
              AND co.normalized_annual_amount > 0
              AND co.location_code IS NOT NULL
            GROUP BY co.location_code
        """)
        for row in cur:
            key = (None, row["location_code"])
            data.metadata[key] = _format_metadata(row)

        # Global
        logger.info("  Metadata: global...")
        cur.execute("""
            SELECT ARRAY_AGG(DISTINCT ds.display_name) AS sources,
                   COUNT(DISTINCT ds.source_id) AS source_count,
                   AVG(ds.confidence_weight) AS avg_weight,
                   MIN(co.observed_at) AS earliest,
                   MAX(co.observed_at) AS latest
            FROM compensation_observations co
            JOIN dim_source ds ON ds.source_id = co.source_id
            WHERE co.normalized_annual_amount IS NOT NULL
              AND co.normalized_annual_amount > 0
        """)
        row = cur.fetchone()
        if row:
            data.metadata[(None, None)] = _format_metadata(row)

    logger.info("  Total metadata entries: %d", len(data.metadata))


def _derive_yoy_growth(data: PrecomputedData):
    """Derive year-over-year growth from pre-computed trends (no SQL)."""
    for key, trend_list in data.trends.items():
        valid = [
            t for t in trend_list
            if t["sample_size"] >= MIN_SAMPLE_SIZE and t["p50"]
        ]
        if len(valid) >= 2:
            current = valid[-1]["p50"]
            previous = valid[-2]["p50"]
            if previous and previous > 0:
                data.yoy_growth[key] = round(
                    (current - previous) / previous * 100, 1
                )

    logger.info("  YoY growth computed for %d series", len(data.yoy_growth))


# ---------------------------------------------------------------------------
# Payload assembly (pure Python lookups — zero SQL)
# ---------------------------------------------------------------------------

def _lookup_pctile(
    data: PrecomputedData,
    occ: str,
    loc: str,
    band: Optional[str] = None,
) -> dict:
    """Look up percentiles from the cache, handling _all sentinels."""
    occ_key = None if occ == "_all" else occ
    loc_key = None if loc == "_all" else loc
    band_key = None if band in ("_all", "unknown", None) else band

    result = data.percentiles.get((occ_key, loc_key, band_key))
    if result:
        return dict(result)  # copy to prevent mutation
    return dict(_EMPTY_PCTILE)


def assemble_payload(data: PrecomputedData, key: ProfileKey) -> dict:
    """Assemble the full analytics JSONB payload for one profile key.

    Output schema is identical to compute.compute_analytics().
    """
    return {
        "profile": _assemble_profile(data, key),
        "market": _assemble_market(data, key),
        "career_ladder": _assemble_career_ladder(data, key),
        "regions": _assemble_regions(data, key),
        "sectors": _assemble_sectors(data, key),
        "trends": _assemble_trends(data, key),
        "distribution": _assemble_distribution(data, key),
        "national_benchmark": _assemble_national_benchmark(data, key),
        "metadata": _assemble_metadata(data, key),
    }


def _assemble_profile(data: PrecomputedData, key: ProfileKey) -> dict:
    """Resolve human-readable labels for the profile dimensions."""
    if key.occupation_code != "_all":
        occ_label = data.occ_labels.get(key.occupation_code, key.occupation_code)
    else:
        occ_label = "All Occupations"

    if key.location_code != "_all":
        loc_label = data.loc_labels.get(key.location_code, key.location_code)
        is_london = data.loc_london.get(key.location_code, False)
    else:
        loc_label = "United Kingdom"
        is_london = False

    return {
        "occupation_code": key.occupation_code,
        "occupation_label": occ_label,
        "location_code": key.location_code,
        "location_label": loc_label,
        "is_london": is_london,
        "sector": key.sector,
        "experience_band": key.experience_band,
    }


def _assemble_market(data: PrecomputedData, key: ProfileKey) -> dict:
    """Market percentiles: exact segment + overall (all bands)."""
    regional = _lookup_pctile(
        data, key.occupation_code, key.location_code, key.experience_band
    )
    overall = _lookup_pctile(
        data, key.occupation_code, key.location_code
    )
    return {
        "regional_percentiles": regional,
        "overall_percentiles": overall,
    }


def _assemble_career_ladder(data: PrecomputedData, key: ProfileKey) -> list:
    """Percentiles for each experience band at the same occ+loc."""
    ladder = []
    for band in BAND_ORDER:
        pctiles = _lookup_pctile(
            data, key.occupation_code, key.location_code, band
        )
        entry = {
            "band": band,
            "is_user_band": band == key.experience_band,
            **pctiles,
        }
        if pctiles.get("sample_size", 0) > 0 or band == key.experience_band:
            ladder.append(entry)
    return ladder


def _assemble_regions(data: PrecomputedData, key: ProfileKey) -> list:
    """Percentiles for the same occupation across all regions."""
    occ_key = None if key.occupation_code == "_all" else key.occupation_code
    regions = data.occ_regions.get(occ_key, [])

    result = []
    for region in regions:
        loc_code = region["location_code"]

        # Try with band filter first
        pctiles = _lookup_pctile(
            data, key.occupation_code, loc_code, key.experience_band
        )
        # Fallback: drop band filter if sparse
        if pctiles.get("sample_size", 0) < MIN_SAMPLE_SIZE:
            pctiles = _lookup_pctile(data, key.occupation_code, loc_code)
        if pctiles.get("sample_size", 0) == 0:
            continue

        result.append({
            "location_code": loc_code,
            "label": region["label"],
            "is_user_region": loc_code == key.location_code,
            "is_london": region["is_london"],
            "col_index": region["col_index"],
            **pctiles,
        })

    result.sort(key=lambda r: r.get("p50", 0) or 0, reverse=True)
    return result


def _assemble_sectors(data: PrecomputedData, key: ProfileKey) -> list:
    """Percentiles for each sector.

    Note: the original code's _query_percentiles never filtered by sector,
    so all three sectors get the same percentile data. We replicate that
    exact behaviour here — only is_user_sector and yoy_growth_pct differ.
    """
    pctiles = _lookup_pctile(
        data, key.occupation_code, key.location_code, key.experience_band
    )
    if pctiles.get("sample_size", 0) == 0:
        return []

    occ_key = None if key.occupation_code == "_all" else key.occupation_code
    loc_key = None if key.location_code == "_all" else key.location_code
    yoy = data.yoy_growth.get((occ_key, loc_key))

    result = []
    for sector in ("private", "public", "nhs"):
        result.append({
            "sector": sector,
            "is_user_sector": sector == key.sector,
            "yoy_growth_pct": yoy,
            **dict(pctiles),  # copy
        })

    result.sort(key=lambda r: r.get("p50", 0) or 0, reverse=True)
    return result


def _assemble_trends(data: PrecomputedData, key: ProfileKey) -> list:
    """Year-over-year median salary trend."""
    occ_key = None if key.occupation_code == "_all" else key.occupation_code
    loc_key = None if key.location_code == "_all" else key.location_code
    return list(data.trends.get((occ_key, loc_key), []))


def _assemble_distribution(data: PrecomputedData, key: ProfileKey) -> dict:
    """Salary distribution histogram."""
    occ_key = None if key.occupation_code == "_all" else key.occupation_code
    loc_key = None if key.location_code == "_all" else key.location_code
    dist = data.distributions.get((occ_key, loc_key))
    if dist:
        return dict(dist)
    return {"bin_width": 5000, "bins": [], "total_observations": 0}


def _assemble_national_benchmark(data: PrecomputedData, key: ProfileKey) -> dict:
    """National-level percentiles for the same occupation."""
    return _lookup_pctile(data, key.occupation_code, "_all")


def _assemble_metadata(data: PrecomputedData, key: ProfileKey) -> dict:
    """Data provenance: sources, data window, freshness."""
    occ_key = None if key.occupation_code == "_all" else key.occupation_code
    loc_key = None if key.location_code == "_all" else key.location_code

    # Try specific (occ, loc) first, fall back to (occ), then global
    meta = data.metadata.get((occ_key, loc_key))
    if not meta:
        meta = data.metadata.get((occ_key, None))
    if not meta:
        meta = data.metadata.get((None, loc_key))
    if not meta:
        meta = data.metadata.get((None, None))
    if not meta:
        return {
            "sources_used": [],
            "source_count": 0,
            "avg_confidence_weight": 0.0,
            "data_window_start": None,
            "data_window_end": None,
            "computed_at": datetime.utcnow().isoformat() + "Z",
        }

    return dict(meta)  # copy
