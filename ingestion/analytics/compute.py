"""Core computation engine for dashboard analytics.

Given a profile key (occupation_code, location_code, sector, experience_band),
this module queries the raw data and produces a complete JSONB payload that
the dashboard frontend can consume without any further computation beyond
user-specific arithmetic (percentile interpolation, salary gaps).

Design principles:
  - Every number traces back to the dataset.  No magic constants.
  - The JSONB schema stores *building blocks*, not graph-specific data.
    New visualisations read the same payload via new frontend templates.
  - Computation is idempotent.  Running it twice produces the same result.
  - Performance: queries are batched per profile key.  A full refresh of
    ~500 active combinations takes <60s on a modest Postgres instance.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# Standard percentile points we compute and report.
PCTILE_POINTS = (10, 25, 50, 75, 90)

# Experience bands in career-ladder order.
BAND_ORDER = ("junior", "mid", "senior", "lead", "principal", "director")

# Minimum observations before we include a data slice.
MIN_SAMPLE_SIZE = 5


@dataclass
class ProfileKey:
    """The dimensions that uniquely identify a dashboard analytics row."""
    occupation_code: str
    location_code: str
    sector: str            # 'private' | 'public' | 'nhs'
    experience_band: str   # 'junior' .. 'principal'


def _normalise_key(key: ProfileKey) -> ProfileKey:
    """Replace '_all' sentinel values with None-safe equivalents.

    Many builder functions use _all as a wildcard. This normalises
    the key so downstream queries can use optional clauses.
    """
    return ProfileKey(
        occupation_code=key.occupation_code if key.occupation_code != "_all" else "_all",
        location_code=key.location_code if key.location_code != "_all" else "_all",
        sector=key.sector if key.sector != "_all" else "_all",
        experience_band=key.experience_band if key.experience_band != "_all" else "_all",
    )


def compute_analytics(conn, key: ProfileKey) -> dict[str, Any]:
    """Compute the full analytics payload for one profile key.

    Returns a dict ready to be serialised as JSONB and stored in
    dashboard_analytics.analytics.
    """
    payload: dict[str, Any] = {}

    payload["profile"] = _build_profile_metadata(conn, key)
    payload["market"] = _build_market_percentiles(conn, key)
    payload["career_ladder"] = _build_career_ladder(conn, key)
    payload["regions"] = _build_regional_comparison(conn, key)
    payload["sectors"] = _build_sector_comparison(conn, key)
    payload["trends"] = _build_trends(conn, key)
    payload["distribution"] = _build_distribution(conn, key)
    payload["national_benchmark"] = _build_national_benchmark(conn, key)
    payload["metadata"] = _build_metadata(conn, key)

    return payload


def compute_sample_size(analytics: dict) -> int:
    """Extract total sample size from a computed analytics payload."""
    market = analytics.get("market", {})
    regional = market.get("regional_percentiles", {})
    return regional.get("sample_size", 0)


def compute_confidence(analytics: dict) -> float:
    """Derive a confidence score (0.0-1.0) from the analytics payload."""
    market = analytics.get("market", {})
    regional = market.get("regional_percentiles", {})
    sample = regional.get("sample_size", 0)
    filled = sum(1 for p in PCTILE_POINTS
                 if regional.get(f"p{p}") is not None)
    sources = len(analytics.get("metadata", {}).get("sources_used", []))

    # Heuristic: confidence = f(filled_percentiles, sample_size, source_count)
    pctile_score = filled / len(PCTILE_POINTS)              # 0-1
    sample_score = min(1.0, sample / 500)                    # saturates at 500
    source_score = min(1.0, sources / 3)                     # saturates at 3
    return round(pctile_score * 0.4 + sample_score * 0.4 + source_score * 0.2, 2)


# -------------------------------------------------------------------
# Internal builders — each produces one section of the JSONB payload
# -------------------------------------------------------------------

def _build_profile_metadata(conn, key: ProfileKey) -> dict:
    """Resolve human-readable labels for the profile dimensions."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Occupation label
        cur.execute(
            "SELECT label FROM dim_occupation WHERE occupation_code = %s",
            (key.occupation_code,)
        )
        occ_row = cur.fetchone()

        # Location label + is_london flag
        cur.execute(
            "SELECT label, is_london FROM dim_location WHERE location_code = %s",
            (key.location_code,)
        )
        loc_row = cur.fetchone()

    return {
        "occupation_code": key.occupation_code,
        "occupation_label": occ_row["label"] if occ_row else key.occupation_code,
        "location_code": key.location_code,
        "location_label": loc_row["label"] if loc_row else key.location_code,
        "is_london": bool(loc_row["is_london"]) if loc_row else False,
        "sector": key.sector,
        "experience_band": key.experience_band,
    }


