-- Rung — User submissions + anonymised aggregates
--
-- Two-table pattern for GDPR compliance:
--
--   user_submissions          Raw individual salary reports. Deletable on
--                             user request (RIGHT TO ERASURE). Retained only
--                             as long as needed to feed aggregates.
--
--   compensation_aggregates   Anonymised statistical buckets. Not personally
--                             identifiable, so exempt from deletion requests.
--                             Survives user data purges. Queryable by the app.
--
-- Flow: user submits salary → row inserted into user_submissions →
--       aggregation runs (on insert and periodically) → bucket in
--       compensation_aggregates updated → user_submissions row can be
--       deleted at any time without losing the statistical contribution.
--
-- k-anonymity threshold: k=3. Buckets with fewer than 3 contributors
-- are suppressed from app queries via the is_publishable flag.

-- ============================================================
-- 1. user_submissions — raw, deletable user salary reports
-- ============================================================

CREATE TABLE IF NOT EXISTS user_submissions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL,           -- FK to auth.users in Supabase
    job_title               TEXT NOT NULL,            -- free-text role title
    occupation_code         TEXT,                     -- SOC code if mapped
    location_code           TEXT,                     -- ONS GSS code (e.g. E12000007)
    location_text           TEXT,                     -- free-text location as entered
    company_name            TEXT,                     -- optional, not published
    company_sector          TEXT,                     -- e.g. "private", "public", "nhs"
    contract_type           TEXT NOT NULL DEFAULT 'permanent'
        CHECK (contract_type IN (
            'permanent', 'contract_daily', 'contract_hourly', 'part_time', 'unknown'
        )),
    experience_years        SMALLINT,
    experience_band         TEXT NOT NULL DEFAULT 'unknown'
        CHECK (experience_band IN (
            'junior', 'mid', 'senior', 'lead', 'principal', 'director', 'unknown'
        )),
    base_salary             NUMERIC(14,2) NOT NULL,  -- annual gross base
    total_compensation      NUMERIC(14,2),           -- base + bonus + equity etc.
    currency                CHAR(3) NOT NULL DEFAULT 'GBP',
    period                  TEXT NOT NULL DEFAULT 'annual'
        CHECK (period IN ('annual', 'weekly', 'daily', 'hourly')),
    normalized_annual       NUMERIC(14,2) NOT NULL,  -- always annual GBP for aggregation
    reported_at             DATE NOT NULL DEFAULT CURRENT_DATE,
    quarter                 TEXT NOT NULL,            -- e.g. '2025-Q2'

    -- Aggregation tracking
    aggregated_at           TIMESTAMPTZ,             -- NULL = not yet aggregated
    is_verified             BOOLEAN NOT NULL DEFAULT FALSE,

    -- Metadata
    source_detail           JSONB NOT NULL DEFAULT '{}'::JSONB,  -- device, referral, etc.
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Quarter must match reported_at
    CONSTRAINT ck_quarter_format
        CHECK (quarter ~ '^\d{4}-Q[1-4]$')
);

-- Indices for user_submissions
CREATE INDEX IF NOT EXISTS idx_us_user_id
    ON user_submissions (user_id);
CREATE INDEX IF NOT EXISTS idx_us_quarter
    ON user_submissions (quarter);
CREATE INDEX IF NOT EXISTS idx_us_not_aggregated
    ON user_submissions (created_at)
    WHERE aggregated_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_us_occupation_location
    ON user_submissions (occupation_code, location_code);

-- updated_at trigger (reuse existing function from migration 0001)
DROP TRIGGER IF EXISTS trg_us_updated_at ON user_submissions;
CREATE TRIGGER trg_us_updated_at
    BEFORE UPDATE ON user_submissions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- 2. compensation_aggregates — anonymised, permanent buckets
-- ============================================================

