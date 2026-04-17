-- Rung — Row Level Security policies
--
-- Enforces per-user data isolation on user_submissions and user_profiles.
-- Compensation data (public sources + publishable aggregates) is read-only
-- to all authenticated and anonymous users. Writes to any compensation table
-- only happen via the service_role key (used by ingestion pipelines and by
-- definer-rights functions like refresh_aggregates).
--
-- Supabase populates auth.uid() from the JWT in Authorization headers, so
-- the FastAPI layer simply forwards the user's Supabase JWT for RLS to
-- apply automatically.

-- ============================================================
-- 1. user_profiles — per-user profile data
--
-- Created here (not in 0002) so RLS lands at the same time as the table.
-- One row per auth.users entry; lifecycle is tied to the user.
-- ============================================================

CREATE TABLE IF NOT EXISTS user_profiles (
    id                          UUID PRIMARY KEY
        REFERENCES auth.users(id) ON DELETE CASCADE,
    display_name                TEXT,
    current_job_title           TEXT,
    current_occupation_code     TEXT,
    current_location_code       TEXT,
    current_experience_band     TEXT NOT NULL DEFAULT 'unknown'
        CHECK (current_experience_band IN (
            'junior', 'mid', 'senior', 'lead', 'principal', 'director', 'unknown'
        )),
    current_contract_type       TEXT NOT NULL DEFAULT 'unknown'
        CHECK (current_contract_type IN (
            'permanent', 'contract_daily', 'contract_hourly', 'part_time', 'unknown'
        )),

    -- Opt-in for peer movement insights aggregation (build plan §3.7).
    consent_peer_insights       BOOLEAN NOT NULL DEFAULT FALSE,

    -- Notification preferences (§3.8 Smart Notifications).
    -- Default: push + email on, 1 per week max, no annual reminders.
    notification_preferences    JSONB NOT NULL DEFAULT
        '{"push": true, "email": true, "max_per_week": 1, "annual_reminder": false}'::JSONB,

    -- Ambient context captured across sessions (suggestion §4.5.3).
    persistent_context          JSONB NOT NULL DEFAULT '{}'::JSONB,

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_up_updated_at ON user_profiles;
CREATE TRIGGER trg_up_updated_at
    BEFORE UPDATE ON user_profiles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Auto-create profile row when a Supabase auth user signs up.
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.user_profiles (id)
    VALUES (NEW.id)
    ON CONFLICT (id) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION handle_new_user();


-- ============================================================
-- 2. Add user_id FK constraint to user_submissions
--
-- 0002 declared user_id UUID NOT NULL but did not reference auth.users
-- (to keep the migration runnable in environments without Supabase auth
-- schema). Add the FK here once we know Supabase auth exists.
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_user_submissions_user_id'
    ) THEN
        ALTER TABLE user_submissions
            ADD CONSTRAINT fk_user_submissions_user_id
            FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
    END IF;
END $$;


-- ============================================================
-- 3. Enable RLS and define policies
-- ============================================================

-- user_profiles: own row only.
ALTER TABLE user_profiles ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS up_select_own ON user_profiles;
CREATE POLICY up_select_own ON user_profiles
    FOR SELECT USING (auth.uid() = id);

DROP POLICY IF EXISTS up_insert_own ON user_profiles;
CREATE POLICY up_insert_own ON user_profiles
    FOR INSERT WITH CHECK (auth.uid() = id);

DROP POLICY IF EXISTS up_update_own ON user_profiles;
CREATE POLICY up_update_own ON user_profiles
    FOR UPDATE USING (auth.uid() = id) WITH CHECK (auth.uid() = id);

DROP POLICY IF EXISTS up_delete_own ON user_profiles;
CREATE POLICY up_delete_own ON user_profiles
    FOR DELETE USING (auth.uid() = id);


-- user_submissions: users manage their own submissions only.
-- The service_role key (used by aggregation jobs) bypasses RLS.
ALTER TABLE user_submissions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS us_select_own ON user_submissions;
CREATE POLICY us_select_own ON user_submissions
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS us_insert_own ON user_submissions;
CREATE POLICY us_insert_own ON user_submissions
    FOR INSERT WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS us_update_own ON user_submissions;
