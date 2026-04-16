-- Rung — compensation_observations base table
-- Yearly partitioning by observed_at so we can detach / archive cheaply.
-- Every ingestion source writes into this one table via bulk_upsert.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS compensation_observations (
    id                          BIGSERIAL,
    source_id                   TEXT NOT NULL,
    source_reference            TEXT NOT NULL,
    occupation_code             TEXT,
    location_code               TEXT,
    company_ref                 TEXT,
    observation_type            TEXT NOT NULL
        CHECK (observation_type IN ('point','range','percentile')),
    value_amount                NUMERIC(14,2),
    value_min                   NUMERIC(14,2),
    value_max                   NUMERIC(14,2),
    percentile                  SMALLINT CHECK (percentile BETWEEN 1 AND 99),
    period                      TEXT NOT NULL
        CHECK (period IN ('annual','weekly','daily','hourly')),
    normalized_annual_amount    NUMERIC(14,2),
    normalization_method_version TEXT,
    currency                    CHAR(3) NOT NULL DEFAULT 'GBP',
    experience_band             TEXT NOT NULL DEFAULT 'unknown'
        CHECK (experience_band IN ('junior','mid','senior','lead','principal','director','unknown')),
    contract_type               TEXT NOT NULL DEFAULT 'unknown'
        CHECK (contract_type IN ('permanent','contract_daily','contract_hourly','part_time','unknown')),
    sample_size                 INTEGER,
    total_comp_annual           NUMERIC(14,2),
    observed_at                 DATE NOT NULL,
    observed_year               SMALLINT NOT NULL,
    source_payload              JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- observed_year must match observed_at. Declared as a CHECK rather than a
    -- GENERATED column because Postgres only permits IMMUTABLE expressions in
    -- generated columns, and EXTRACT/CURRENT_DATE are STABLE. Application
    -- code populates observed_year via CompensationObservation.to_dict().
    CONSTRAINT ck_observed_year_matches
        CHECK (observed_year = EXTRACT(YEAR FROM observed_at)::SMALLINT),

    PRIMARY KEY (id, observed_year),
    CONSTRAINT uq_source UNIQUE (source_id, source_reference, observed_year)
) PARTITION BY RANGE (observed_year);

-- Create one partition per year. Add more as time rolls forward.
CREATE TABLE IF NOT EXISTS compensation_observations_2021
    PARTITION OF compensation_observations FOR VALUES FROM (2021) TO (2022);
CREATE TABLE IF NOT EXISTS compensation_observations_2022
    PARTITION OF compensation_observations FOR VALUES FROM (2022) TO (2023);
CREATE TABLE IF NOT EXISTS compensation_observations_2023
    PARTITION OF compensation_observations FOR VALUES FROM (2023) TO (2024);
CREATE TABLE IF NOT EXISTS compensation_observations_2024
    PARTITION OF compensation_observations FOR VALUES FROM (2024) TO (2025);
CREATE TABLE IF NOT EXISTS compensation_observations_2025
    PARTITION OF compensation_observations FOR VALUES FROM (2025) TO (2026);
CREATE TABLE IF NOT EXISTS compensation_observations_2026
    PARTITION OF compensation_observations FOR VALUES FROM (2026) TO (2027);
CREATE TABLE IF NOT EXISTS compensation_observations_2027
    PARTITION OF compensation_observations FOR VALUES FROM (2027) TO (2028);

-- Hot-path lookup indices (all on the base; Postgres creates one per partition).
CREATE INDEX IF NOT EXISTS idx_co_occupation_location
    ON compensation_observations (occupation_code, location_code);
CREATE INDEX IF NOT EXISTS idx_co_source_id
    ON compensation_observations (source_id);
CREATE INDEX IF NOT EXISTS idx_co_observed_at
    ON compensation_observations (observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_co_company_ref
    ON compensation_observations (company_ref)
    WHERE company_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_co_payload_gin
    ON compensation_observations USING GIN (source_payload);

-- updated_at trigger
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_co_updated_at ON compensation_observations;
CREATE TRIGGER trg_co_updated_at
    BEFORE UPDATE ON compensation_observations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Lightweight dim tables used as FK references later. Unused in MVP writes.
CREATE TABLE IF NOT EXISTS dim_source (
    source_id           TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    provider            TEXT NOT NULL,
    licence             TEXT NOT NULL,
    cadence             TEXT NOT NULL,
    confidence_weight   NUMERIC(3,2) NOT NULL DEFAULT 0.50,
    notes               TEXT,
    added_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO dim_source (source_id, display_name, provider, licence, cadence, confidence_weight, notes) VALUES
    ('nomis_ashe_nm_99_1', 'ONS ASHE via Nomis (SOC 2020)', 'ONS / Nomis', 'OGL v3.0', 'annual',  0.95, 'Official UK earnings survey, SOC 2020 classification'),
    ('nomis_ashe_nm_30_1', 'ONS ASHE via Nomis (SOC 2010, legacy)', 'ONS / Nomis', 'OGL v3.0', 'annual', 0.90, 'Legacy SOC 2010 backfill'),
    ('reed_jobseeker',     'Reed Jobseeker API', 'Reed', 'Reed Developer T&Cs', 'daily', 0.55, 'Advertised salary ranges; noisy but timely'),
    ('hmrc_paye_rti',      'ONS/HMRC Earnings from PAYE RTI', 'ONS / HMRC', 'OGL v3.0', 'monthly', 0.90, 'Monthly PAYE earnings aggregates'),
    ('stackoverflow_survey','Stack Overflow Developer Survey', 'Stack Overflow', 'CC BY-SA 4.0', 'annual', 0.70, 'Self-reported; global but filterable to UK'),
    ('nhs_afc',            'NHS Agenda for Change pay scales', 'NHS Employers', 'OGL v3.0', 'annual', 0.95, 'Authoritative for ~1.5M NHS roles'),
    ('ucu_pay_spine',      'UCEA single pay spine', 'UCEA / UCU', 'OGL v3.0', 'annual', 0.90, 'Higher education; 51 spine points'),
    ('civil_service_pay',  'Civil Service pay bands', 'Cabinet Office', 'OGL v3.0', 'annual', 0.85, 'AA through SCS4 across departments'),
    ('ons_earn',           'ONS EARN01/02/03 average weekly earnings', 'ONS', 'OGL v3.0', 'monthly', 0.85, 'National/industry/region AWE'),
    ('london_datastore',   'London Datastore borough earnings', 'GLA', 'OGL v3.0', 'annual', 0.85, 'Borough-level earnings'),
    ('hmrc_spi',           'HMRC Survey of Personal Incomes', 'HMRC', 'OGL v3.0', 'annual', 0.80, 'Full distribution; ~2 year lag'),
    ('local_gov_transparency', 'Local Government Transparency senior salaries', 'Councils (various)', 'OGL v3.0', 'annual', 0.70, 'Disclosures for officers earning >£50k')
ON CONFLICT (source_id) DO UPDATE
SET display_name       = EXCLUDED.display_name,
    provider           = EXCLUDED.provider,
    licence            = EXCLUDED.licence,
    cadence            = EXCLUDED.cadence,
    confidence_weight  = EXCLUDED.confidence_weight,
    notes              = EXCLUDED.notes;
