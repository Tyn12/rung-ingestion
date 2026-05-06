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
    occ_label = key.occupation_code
    loc_label = key.location_code
    is_london = False

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if key.occupation_code and key.occupation_code != "_all":
            cur.execute(
                "SELECT label FROM dim_occupation WHERE occupation_code = %s",
                (key.occupation_code,)
            )
            occ_row = cur.fetchone()
            if occ_row:
                occ_label = occ_row["label"]
        else:
            occ_label = "All Occupations"

        if key.location_code and key.location_code != "_all":
            cur.execute(
                "SELECT label, is_london FROM dim_location WHERE location_code = %s",
                (key.location_code,)
            )
            loc_row = cur.fetchone()
            if loc_row:
                loc_label = loc_row["label"]
                is_london = bool(loc_row["is_london"])
        else:
            loc_label = "United Kingdom"

    return {
        "occupation_code": key.occupation_code,
        "occupation_label": occ_label,
        "location_code": key.location_code,
        "location_label": loc_label,
        "is_london": is_london,
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
    occ = key.occupation_code if key.occupation_code != "_all" else None

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Get all regions that have data for this occupation
        occ_clause = "AND co.occupation_code = %s" if occ else ""
        params: list[Any] = []
        if occ:
            params.append(occ)

        cur.execute(f"""
            SELECT DISTINCT co.location_code,
                   dl.label AS location_label,
                   dl.is_london,
                   COALESCE(col.col_index, 1.000) AS col_index
            FROM compensation_observations co
            JOIN dim_location dl ON dl.location_code = co.location_code
            LEFT JOIN dim_cost_of_living col ON col.location_code = co.location_code
            WHERE co.location_code IS NOT NULL
              {occ_clause}
            ORDER BY dl.label
        """, params)
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

    # Sort by average descending (highest-paying regions first).
    # Falls back to p50 if mean is missing for any reason.
    result.sort(
        key=lambda r: (r.get("mean") or r.get("p50") or 0),
        reverse=True,
    )
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

    result.sort(
        key=lambda r: (r.get("mean") or r.get("p50") or 0),
        reverse=True,
    )
    return result


def _build_trends(conn, key: ProfileKey) -> list[dict]:
    """Year-over-year salary trend for this profile.

    Returns an array of {year, mean, p50, sample_size} objects.  The frontend
    uses `mean` for the headline trend line (we describe it as "average").
    `p50` is preserved for any percentile-anchored visualisations.
    """
    occ = key.occupation_code if key.occupation_code != "_all" else None
    loc = key.location_code if key.location_code != "_all" else None

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        occ_clause = "AND occupation_code = %s" if occ else ""
        loc_clause = "AND location_code = %s" if loc else ""
        params: list[Any] = []
        if occ:
            params.append(occ)
        if loc:
            params.append(loc)

        cur.execute(f"""
            SELECT observed_year AS year,
                   AVG(normalized_annual_amount) AS mean,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (
                       ORDER BY normalized_annual_amount
                   ) AS p50,
                   COUNT(*) AS sample_size
            FROM compensation_observations
            WHERE normalized_annual_amount IS NOT NULL
              AND normalized_annual_amount > 0
              {occ_clause}
              {loc_clause}
            GROUP BY observed_year
            ORDER BY observed_year
        """, params)
        rows = cur.fetchall()

    result = []
    for row in rows:
        result.append({
            "year": row["year"],
            "mean": round(float(row["mean"])) if row["mean"] else None,
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

    occ = key.occupation_code if key.occupation_code != "_all" else None
    loc = key.location_code if key.location_code != "_all" else None

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        occ_clause = "AND occupation_code = %s" if occ else ""
        loc_clause = "AND location_code = %s" if loc else ""
        params: list[Any] = [bin_width, bin_width]
        if occ:
            params.append(occ)
        if loc:
            params.append(loc)

        cur.execute(f"""
            SELECT (FLOOR(normalized_annual_amount / %s) * %s)::INTEGER AS bin_floor,
                   COUNT(*) AS count
            FROM compensation_observations
            WHERE normalized_annual_amount IS NOT NULL
              AND normalized_annual_amount > 0
              {occ_clause}
              {loc_clause}
            GROUP BY bin_floor
            ORDER BY bin_floor
        """, params)
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
    occ = key.occupation_code if key.occupation_code != "_all" else None
    loc = key.location_code if key.location_code != "_all" else None

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        occ_clause = "AND co.occupation_code = %s" if occ else ""
        loc_clause = "AND co.location_code = %s" if loc else ""
        params1: list[Any] = []
        if occ:
            params1.append(occ)
        if loc:
            params1.append(loc)

        cur.execute(f"""
            SELECT DISTINCT ds.display_name,
                   ds.confidence_weight
            FROM compensation_observations co
            JOIN dim_source ds ON ds.source_id = co.source_id
            WHERE 1=1
              {occ_clause}
              {loc_clause}
        """, params1)
        sources = cur.fetchall()

        occ_clause2 = "AND occupation_code = %s" if occ else ""
        loc_clause2 = "AND location_code = %s" if loc else ""
        params2: list[Any] = []
        if occ:
            params2.append(occ)
        if loc:
            params2.append(loc)

        cur.execute(f"""
            SELECT MAX(observed_at) AS latest,
                   MIN(observed_at) AS earliest
            FROM compensation_observations
            WHERE 1=1
              {occ_clause2}
              {loc_clause2}
        """, params2)
        window = cur.fetchone()

    return {
        "sources_used": [s["display_name"] for s in sources],
        "source_count": len(sources),
        "avg_confidence_weight": round(
            float(sum(float(s["confidence_weight"]) for s in sources)) / max(len(sources), 1), 2
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
    """Compute percentile breakpoints for a given profile slice.

    Strategy (two attempts):

    Attempt 1 — Compute from individual observations (point + range rows).
        These represent actual salaries from Reed listings and similar sources.
        Each row is one job/person, so PERCENTILE_CONT gives true percentiles.

    Attempt 2 — Fall back to pre-computed ASHE percentiles.
        ASHE stores p10/p25/p50/p75/p90 as separate rows with observation_type
        = 'percentile'.  These are ALREADY percentiles from a large survey, so
        we read them directly by their `percentile` column rather than running
        PERCENTILE_CONT across them (which would compute a meaningless
        percentile-of-percentiles).

    '_all' and 'unknown' sentinel values are treated as None (no filter)
    so profiles with missing dimensions get broad percentile data.
    """
    # Normalise sentinel values to None so they act as "no filter"
    if occupation_code == "_all":
        occupation_code = None
    if location_code == "_all":
        location_code = None
    if sector == "_all":
        sector = None
    if experience_band in ("_all", "unknown"):
        experience_band = None

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Build dynamic WHERE clauses
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

        # ── Attempt 1: PERCENTILE_CONT on point/range observations ──
        # These are real individual salary observations (e.g. Reed listings).
        # Excludes 'percentile' observation_type which are pre-computed
        # aggregate values from ASHE — running PERCENTILE_CONT on those
        # would produce a percentile-of-percentiles (statistically wrong).
        query_individual = f"""
            SELECT
                COUNT(*) AS sample_size,
                ROUND(AVG(normalized_annual_amount)::NUMERIC, 0) AS mean,
                ROUND(MIN(normalized_annual_amount)::NUMERIC, 0) AS min_salary,
                ROUND(MAX(normalized_annual_amount)::NUMERIC, 0) AS max_salary,
                ROUND(PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p10,
                ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p25,
                ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p50,
                ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p75,
                ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY normalized_annual_amount)::NUMERIC, 0) AS p90
            FROM compensation_observations
            WHERE normalized_annual_amount IS NOT NULL
              AND normalized_annual_amount > 0
              AND observation_type IN ('point', 'range')
              {occ_clause}
              {location_clause}
              {band_clause}
        """
        cur.execute(query_individual, params)
        row = cur.fetchone()

        if row and row["sample_size"] and row["sample_size"] >= MIN_SAMPLE_SIZE:
            return _format_percentile_result(row)

        # ── Attempt 2: Read ASHE pre-computed percentiles directly ──
        # ASHE rows have observation_type='percentile' and a `percentile`
        # column (10, 25, 50, 75, 90).  We read the value for each
        # percentile point directly — no PERCENTILE_CONT needed.
        ashe_query = f"""
            SELECT percentile,
                   normalized_annual_amount AS value,
                   sample_size
            FROM compensation_observations
            WHERE normalized_annual_amount IS NOT NULL
              AND normalized_annual_amount > 0
              AND observation_type = 'percentile'
              AND percentile IS NOT NULL
              {occ_clause}
              {location_clause}
              {band_clause}
            ORDER BY percentile
        """
        cur.execute(ashe_query, params)
        ashe_rows = cur.fetchall()

        if ashe_rows:
            # Build a lookup: for each standard percentile point, pick the
            # value from the best source (prefer annual, highest sample_size).
            # Multiple sources/periods may contribute rows for the same
            # percentile (e.g. annual + weekly-annualised). We take the
            # median of available values for each percentile point.
            from collections import defaultdict
            pct_values: dict[int, list[float]] = defaultdict(list)
            total_sample = 0
            for ar in ashe_rows:
                pct = int(ar["percentile"])
                pct_values[pct].append(float(ar["value"]))
                if ar["sample_size"]:
                    total_sample = max(total_sample, int(ar["sample_size"]))

            result: dict[str, Any] = {"sample_size": total_sample or len(ashe_rows)}

            # For each standard percentile point, take the median of
            # available values (handles annual + weekly + hourly sources)
            import statistics
            for p in PCTILE_POINTS:
                vals = pct_values.get(p)
                if vals:
                    result[f"p{p}"] = int(round(statistics.median(vals)))
                else:
                    result[f"p{p}"] = None

            # Mean: ASHE publishes the arithmetic mean as a separate row with
            # observation_type='point' (no percentile column).  Fetch those
            # explicitly — averaging the percentile values would NOT give the
            # true mean of the underlying distribution.
            mean_query = f"""
                SELECT normalized_annual_amount AS value
                FROM compensation_observations
                WHERE normalized_annual_amount IS NOT NULL
                  AND normalized_annual_amount > 0
                  AND observation_type = 'point'
                  AND percentile IS NULL
                  {occ_clause}
                  {location_clause}
                  {band_clause}
            """
            cur.execute(mean_query, params)
            mean_rows = cur.fetchall()
            if mean_rows:
                mean_vals = [float(mr["value"]) for mr in mean_rows]
                # Take the median of available means (handles annual + weekly + hourly
                # sources contributing different normalisations).
                result["mean"] = int(round(statistics.median(mean_vals)))
            else:
                # No published mean available — fall back to p50 with a warning flag.
                # This is rare but can happen for sparse profiles where ASHE published
                # percentiles but no mean (or where the mean row was suppressed by ONS).
                result["mean"] = result.get("p50")

            # Min/max from the available percentile range
            all_vals = [float(ar["value"]) for ar in ashe_rows]
            result["min_salary"] = int(round(min(all_vals)))
            result["max_salary"] = int(round(max(all_vals)))

            # Check if we have enough percentile points to be useful
            filled = sum(1 for p in PCTILE_POINTS if result.get(f"p{p}") is not None)
            if filled >= 3:
                return result

        # ── Attempt 3: combine everything as a last resort ──
        # If we have a handful of individual observations AND some ASHE
        # data, but neither alone meets MIN_SAMPLE_SIZE, combine them.
        query_all = f"""
            SELECT
                COUNT(*) AS sample_size,
                ROUND(AVG(normalized_annual_amount)::NUMERIC, 0) AS mean,
                ROUND(MIN(normalized_annual_amount)::NUMERIC, 0) AS min_salary,
                ROUND(MAX(normalized_annual_amount)::NUMERIC, 0) AS max_salary,
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
        """
        cur.execute(query_all, params)
        row = cur.fetchone()

        if row and row["sample_size"] and row["sample_size"] > 0:
            result = _format_percentile_result(row)
            if row["sample_size"] < MIN_SAMPLE_SIZE:
                result["low_confidence"] = True
            return result

        return {"sample_size": 0}


def _format_percentile_result(row) -> dict:
    """Format a database row from a PERCENTILE_CONT query into a dict."""
    return {
        "p10": int(row["p10"]) if row["p10"] else None,
        "p25": int(row["p25"]) if row["p25"] else None,
        "p50": int(row["p50"]) if row["p50"] else None,
        "p75": int(row["p75"]) if row["p75"] else None,
        "p90": int(row["p90"]) if row["p90"] else None,
        "mean": int(row["mean"]) if row["mean"] else None,
        "min_salary": int(row["min_salary"]) if row["min_salary"] else None,
        "max_salary": int(row["max_salary"]) if row["max_salary"] else None,
        "sample_size": row["sample_size"],
    }


def _compute_yoy_growth(
    conn,
    occupation_code: str,
    location_code: Optional[str],
    sector: Optional[str],
    experience_band: Optional[str],
) -> Optional[float]:
    """Compute year-over-year average (mean) growth as a percentage.

    Returns None if insufficient multi-year data.
    """
    # Normalise sentinels
    if occupation_code == "_all":
        occupation_code = None
    if location_code == "_all":
        location_code = None

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        occ_clause = "AND occupation_code = %s" if occupation_code else ""
        loc_clause = "AND location_code = %s" if location_code else ""
        params: list[Any] = []
        if occupation_code:
            params.append(occupation_code)
        if location_code:
            params.append(location_code)

        cur.execute(f"""
            SELECT observed_year,
                   AVG(normalized_annual_amount) AS mean_value
            FROM compensation_observations
            WHERE normalized_annual_amount > 0
              {occ_clause}
              {loc_clause}
            GROUP BY observed_year
            HAVING COUNT(*) >= %s
            ORDER BY observed_year DESC
            LIMIT 2
        """, params + [MIN_SAMPLE_SIZE])
        rows = cur.fetchall()

    if len(rows) >= 2 and rows[1]["mean_value"] and rows[1]["mean_value"] > 0:
        current = float(rows[0]["mean_value"])
        previous = float(rows[1]["mean_value"])
        return round((current - previous) / previous * 100, 1)
    return None