def _build_market_percentiles(conn, key: ProfileKey) -> dict:
    """Percentile distribution for the user's exact market segment.

    Returns two objects:
      - regional_percentiles: filtered to user's region
      - overall_percentiles:  same region, all experience bands combined
    """
    regional = _query_percentiles(
        conn, key.occupation_code, key.location_code,
        key.sector, key.experience_band
    )
    overall = _query_percentiles(
        conn, key.occupation_code, key.location_code,
        key.sector, experience_band=None  # all bands
    )
    return {
        "regional_percentiles": regional,
        "overall_percentiles": overall,
    }


def _build_career_ladder(conn, key: ProfileKey) -> list[dict]:
    """Percentiles for each experience band at the same occupation + region.

    This powers the Career Ladder chart and the Potential tab's role
    progression view.  Bands with insufficient data are included with
    a `low_confidence` flag so the frontend can decide to show/hide.
    """
    ladder = []
    for band in BAND_ORDER:
        pctiles = _query_percentiles(
            conn, key.occupation_code, key.location_code,
            key.sector, band
        )
        entry = {
            "band": band,
            "is_user_band": band == key.experience_band,
            **pctiles,
        }
        # Only include bands that have some data
        if pctiles.get("sample_size", 0) > 0 or band == key.experience_band:
            ladder.append(entry)
    return ladder


def _build_regional_comparison(conn, key: ProfileKey) -> list[dict]:
    """Percentiles for the same occupation + sector + band across all regions.

    Each entry includes the COL index from dim_cost_of_living so the frontend
    can compute COL-adjusted figures without a separate query.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Get all regions that have data for this occupation
        cur.execute("""
            SELECT DISTINCT co.location_code,
                   dl.label AS location_label,
                   dl.is_london,
                   COALESCE(col.col_index, 1.000) AS col_index
            FROM compensation_observations co
            JOIN dim_location dl ON dl.location_code = co.location_code
            LEFT JOIN dim_cost_of_living col ON col.location_code = co.location_code
            WHERE co.occupation_code = %s
              AND co.location_code IS NOT NULL
            ORDER BY dl.label
        """, (key.occupation_code,))
        regions = cur.fetchall()

    result = []
    for region in regions:
        loc_code = region["location_code"]
        pctiles = _query_percentiles(
            conn, key.occupation_code, loc_code,
            key.sector, key.experience_band
        )
        if pctiles.get("sample_size", 0) < MIN_SAMPLE_SIZE:
            # Try without band filter for sparse regions
            pctiles = _query_percentiles(
                conn, key.occupation_code, loc_code,
                key.sector, experience_band=None
            )
        if pctiles.get("sample_size", 0) == 0:
            continue

        result.append({
            "location_code": loc_code,
            "label": region["location_label"],
            "is_user_region": loc_code == key.location_code,
            "is_london": bool(region["is_london"]),
            "col_index": float(region["col_index"]),
            **pctiles,
        })

    # Sort by median descending (highest-paying regions first)
    result.sort(key=lambda r: r.get("p50", 0) or 0, reverse=True)
    return result


def _build_sector_comparison(conn, key: ProfileKey) -> list[dict]:
    """Percentiles for each sector at the same occupation + region + band.

    Uses a simple sector classification derived from source metadata.
    For sector-agnostic sources, we query without sector filter.
    """
    # Get sectors that have data for this occupation+region
    sectors_to_query = ["private", "public", "nhs"]
    result = []

    for sector in sectors_to_query:
        pctiles = _query_percentiles(
            conn, key.occupation_code, key.location_code,
            sector, key.experience_band
        )
        if pctiles.get("sample_size", 0) == 0:
            continue

        # Compute YoY growth if we have multi-year data
        yoy = _compute_yoy_growth(
            conn, key.occupation_code, key.location_code,
            sector, key.experience_band
        )

        result.append({
            "sector": sector,
            "is_user_sector": sector == key.sector,
            "yoy_growth_pct": yoy,
            **pctiles,
        })

    result.sort(key=lambda r: r.get("p50", 0) or 0, reverse=True)
    return result


def _build_trends(conn, key: ProfileKey) -> list[dict]:
    """Year-over-year median salary trend for this profile.

    Returns an array of {year, p50, sample_size} objects.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT observed_year AS year,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (
                       ORDER BY normalized_annual_amount
                   ) AS p50,
                   COUNT(*) AS sample_size
            FROM compensation_observations
            WHERE occupation_code = %s
              AND (location_code = %s OR %s IS NULL)
              AND normalized_annual_amount IS NOT NULL
              AND normalized_annual_amount > 0
            GROUP BY observed_year
            ORDER BY observed_year
        """, (key.occupation_code, key.location_code, key.location_code))
        rows = cur.fetchall()

    result = []
    for row in rows:
        result.append({
            "year": row["year"],
            "p50": round(float(row["p50"])) if row["p50"] else None,
            "sample_size": row["sample_size"],
        })
    return result


def _build_distribution(conn, key: ProfileKey) -> dict:
    """Salary distribution histogram for the user's market segment.

    Returns bin_width and an array of {floor, count} objects.  The frontend
    uses this to render the Peers tab distribution chart.
    """
    bin_width = 5000  # £5k bins

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT (FLOOR(normalized_annual_amount / %s) * %s)::INTEGER AS bin_floor,
                   COUNT(*) AS count
            FROM compensation_observations
            WHERE occupation_code = %s
              AND (location_code = %s OR %s IS NULL)
              AND normalized_annual_amount IS NOT NULL
              AND normalized_annual_amount > 0
              AND observation_type IN ('point', 'range')
            GROUP BY bin_floor
            ORDER BY bin_floor
        """, (bin_width, bin_width,
              key.occupation_code, key.location_code, key.location_code))
        rows = cur.fetchall()

    bins = [{"floor": row["bin_floor"], "count": row["count"]} for row in rows]
    total = sum(b["count"] for b in bins)

    return {
        "bin_width": bin_width,
        "bins": bins,
        "total_observations": total,
    }