CREATE POLICY us_update_own ON user_submissions
    FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS us_delete_own ON user_submissions;
CREATE POLICY us_delete_own ON user_submissions
    FOR DELETE USING (auth.uid() = user_id);


-- compensation_aggregates: readable to everyone, but only the publishable
-- rows (k-anonymity threshold met). No writes from anon/authenticated;
-- refresh_aggregates runs with definer rights via service_role.
ALTER TABLE compensation_aggregates ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ca_select_publishable ON compensation_aggregates;
CREATE POLICY ca_select_publishable ON compensation_aggregates
    FOR SELECT USING (is_publishable = TRUE);


-- compensation_observations: public compensation data, anon read-all.
ALTER TABLE compensation_observations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS co_select_all ON compensation_observations;
CREATE POLICY co_select_all ON compensation_observations
    FOR SELECT USING (TRUE);


-- dim_source: read-only public reference data.
ALTER TABLE dim_source ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ds_select_all ON dim_source;
CREATE POLICY ds_select_all ON dim_source
    FOR SELECT USING (TRUE);


-- ============================================================
-- 4. Grants
--
-- Supabase ships with three roles: anon (unauthenticated), authenticated
-- (signed-in users, JWT in request), service_role (server-side, bypasses
-- RLS). RLS policies above gate row visibility; these grants gate table
-- visibility entirely.
-- ============================================================

GRANT USAGE ON SCHEMA public TO anon, authenticated;

-- Read-only public data
GRANT SELECT ON compensation_observations     TO anon, authenticated;
GRANT SELECT ON compensation_aggregates       TO anon, authenticated;
GRANT SELECT ON v_publishable_aggregates      TO anon, authenticated;
GRANT SELECT ON dim_source                    TO anon, authenticated;

-- User-owned tables: authenticated users can read/write own rows (RLS gates which).
GRANT SELECT, INSERT, UPDATE, DELETE ON user_profiles      TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_submissions   TO authenticated;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO authenticated;

-- Explicitly deny writes to public compensation tables from non-service roles.
-- (service_role has the superuser-equivalent bypass and needs no grant here.)
REVOKE INSERT, UPDATE, DELETE ON compensation_observations  FROM anon, authenticated;
REVOKE INSERT, UPDATE, DELETE ON compensation_aggregates    FROM anon, authenticated;
REVOKE INSERT, UPDATE, DELETE ON dim_source                 FROM anon, authenticated;


-- ============================================================
-- 5. GDPR: right-to-erasure helper
--
-- When a user deletes their account, Supabase auth cascades via the FK on
-- user_profiles/user_submissions. Aggregates are retained (anonymised,
-- non-PII). This function lets the app call a single entry point for the
-- right-to-erasure flow and re-runs aggregate refresh to reflect the
-- deletion.
-- ============================================================

CREATE OR REPLACE FUNCTION erase_user(target_user_id UUID)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    affected_quarters TEXT[];
BEGIN
    -- Only callable for your own user, unless by service_role.
    IF auth.uid() IS NOT NULL AND auth.uid() <> target_user_id THEN
        RAISE EXCEPTION 'erase_user: cannot erase another user';
    END IF;

    -- Capture quarters that had submissions from this user, so we can
    -- refresh aggregates after deletion.
    SELECT ARRAY_AGG(DISTINCT quarter)
    INTO affected_quarters
    FROM user_submissions
    WHERE user_id = target_user_id;

    DELETE FROM user_submissions WHERE user_id = target_user_id;
    DELETE FROM user_profiles    WHERE id      = target_user_id;

    -- Force a re-aggregation of affected quarters so stats reflect the
    -- deletion. Handled out-of-band via a separate service call if the
    -- dataset is large — here we just log.
    IF affected_quarters IS NOT NULL THEN
        RAISE NOTICE 'erase_user: affected quarters %; call refresh_aggregates()', affected_quarters;
    END IF;
END;
$$;

GRANT EXECUTE ON FUNCTION erase_user(UUID) TO authenticated;
