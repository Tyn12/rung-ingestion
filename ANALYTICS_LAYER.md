# Dashboard Analytics Layer

Pre-computed analytics cache that powers the Rung salary dashboard.  Every text claim, bar width, chart data point, and percentile figure traces back deterministically to the dataset — no LLM, no hand-tuning.

## Architecture Overview

```
compensation_observations ─┐
compensation_aggregates   ─┤  nightly refresh
dim_location / dim_source ─┤  ─────────────►  dashboard_analytics (JSONB)
dim_cost_of_living        ─┘                         │
                                                     │  single row lookup
                                                     ▼
                                              GET /api/v1/dashboard
                                                     │
                                                     │  + user salary
                                                     ▼
                                              user-specific metrics
                                              (percentile, gaps, COL adj)
                                                     │
                                                     ▼
                                              frontend templates
```

## Data Flow

1. **Ingestion** — 13+ UK data sources (ASHE, Reed, HMRC, NHS, etc.) feed `compensation_observations` (partitioned by year) and `compensation_aggregates` (user submissions, k-anonymity enforced).

2. **Nightly Refresh** (`python -m ingestion.analytics.refresh`) — discovers all `(occupation_code, location_code, sector, experience_band)` combinations with data, computes a JSONB payload for each, upserts into `dashboard_analytics`.

3. **API Lookup** (`GET /api/v1/dashboard`) — single row fetch by profile key.  Adds user-specific arithmetic (percentile interpolation, salary gaps, COL adjustment) at request time.  Returns the full payload + user metrics.

4. **Frontend Rendering** — templates read the payload fields and do string interpolation only.  Example: `"You're £{user_metrics.regional_gap} {above/below} the {profile.location_label} average"`.

## JSONB Payload Schema

Each `dashboard_analytics.analytics` JSONB blob contains these top-level sections:

### `profile`
Human-readable labels for the profile dimensions.
```json
{
  "occupation_code": "2136",
  "occupation_label": "Senior Software Developer",
  "location_code": "E12000007",
  "location_label": "London",
  "is_london": true,
  "sector": "private",
  "experience_band": "senior"
}
```

### `market`
Percentile distributions for the user's market segment.
```json
{
  "regional_percentiles": {
    "p10": 35000, "p25": 42000, "p50": 52400,
    "p75": 65000, "p90": 82000,
    "mean": 54200, "sample_size": 1247
  },
  "overall_percentiles": {
    "p10": 28000, "p25": 36000, "p50": 48000,
    "p75": 62000, "p90": 78000,
    "mean": 49800, "sample_size": 3420
  }
}
```
- `regional_percentiles` — filtered to user's region + sector + band
- `overall_percentiles` — same region + sector, all experience bands

### `career_ladder`
Percentiles per experience band for the same occupation + region + sector.  Powers the Career Ladder chart and Potential tab.
```json
[
  {"band": "junior", "is_user_band": false, "p50": 32000, "sample_size": 340, ...},
  {"band": "mid",    "is_user_band": false, "p50": 44000, "sample_size": 580, ...},
  {"band": "senior", "is_user_band": true,  "p50": 55000, "sample_size": 420, ...},
  {"band": "lead",   "is_user_band": false, "p50": 74500, "sample_size": 180, ...}
]
```

### `regions`
Cross-regional comparison with COL indices.  Powers the Location tab.
```json
[
  {"location_code": "E12000007", "label": "London", "is_user_region": true,
   "col_index": 1.270, "p50": 52400, "sample_size": 1247, ...},
  {"location_code": "E12000008", "label": "South East", "is_user_region": false,
   "col_index": 1.130, "p50": 45000, "sample_size": 890, ...}
]
```
Sorted by median descending.  Frontend computes COL-adjusted salary as `user_salary / col_index`.

### `sectors`
Cross-sector comparison with YoY growth.  Powers the Industry tab.
```json
[
  {"sector": "private", "is_user_sector": true,
   "yoy_growth_pct": 4.2, "p50": 55000, "sample_size": 980, ...},
  {"sector": "public",  "is_user_sector": false,
   "yoy_growth_pct": 2.1, "p50": 42000, "sample_size": 620, ...}
]
```

### `trends`
Year-over-year median salary time series.
```json
[
  {"year": 2021, "p50": 48000, "sample_size": 900},
  {"year": 2022, "p50": 50000, "sample_size": 1100},
  {"year": 2023, "p50": 51500, "sample_size": 1250},
  {"year": 2024, "p50": 52400, "sample_size": 1247}
]
```