def _build_national_benchmark(conn, key: ProfileKey) -> dict:
    """National-level percentiles for the same occupation (all regions, all bands).

    Provides a baseline for regional premium calculations.
    """
    return _query_percentiles(
        conn, key.occupation_code, location_code=None,
        sector=None, experience_band=None
    )


def _build_metadata(conn, key: ProfileKey) -> dict:
    """Data provenance: which sources contributed, data window, freshness."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT ds.display_name,
                   ds.confidence_weight
            FROM compensation_observations co
            JOIN dim_source ds ON ds.source_id = co.source_id
            WHERE co.occupation_code = %s
              AND (co.location_code = %s OR %s IS NULL)
        """, (key.occupation_code, key.location_code, key.location_code))
        sources = cur.fetchall()

        cur.execute("""
            SELECT MAX(observed_at) AS latest,
                   MIN(observed_at) AS earliest
            FROM compensation_observations
            WHERE occupation_code = %s
              AND (location_code = %s OR %s IS NULL)
        """, (key.occupation_code, key.location_code, key.location_code))
        window = cur.fetchone()

    return {
        "sources_used": [s["display_name"] for s in sources],
        "source_count": len(sources),
        "avg_confidence_weight": round(
            sum(s["confidence_weight"] for s in sources) / max(len(sources), 1), 2
        ),
        "data_window_start": window["earliest"].isoformat() if window and window["earliest"] else None,
        "data_window_end": window["latest"].isoformat() if window and window["latest"] else None,
        "computed_at": datetime.utcnow().isoformat() + "Z",
    }


# -------------------------------------------------------------------
# Shared query helpers
# -------------------------------------------------------------------

