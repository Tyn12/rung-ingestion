-- 0007_dashboard_analytics_layer.sql
--
-- Pre-computed analytics cache for the salary dashboard.
--
-- Problem: rendering the dashboard currently requires the API to run many
-- cross-dimensional queries (percentiles × regions × sectors × bands × time)
-- per request.  Every text claim, bar width, and chart data point must be
-- deterministically derived from the dataset — no LLM, no hand-tuning.
--
-- Solution: a nightly refresh job pre-computes a single JSONB payload for
-- each (occupation, location, sector, experience_band) combination.  The
-- dashboard API does ONE row lookup and simple user-specific arithmetic
-- (percentile interpolation, gap calculation).
--
-- The JSONB schema is intentionally generic: it stores fundamental building
-- blocks (percentile arrays, regional arrays, sector arrays, time series,
-- distribution bins) that any current or future graph can consume without
-- schema changes.  New visualisations = new frontend templates reading the
-- same payload.
-- -----------------------------------------------------------------------


-- -----------------------------------------------------------------------
-- 1.  Cost-of-living reference table
-- -----------------------------------------------------------------------
-- ONS regional price parities / composite COL index.  Used by the refresh
-- job to add col_index to each region in the analytics payload, so the
-- frontend can show COL-adjusted comparisons without a second query.