CREATE TABLE IF NOT EXISTS compensation_aggregates (
    id                      BIGSERIAL PRIMARY KEY,

    -- Bucket dimensions
    quarter                 TEXT NOT NULL,            -- '2025-Q2'
    occupation_code         TEXT,                     -- SOC code or NULL for "all"
    occupation_label        TEXT,                     -- human-readable label
    location_code           TEXT,                     -- GSS code or NULL for "all"
    location_label          TEXT,                     -- human-readable label
    experience_band         TEXT NOT NULL DEFAULT 'unknown'
        CHECK (experience_band IN (
            'junior', 'mid', 'senior', 'lead', 'principal', 'director', 'unknown', 'all'
        )),
    contract_type           TEXT NOT NULL DEFAULT 'all'
        CHECK (contract_type IN (
            'permanent', 'contract_daily', 'contract_hourly', 'part_time', 'unknown', 'all'
        )),
    company_sector          TEXT DEFAULT 'all',

    -- Aggregate statistics (all in annual GBP)
    contributor_count       INTEGER NOT NULL DEFAULT 0,
    mean_annual             NUMERIC(14,2),
    median_annual           NUMERIC(14,2),
    p10_annual              NUMERIC(14,2),
    p25_annual              NUMERIC(14,2),
    p75_annual              NUMERIC(14,2),
    p90_annual              NUMERIC(14,2),
    min_annual              NUMERIC(14,2),
    max_annual              NUMERIC(14,2),
    stddev_annual           NUMERIC(14,2),

    -- Total comp aggregates (if enough data)
    mean_total_comp         NUMERIC(14,2),
    median_total_comp       NUMERIC(14,2),

    -- Data quality
    k_threshold             SMALLINT NOT NULL DEFAULT 3,
    is_publishable          BOOLEAN NOT NULL
        GENERATED ALWAYS AS (contributor_count >= 3) STORED,

    -- Mixed source: user submissions + ingested data can both feed buckets
    source_mix              JSONB NOT NULL DEFAULT '{}'::JSONB,
        -- e.g. {"user_submissions": 5, "reed_jobseeker": 12, "hmrc_paye_rti": 200}

    -- Metadata
    last_aggregated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- One bucket per unique dimension combination
    CONSTRAINT uq_aggregate_bucket UNIQUE (
        quarter, occupation_code, location_code,
        experience_band, contract_type, company_sector
    ),

    CONSTRAINT ck_quarter_format_agg
        CHECK (quarter ~ '^\d{4}-Q[1-4]$')
);

-- Indices for compensation_aggregates
CREATE INDEX IF NOT EXISTS idx_ca_occupation_location
    ON compensation_aggregates (occupation_code, location_code);
CREATE INDEX IF NOT EXISTS idx_ca_quarter
    ON compensation_aggregates (quarter DESC);
CREATE INDEX IF NOT EXISTS idx_ca_publishable
    ON compensation_aggregates (is_publishable)
    WHERE is_publishable = TRUE;
CREATE INDEX IF NOT EXISTS idx_ca_experience
    ON compensation_aggregates (experience_band)
    WHERE is_publishable = TRUE;

-- updated_at trigger
DROP TRIGGER IF EXISTS trg_ca_updated_at ON compensation_aggregates;
CREATE TRIGGER trg_ca_updated_at
    BEFORE UPDATE ON compensation_aggregates
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ============================================================
-- 3. Helper function: compute quarter string from a date
-- ============================================================

CREATE OR REPLACE FUNCTION date_to_quarter(d DATE)
RETURNS TEXT AS $$
BEGIN
    RETURN EXTRACT(YEAR FROM d)::TEXT || '-Q' || EXTRACT(QUARTER FROM d)::TEXT;
END;
$$ LANGUAGE plpgsql IMMUTABLE;


-- ============================================================
-- 4. Aggregation procedure
--
-- Call periodically (e.g. after each batch of submissions) or
-- via pg_cron. Recomputes all buckets for quarters that have
-- un-aggregated submissions.
-- ============================================================

CREATE OR REPLACE PROCEDURE refresh_aggregates()
LANGUAGE plpgsql AS $$
DECLARE
    affected_quarters TEXT[];