def _query_percentiles(
    conn,
    occupation_code: str,
    location_code: Optional[str],
    sector: Optional[str],
    experience_band: Optional[str],
) -> dict:
    """Compute percentile breakpoints from raw observations.

    Note: '_all' values are treated as None (no filter) so that profiles
    with missing dimensions still get broad percentile data.

    This mirrors the blending logic in the benchmarks engine but is
    simpler — it uses PERCENTILE_CONT on the raw observations table
    directly, since we're computing ahead of time and can afford the
    query cost.

    Falls back to compensation_aggregates when raw point observations
    are sparse.
    """
    # Normalise '_all' sentinel to None so it acts as "no filter"
    if occupation_code == "_all":
        occupation_code = None
    if location_code == "_all":
        location_code = None
    if sector == "_all":
        sector = None
    if experience_band == "_all":
        experience_band = None

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # --- Attempt 1: compute from raw point observations ---
        occ_clause = "AND occupation_code = %s" if occupation_code else ""
        band_clause = "AND experience_band = %s" if experience_band else ""
        location_clause = "AND location_code = %s" if location_code else ""

        params: list[Any] = []
        if occupation_code:
            params.append(occupation_code)
        if location_code:
            params.append(location_code)
        if experience_band:
            params.append(experience_band)

        query = f"""
            SELECT
                COUNT(*) AS sample_size,
                ROUND(AVG(normalized_annual_amount)::NUMERIC, 0) AS mean,
                ROUND(PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p10,
                ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p25,
                ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p50,
                ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p75,
                ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p90
            FROM compensation_observations
            WHERE normalized_annual_amount IS NOT NULL
              AND normalized_annual_amount > 0
              {occ_clause}
              {location_clause}
              {band_clause}
              AND observation_type IN ('point', 'range')
        """
        cur.execute(query, params)
        row = cur.fetchone()

        if row and row["sample_size"] and row["sample_size"] >= MIN_SAMPLE_SIZE:
            return {
                "p10": int(row["p10"]) if row["p10"] else None,
                "p25": int(row["p25"]) if row["p25"] else None,
                "p50": int(row["p50"]) if row["p50"] else None,
                "p75": int(row["p75"]) if row["p75"] else None,
                "p90": int(row["p90"]) if row["p90"] else None,
                "mean": int(row["mean"]) if row["mean"] else None,
                "sample_size": row["sample_size"],
            }

        # --- Attempt 2: fall back to pre-aggregated percentiles ---
        # These come from percentile-type observations (ASHE) and
        # compensation_aggregates (user submissions).
        pctile_map: dict[int, list[float]] = {p: [] for p in PCTILE_POINTS}
        agg_sample = 0

        # From percentile observations (e.g. ASHE Table 2)
        params2: list[Any] = [occupation_code]
        loc_clause2 = ""
        if location_code:
            loc_clause2 = "AND location_code = %s"
            params2.append(location_code)

        cur.execute(f"""
            SELECT percentile, normalized_annual_amount, sample_size
            FROM compensation_observations
            WHERE occupation_code = %s
              {loc_clause2}
              AND observation_type = 'percentile'
              AND percentile IN (10, 25, 50, 75, 90)
              AND normalized_annual_amount IS NOT NULL
            ORDER BY observed_year DESC
            LIMIT 20
        """, params2)

        for prow in cur.fetchall():
            p = prow["percentile"]
            if p in pctile_map:
                pctile_map[p].append(float(prow["normalized_annual_amount"]))
                if prow["sample_size"]:
                    agg_sample = max(agg_sample, prow["sample_size"])

        # From compensation_aggregates
        agg_params: list[Any] = [occupation_code]
        agg_loc = ""
        agg_band = ""
        if location_code:
            agg_loc = "AND location_code = %s"
            agg_params.append(location_code)
        if experience_band:
            agg_band = "AND experience_band = %s"
            agg_params.append(experience_band)

        cur.execute(f"""
            SELECT p10_annual, p25_annual, median_annual, p75_annual, p90_annual,
                   mean_annual, contributor_count
            FROM v_publishable_aggregates
            WHERE occupation_code = %s
              {agg_loc}
              {agg_band}
            ORDER BY quarter DESC
            LIMIT 1
        """, agg_params)
        agg_row = cur.fetchone()

        if agg_row:
            for p, col in [(10, "p10_annual"), (25, "p25_annual"),
                           (50, "median_annual"), (75, "p75_annual"),
                           (90, "p90_annual")]:
                if agg_row[col]:
                    pctile_map[p].append(float(agg_row[col]))
            agg_sample = max(agg_sample, agg_row["contributor_count"] or 0)

        # Average across sources for each percentile point
        result: dict[str, Any] = {"sample_size": agg_sample}
        for p in PCTILE_POINTS:
            vals = pctile_map[p]
            result[f"p{p}"] = int(round(sum(vals) / len(vals))) if vals else None

        # Compute mean from available p50 as fallback
        if result.get("p50"):
            result["mean"] = result["p50"]  # best available approximation

        # Add point observation count if we had any
        if row and row["sample_size"]:
            result["sample_size"] = max(result["sample_size"], row["sample_size"])

        return result


def _compute_yoy_growth(
    conn,
    occupation_code: str,
    location_code: Optional[str],
    sector: Optional[str],
    experience_band: Optional[str],
) -> Optional[float]:
    """Compute year-over-year median growth as a percentage.

    Returns None if insufficient multi-year data.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        params: list[Any] = [occupation_code]
        loc_clause = ""
        if location_code:
            loc_clause = "AND location_code = %s"
            params.append(location_code)

        cur.execute(f"""
            SELECT observed_year,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (
                       ORDER BY normalized_annual_amount
                   ) AS median
            FROM compensation_observations
            WHERE occupation_code = %s
              {loc_clause}
              AND normalized_annual_amount > 0
              AND observation_type IN ('point', 'range')
            GROUP BY observed_year
            HAVING COUNT(*) >= %s
            ORDER BY observed_year DESC
            LIMIT 2
        """, params + [MIN_SAMPLE_SIZE])
        rows = cur.fetchall()

    if len(rows) >= 2 and rows[1]["median"] and rows[1]["median"] > 0:
        current = float(rows[0]["median"])
        previous = float(rows[1]["median"])
        return round((current - previous) / previous * 100, 1)
    return None