CREATE TABLE IF NOT EXISTS dim_cost_of_living (
    location_code   TEXT PRIMARY KEY REFERENCES dim_location(location_code),
    col_index       NUMERIC(5,3) NOT NULL DEFAULT 1.000,
    -- col_index = 1.0 is the national baseline.  London ≈ 1.27.
    source          TEXT,          -- e.g. 'ONS Regional Price Parities 2024'
    reference_year  INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE dim_cost_of_living IS
    'Regional cost-of-living indices relative to UK average (1.000). '
    'Used by the dashboard analytics refresh to embed COL data in the payload.';

-- Seed initial values (ONS Regional Price Parities, approximate 2024 data).
-- These are placeholders — replace with authoritative figures when available.
INSERT INTO dim_cost_of_living (location_code, col_index, source, reference_year) VALUES
    -- Countries & nations
    ('K02000001', 1.000, 'ONS baseline',       2024),   -- United Kingdom
    ('E92000001', 1.020, 'ONS regional prices', 2024),   -- England
    ('S92000003', 0.950, 'ONS regional prices', 2024),   -- Scotland
    ('W92000004', 0.920, 'ONS regional prices', 2024),   -- Wales
    ('N92000002', 0.910, 'ONS regional prices', 2024),   -- Northern Ireland
    -- English regions
    ('E12000001', 0.900, 'ONS regional prices', 2024),   -- North East
    ('E12000002', 0.920, 'ONS regional prices', 2024),   -- North West
    ('E12000003', 0.910, 'ONS regional prices', 2024),   -- Yorkshire & Humber
    ('E12000004', 0.920, 'ONS regional prices', 2024),   -- East Midlands
    ('E12000005', 0.930, 'ONS regional prices', 2024),   -- West Midlands
    ('E12000006', 0.960, 'ONS regional prices', 2024),   -- East of England
    ('E12000007', 1.270, 'ONS regional prices', 2024),   -- London
    ('E12000008', 1.130, 'ONS regional prices', 2024),   -- South East
    ('E12000009', 0.970, 'ONS regional prices', 2024)    -- South West
ON CONFLICT (location_code) DO UPDATE SET
    col_index      = EXCLUDED.col_index,
    source         = EXCLUDED.source,
    reference_year = EXCLUDED.reference_year,
    updated_at     = NOW();


-- -----------------------------------------------------------------------
-- 2.  Dashboard analytics cache
-- -----------------------------------------------------------------------
-- One row per profile combination.  The `analytics` JSONB blob contains
-- everything the frontend needs to render all 6 tabs.  The refresh job
-- recomputes this nightly (or on-demand after ingestion).

CREATE TABLE IF NOT EXISTS dashboard_analytics (
    -- Profile key — matches user_profiles dimensions
    occupation_code TEXT    NOT NULL,
    location_code   TEXT    NOT NULL,   -- user's region (ONS GSS code)
    sector          TEXT    NOT NULL,   -- 'private' | 'public' | 'nhs' | '_all'
    experience_band TEXT    NOT NULL,   -- 'junior'..'principal' | '_all'

    -- The full pre-computed payload (see ANALYTICS_LAYER.md for schema)
    analytics       JSONB   NOT NULL DEFAULT '{}',

    -- Summary metadata (queryable without parsing JSONB)
    sample_size          INTEGER       NOT NULL DEFAULT 0,
    confidence           NUMERIC(3,2)  NOT NULL DEFAULT 0.00,
    data_freshness_date  DATE,         -- latest observation date included
    computed_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    PRIMARY KEY (occupation_code, location_code, sector, experience_band)
);

-- Fast lookup by occupation + location (most common access pattern)
CREATE INDEX IF NOT EXISTS idx_da_occ_loc
    ON dashboard_analytics (occupation_code, location_code);

-- Find stale rows
CREATE INDEX IF NOT EXISTS idx_da_computed_at
    ON dashboard_analytics (computed_at);

-- JSONB GIN for ad-hoc analytics queries against the payload
CREATE INDEX IF NOT EXISTS idx_da_analytics_gin
    ON dashboard_analytics USING GIN (analytics jsonb_path_ops);

COMMENT ON TABLE dashboard_analytics IS
    'Pre-computed dashboard payload per (occupation, location, sector, band). '
    'Refreshed nightly.  The frontend reads one row and does arithmetic only.';


-- -----------------------------------------------------------------------
-- 3.  Row-Level Security
-- -----------------------------------------------------------------------
-- Analytics are public data (derived from anonymised aggregates).
-- anon and authenticated can SELECT; only service_role can write.

ALTER TABLE dashboard_analytics ENABLE ROW LEVEL SECURITY;
ALTER TABLE dim_cost_of_living  ENABLE ROW LEVEL SECURITY;

-- Read policies
CREATE POLICY da_select_anon ON dashboard_analytics
    FOR SELECT TO anon USING (true);
CREATE POLICY da_select_auth ON dashboard_analytics
    FOR SELECT TO authenticated USING (true);

CREATE POLICY col_select_anon ON dim_cost_of_living
    FOR SELECT TO anon USING (true);
CREATE POLICY col_select_auth ON dim_cost_of_living
    FOR SELECT TO authenticated USING (true);

-- Write policies (service_role bypasses RLS, but explicit for clarity)
CREATE POLICY da_write_service ON dashboard_analytics
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY col_write_service ON dim_cost_of_living
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Revoke direct writes from non-service roles
REVOKE INSERT, UPDATE, DELETE ON dashboard_analytics FROM anon, authenticated;
REVOKE INSERT, UPDATE, DELETE ON dim_cost_of_living  FROM anon, authenticated;


-- -----------------------------------------------------------------------
-- 4.  Helper function: compute user percentile from a JSONB percentiles object
-- -----------------------------------------------------------------------
-- Used by the dashboard API to avoid re-implementing interpolation in Python.
-- Input:  percentiles JSONB like {"p10": 38000, "p25": 45000, ...}
--         user_salary NUMERIC
-- Output: integer 1-99

CREATE OR REPLACE FUNCTION compute_percentile_of_salary(
    pctiles JSONB,
    user_salary NUMERIC
) RETURNS INTEGER
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    points   INT[]   := ARRAY[10, 25, 50, 75, 90];
    vals     NUMERIC[];
    i        INT;
    lower_p  INT;
    lower_v  NUMERIC;
    upper_p  INT;
    upper_v  NUMERIC;
    result   NUMERIC;
BEGIN
    -- Extract values
    vals := ARRAY[
        (pctiles->>'p10')::NUMERIC,
        (pctiles->>'p25')::NUMERIC,
        (pctiles->>'p50')::NUMERIC,
        (pctiles->>'p75')::NUMERIC,
        (pctiles->>'p90')::NUMERIC
    ];

    -- Below P10
    IF user_salary <= vals[1] THEN
        RETURN GREATEST(1, (user_salary / NULLIF(vals[1], 0) * 10)::INT);
    END IF;

    -- Above P90
    IF user_salary >= vals[5] THEN
        RETURN LEAST(99, (90 + (user_salary - vals[5]) / NULLIF(vals[5] - vals[4], 1) * 9)::INT);
    END IF;

    -- Interpolate between bracketing points
    FOR i IN 1..4 LOOP
        IF user_salary >= vals[i] AND user_salary <= vals[i+1] THEN
            lower_p := points[i];
            lower_v := vals[i];
            upper_p := points[i+1];
            upper_v := vals[i+1];

            IF upper_v = lower_v THEN
                result := (lower_p + upper_p) / 2.0;
            ELSE
                result := lower_p + (user_salary - lower_v) / (upper_v - lower_v)
                          * (upper_p - lower_p);
            END IF;

            RETURN GREATEST(1, LEAST(99, result::INT));
        END IF;
    END LOOP;

    RETURN 50;  -- fallback
END;
$$;

COMMENT ON FUNCTION compute_percentile_of_salary IS
    'Linear interpolation of user salary against P10/P25/P50/P75/P90 breakpoints. '
    'Returns integer 1-99.  Used by the dashboard API for user-specific metrics.';