BEGIN
    -- Find quarters with new un-aggregated submissions
    SELECT ARRAY_AGG(DISTINCT quarter)
    INTO affected_quarters
    FROM user_submissions
    WHERE aggregated_at IS NULL;

    IF affected_quarters IS NULL OR array_length(affected_quarters, 1) IS NULL THEN
        RAISE NOTICE 'No un-aggregated submissions found.';
        RETURN;
    END IF;

    -- Recompute aggregates for each affected quarter.
    -- This is a full recompute per quarter (not incremental) so that
    -- deletions are correctly reflected.
    INSERT INTO compensation_aggregates (
        quarter, occupation_code, occupation_label,
        location_code, location_label,
        experience_band, contract_type, company_sector,
        contributor_count,
        mean_annual, median_annual,
        p10_annual, p25_annual, p75_annual, p90_annual,
        min_annual, max_annual, stddev_annual,
        mean_total_comp, median_total_comp,
        source_mix, last_aggregated_at
    )
    SELECT
        us.quarter,
        us.occupation_code,
        MAX(us.job_title)        AS occupation_label,
        us.location_code,
        MAX(us.location_text)    AS location_label,
        us.experience_band,
        us.contract_type,
        COALESCE(us.company_sector, 'all'),
        COUNT(*)                                     AS contributor_count,
        ROUND(AVG(us.normalized_annual), 2)          AS mean_annual,
        ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY us.normalized_annual), 2) AS median_annual,
        ROUND(PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY us.normalized_annual), 2) AS p10_annual,
        ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY us.normalized_annual), 2) AS p25_annual,
        ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY us.normalized_annual), 2) AS p75_annual,
        ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY us.normalized_annual), 2) AS p90_annual,
        ROUND(MIN(us.normalized_annual), 2)          AS min_annual,
        ROUND(MAX(us.normalized_annual), 2)          AS max_annual,
        ROUND(STDDEV(us.normalized_annual), 2)       AS stddev_annual,
        ROUND(AVG(us.total_compensation), 2)         AS mean_total_comp,
        ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY us.total_compensation), 2) AS median_total_comp,
        jsonb_build_object('user_submissions', COUNT(*)) AS source_mix,
        NOW()
    FROM user_submissions us
    WHERE us.quarter = ANY(affected_quarters)
    GROUP BY
        us.quarter,
        us.occupation_code,
        us.location_code,
        us.experience_band,
        us.contract_type,
        COALESCE(us.company_sector, 'all')
    ON CONFLICT ON CONSTRAINT uq_aggregate_bucket
    DO UPDATE SET
        contributor_count    = EXCLUDED.contributor_count,
        mean_annual          = EXCLUDED.mean_annual,
        median_annual        = EXCLUDED.median_annual,
        p10_annual           = EXCLUDED.p10_annual,
        p25_annual           = EXCLUDED.p25_annual,
        p75_annual           = EXCLUDED.p75_annual,
        p90_annual           = EXCLUDED.p90_annual,
        min_annual           = EXCLUDED.min_annual,
        max_annual           = EXCLUDED.max_annual,
        stddev_annual        = EXCLUDED.stddev_annual,
        mean_total_comp      = EXCLUDED.mean_total_comp,
        median_total_comp    = EXCLUDED.median_total_comp,
        source_mix           = EXCLUDED.source_mix,
        last_aggregated_at   = NOW();

    -- Mark submissions as aggregated
    UPDATE user_submissions
    SET aggregated_at = NOW()
    WHERE quarter = ANY(affected_quarters)
      AND aggregated_at IS NULL;

    RAISE NOTICE 'Refreshed aggregates for quarters: %', affected_quarters;
END;
$$;


-- ============================================================
-- 5. View: publishable aggregates only (k >= 3)
-- ============================================================

CREATE OR REPLACE VIEW v_publishable_aggregates AS
SELECT
    quarter,
    occupation_code,
    occupation_label,
    location_code,
    location_label,
    experience_band,
    contract_type,
    company_sector,
    contributor_count,
    mean_annual,
    median_annual,
    p10_annual,
    p25_annual,
    p75_annual,
    p90_annual,
    stddev_annual,
    mean_total_comp,
    median_total_comp,
    source_mix,
    last_aggregated_at
FROM compensation_aggregates
WHERE is_publishable = TRUE;
