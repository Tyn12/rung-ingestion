-- Rung — Dimension tables for occupation and location
--
-- Referenced by compensation_observations.occupation_code /
-- compensation_observations.location_code, and by user_submissions and
-- user_profiles. FK enforcement is intentionally deferred (many ingestion
-- sources use codes we haven't yet seeded), but the tables exist so the
-- app can join and render labels.
--
-- Seeding strategy: start with the subset of codes already present in
-- compensation_observations (populated by running ingestion pipelines),
-- plus a manually-curated minimum set for common UK regions. Full SOC
-- 2020 and ONS GSS backfill is a later data-layer ticket.

-- ============================================================
-- 1. dim_occupation — SOC 2020 4-digit codes + labels + embeddings
-- ============================================================

CREATE TABLE IF NOT EXISTS dim_occupation (
    occupation_code         TEXT PRIMARY KEY,       -- SOC 2020, 4-digit (e.g. '2136')
    label                   TEXT NOT NULL,
    soc_2010_equivalent     TEXT,                   -- For historical cross-walk
    major_group             TEXT,                   -- SOC 2020 major group code (1 digit)
    major_group_label       TEXT,
    description             TEXT,
    -- 1536-dim vectors match OpenAI text-embedding-3-small / Anthropic Voyage voyage-3-lite.
    -- Populated by a later pipeline that embeds ESCO skill descriptions.
    embedding               vector(1536),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_do_major_group
    ON dim_occupation (major_group);

-- IVFFlat index for fast approximate nearest-neighbour search on embeddings.
-- Tuned for ~10k rows; rebuild with --lists=100 at ~100k.
CREATE INDEX IF NOT EXISTS idx_do_embedding_ivfflat
    ON dim_occupation USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 20);

DROP TRIGGER IF EXISTS trg_do_updated_at ON dim_occupation;
CREATE TRIGGER trg_do_updated_at
    BEFORE UPDATE ON dim_occupation
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ============================================================
-- 2. dim_location — ONS GSS codes + labels + hierarchy
-- ============================================================

CREATE TABLE IF NOT EXISTS dim_location (
    location_code           TEXT PRIMARY KEY,       -- e.g. 'E12000007' (London region)
    label                   TEXT NOT NULL,
    location_type           TEXT NOT NULL
        CHECK (location_type IN (
            'country', 'region', 'county',
            'local_authority', 'borough', 'postcode_area'
        )),
    parent_code             TEXT REFERENCES dim_location(location_code),
    country_code            TEXT NOT NULL DEFAULT 'GB-ENG'
        CHECK (country_code IN ('GB-ENG', 'GB-SCT', 'GB-WLS', 'GB-NIR', 'GB')),
    is_london               BOOLEAN NOT NULL DEFAULT FALSE,   -- used for London premium lookups
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dl_parent ON dim_location (parent_code);
CREATE INDEX IF NOT EXISTS idx_dl_type   ON dim_location (location_type);
CREATE INDEX IF NOT EXISTS idx_dl_london ON dim_location (is_london) WHERE is_london = TRUE;

DROP TRIGGER IF EXISTS trg_dl_updated_at ON dim_location;
CREATE TRIGGER trg_dl_updated_at
    BEFORE UPDATE ON dim_location
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ============================================================
-- 3. Minimal seed for UK regions and nations
--
-- Codes follow the ONS GSS pattern: E12 = England region,
-- W92 / S92 / N92 = country-level for Wales / Scotland / NI,
-- E09 = London borough, E08 = metropolitan district, etc.
-- ============================================================

INSERT INTO dim_location (location_code, label, location_type, parent_code, country_code, is_london) VALUES
    ('K02000001', 'United Kingdom',           'country', NULL,        'GB',     FALSE),
    ('E92000001', 'England',                  'country', 'K02000001', 'GB-ENG', FALSE),
    ('W92000004', 'Wales',                    'country', 'K02000001', 'GB-WLS', FALSE),
    ('S92000003', 'Scotland',                 'country', 'K02000001', 'GB-SCT', FALSE),
    ('N92000002', 'Northern Ireland',         'country', 'K02000001', 'GB-NIR', FALSE),
    ('E12000001', 'North East',               'region',  'E92000001', 'GB-ENG', FALSE),
    ('E12000002', 'North West',               'region',  'E92000001', 'GB-ENG', FALSE),
    ('E12000003', 'Yorkshire and The Humber', 'region',  'E92000001', 'GB-ENG', FALSE),
    ('E12000004', 'East Midlands',            'region',  'E92000001', 'GB-ENG', FALSE),
    ('E12000005', 'West Midlands',            'region',  'E92000001', 'GB-ENG', FALSE),
    ('E12000006', 'East of England',          'region',  'E92000001', 'GB-ENG', FALSE),
    ('E12000007', 'London',                   'region',  'E92000001', 'GB-ENG', TRUE),
    ('E12000008', 'South East',               'region',  'E92000001', 'GB-ENG', FALSE),
    ('E12000009', 'South West',               'region',  'E92000001', 'GB-ENG', FALSE)
ON CONFLICT (location_code) DO UPDATE
SET label       = EXCLUDED.label,
    parent_code = EXCLUDED.parent_code,
    is_london   = EXCLUDED.is_london,
    updated_at  = NOW();


-- ============================================================
-- 4. Minimal seed for SOC 2020 major groups
--
-- Major groups only (1-digit codes) — full 4-digit SOC 2020 seed lands in
-- a later data-layer ticket once we decide between embedding from ONS
-- classification files or ESCO cross-walk.
-- ============================================================

INSERT INTO dim_occupation (occupation_code, label, major_group, major_group_label) VALUES
    ('1', 'Managers, directors and senior officials (major group)',            '1', 'Managers, directors and senior officials'),
    ('2', 'Professional occupations (major group)',                            '2', 'Professional occupations'),
    ('3', 'Associate professional occupations (major group)',                  '3', 'Associate professional occupations'),
    ('4', 'Administrative and secretarial occupations (major group)',          '4', 'Administrative and secretarial occupations'),
    ('5', 'Skilled trades occupations (major group)',                          '5', 'Skilled trades occupations'),
    ('6', 'Caring, leisure and other service occupations (major group)',       '6', 'Caring, leisure and other service occupations'),
    ('7', 'Sales and customer service occupations (major group)',              '7', 'Sales and customer service occupations'),
    ('8', 'Process, plant and machine operatives (major group)',               '8', 'Process, plant and machine operatives'),
    ('9', 'Elementary occupations (major group)',                              '9', 'Elementary occupations')
ON CONFLICT (occupation_code) DO UPDATE
SET label             = EXCLUDED.label,
    major_group       = EXCLUDED.major_group,
    major_group_label = EXCLUDED.major_group_label,
    updated_at        = NOW();


-- ============================================================
-- 5. Grants (must match 0003 pattern — these tables are public reference).
-- ============================================================

ALTER TABLE dim_occupation ENABLE ROW LEVEL SECURITY;
ALTER TABLE dim_location   ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS docc_select_all ON dim_occupation;
CREATE POLICY docc_select_all ON dim_occupation FOR SELECT USING (TRUE);

DROP POLICY IF EXISTS dloc_select_all ON dim_location;
CREATE POLICY dloc_select_all ON dim_location FOR SELECT USING (TRUE);

GRANT SELECT ON dim_occupation TO anon, authenticated;
GRANT SELECT ON dim_location   TO anon, authenticated;
REVOKE INSERT, UPDATE, DELETE ON dim_occupation FROM anon, authenticated;
REVOKE INSERT, UPDATE, DELETE ON dim_location   FROM anon, authenticated;