### `distribution`
Salary histogram for the Peers tab distribution chart.
```json
{
  "bin_width": 5000,
  "total_observations": 1247,
  "bins": [
    {"floor": 25000, "count": 45},
    {"floor": 30000, "count": 120},
    {"floor": 35000, "count": 210},
    ...
  ]
}
```

### `national_benchmark`
National-level percentiles (all regions, all bands) for the same occupation.  Used to compute national gaps and regional premiums.
```json
{
  "p10": 28000, "p25": 34000, "p50": 43200,
  "p75": 58000, "p90": 75000,
  "mean": 46100, "sample_size": 8500
}
```

### `metadata`
Data provenance and freshness.
```json
{
  "sources_used": ["ONS ASHE Table 2", "Reed Job Listings", "HMRC PAYE RTI"],
  "source_count": 3,
  "avg_confidence_weight": 0.82,
  "data_window_start": "2021-04-01T00:00:00",
  "data_window_end": "2024-11-15T00:00:00",
  "computed_at": "2025-04-17T02:30:00Z"
}
```

## User-Specific Metrics (API Response)

These are computed at request time from `user_salary` + the pre-computed payload:

| Field | Calculation |
|---|---|
| `percentile` | Linear interpolation between P10/P25/P50/P75/P90 |
| `regional_gap` | `user_salary - regional p50` |
| `regional_gap_pct` | `regional_gap / regional p50 * 100` |
| `national_gap` | `user_salary - national p50` |
| `national_gap_pct` | `national_gap / national p50 * 100` |
| `col_adjusted_salary` | `user_salary / col_index` |

## Database Tables

### `dashboard_analytics`
Primary key: `(occupation_code, location_code, sector, experience_band)`

| Column | Type | Purpose |
|---|---|---|
| `analytics` | JSONB | Full pre-computed payload |
| `sample_size` | INTEGER | Total observations (queryable without parsing JSONB) |
| `confidence` | NUMERIC(3,2) | 0.00–1.00 confidence score |
| `data_freshness_date` | DATE | Latest observation date included |
| `computed_at` | TIMESTAMPTZ | When this row was last refreshed |

Indices: `idx_da_occ_loc` (primary access pattern), `idx_da_computed_at` (staleness checks), `idx_da_analytics_gin` (ad-hoc JSONB queries).

### `dim_cost_of_living`
Primary key: `location_code` (FK → `dim_location`)

Stores regional cost-of-living indices relative to UK average (1.000).  Seeded with ONS Regional Price Parities data.  London = 1.270, South East = 1.130, etc.

## Refresh Cadence

- **Nightly**: full refresh via `python -m ingestion.analytics.refresh`
- **On-demand**: after ingestion runs, `--occupation <code>` flag for targeted refresh
- **Dry-run**: `--dry-run` flag computes without writing (useful for validation)

Typical full refresh: ~500 profile combinations in <60 seconds.

## Security

- `dashboard_analytics` and `dim_cost_of_living` have RLS enabled
- `anon` and `authenticated` roles can SELECT (public data derived from anonymised aggregates)
- Only `service_role` can INSERT/UPDATE/DELETE (used by the refresh job)
- User salary is never stored — it's a query parameter processed in memory only

## Frontend Template Mapping

The JSONB payload is intentionally generic.  Current dashboard tabs map to payload sections as follows:

| Tab | Primary Payload Section(s) |
|---|---|
| Home | `market.regional_percentiles`, `national_benchmark`, `trends` |
| Market | `market`, `career_ladder` |
| Potential | `career_ladder`, `trends` |
| Peers | `distribution`, `market.regional_percentiles` |
| Location | `regions` |
| Industry | `sectors`, `trends` |

New visualisations = new frontend templates reading the same payload.  No schema changes needed unless an entirely new data dimension is required.

## Files

| File | Purpose |
|---|---|
| `schema/migrations/0007_dashboard_analytics_layer.sql` | DDL for tables, indices, RLS, helper function |
| `ingestion/analytics/__init__.py` | Package init |
| `ingestion/analytics/compute.py` | Core computation engine (all builder functions) |
| `ingestion/analytics/refresh.py` | CLI refresh runner (discover → compute → upsert) |
| `rung-web/api/app/routers/dashboard.py` | `GET /api/v1/dashboard` endpoint |
