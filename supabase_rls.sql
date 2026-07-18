-- Supabase Row-Level Security (RLS) policies for multi-tenant data isolation.
-- Run this in the Supabase Dashboard → SQL Editor after adding user_id columns.
--
-- IMPORTANT: The application already enforces tenant isolation at the query level
-- (via derive_business_key prefixing). RLS is defense-in-depth — it protects
-- against accidental direct DB access / admin mistakes.
--
-- After running this script, no user can read or write rows that belong to a
-- different user — even via the Supabase client SDK or REST API.


-- ── Enable RLS on all user-scoped tables ────────────────────────────────────
-- Memory tables use business_key prefix for scoping (no user_id column needed)
-- but we still enable RLS so the anon key can't read them at all.

-- Tables where data is scoped by user_id column:
ALTER TABLE activity_log           ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_snapshot        ENABLE ROW LEVEL SECURITY;
ALTER TABLE smart_analysis_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE leads                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE analyses               ENABLE ROW LEVEL SECURITY;
ALTER TABLE reports                ENABLE ROW LEVEL SECURITY;

-- Memory tables (business_key scoped — RLS blocks anon access entirely):
ALTER TABLE business_memory        ENABLE ROW LEVEL SECURITY;
ALTER TABLE market_memory          ENABLE ROW LEVEL SECURITY;
ALTER TABLE competitor_memory      ENABLE ROW LEVEL SECURITY;
ALTER TABLE audience_memory        ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaign_memory        ENABLE ROW LEVEL SECURITY;
ALTER TABLE opportunity_memory     ENABLE ROW LEVEL SECURITY;
ALTER TABLE offer_memory           ENABLE ROW LEVEL SECURITY;
ALTER TABLE website_memory         ENABLE ROW LEVEL SECURITY;
ALTER TABLE visibility_memory      ENABLE ROW LEVEL SECURITY;
ALTER TABLE outreach_memory        ENABLE ROW LEVEL SECURITY;
ALTER TABLE kpi_memory             ENABLE ROW LEVEL SECURITY;
ALTER TABLE performance_memory     ENABLE ROW LEVEL SECURITY;
ALTER TABLE optimizer_memory       ENABLE ROW LEVEL SECURITY;
ALTER TABLE result_memory          ENABLE ROW LEVEL SECURITY;
ALTER TABLE prospect_memory        ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomous_plan_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE social_intel_memory    ENABLE ROW LEVEL SECURITY;
ALTER TABLE creative_director_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE ad_creative_memory     ENABLE ROW LEVEL SECURITY;
ALTER TABLE creative_studio_memory ENABLE ROW LEVEL SECURITY;

-- growth_memory is intentionally NOT restricted — shared cross-tenant learning.


-- ── Policies for tables with user_id column ──────────────────────────────────

-- activity_log
CREATE POLICY "Users see own activity"
  ON activity_log FOR SELECT
  USING (auth.uid()::text = user_id OR user_id IS NULL);

CREATE POLICY "Users insert own activity"
  ON activity_log FOR INSERT
  WITH CHECK (auth.uid()::text = user_id);

CREATE POLICY "Users update own activity"
  ON activity_log FOR UPDATE
  USING (auth.uid()::text = user_id);

-- report_snapshot
CREATE POLICY "Users see own snapshots"
  ON report_snapshot FOR SELECT
  USING (auth.uid()::text = user_id OR user_id IS NULL);

CREATE POLICY "Users insert own snapshots"
  ON report_snapshot FOR INSERT
  WITH CHECK (auth.uid()::text = user_id);

CREATE POLICY "Users update own snapshots"
  ON report_snapshot FOR UPDATE
  USING (auth.uid()::text = user_id);

-- smart_analysis_history
CREATE POLICY "Users see own analysis history"
  ON smart_analysis_history FOR SELECT
  USING (auth.uid()::text = user_id OR user_id IS NULL);

CREATE POLICY "Users insert own analysis history"
  ON smart_analysis_history FOR INSERT
  WITH CHECK (auth.uid()::text = user_id);

-- leads
CREATE POLICY "Users see own leads"
  ON leads FOR SELECT
  USING (auth.uid()::text = user_id OR user_id IS NULL);

CREATE POLICY "Users insert own leads"
  ON leads FOR INSERT
  WITH CHECK (auth.uid()::text = user_id);

CREATE POLICY "Users update own leads"
  ON leads FOR UPDATE
  USING (auth.uid()::text = user_id);

CREATE POLICY "Users delete own leads"
  ON leads FOR DELETE
  USING (auth.uid()::text = user_id);

-- analyses
CREATE POLICY "Users see own analyses"
  ON analyses FOR SELECT
  USING (auth.uid()::text = user_id OR user_id IS NULL);

CREATE POLICY "Users insert own analyses"
  ON analyses FOR INSERT
  WITH CHECK (auth.uid()::text = user_id);

-- reports
CREATE POLICY "Users see own reports"
  ON reports FOR SELECT
  USING (auth.uid()::text = user_id OR user_id IS NULL);

CREATE POLICY "Users insert own reports"
  ON reports FOR INSERT
  WITH CHECK (auth.uid()::text = user_id);


-- ── Policies for memory tables (scoped by business_key prefix) ───────────────
-- The FastAPI backend uses SERVICE_ROLE_KEY to bypass RLS for these tables.
-- For the anon key (frontend), block all access — only the backend should touch these.

-- Helper: block anon access entirely (the backend uses service_role which bypasses RLS)
CREATE POLICY "Backend only — block anon reads"
  ON business_memory FOR ALL
  USING (false);

-- Repeat for each memory table:
CREATE POLICY "Backend only" ON market_memory         FOR ALL USING (false);
CREATE POLICY "Backend only" ON competitor_memory     FOR ALL USING (false);
CREATE POLICY "Backend only" ON audience_memory       FOR ALL USING (false);
CREATE POLICY "Backend only" ON campaign_memory       FOR ALL USING (false);
CREATE POLICY "Backend only" ON opportunity_memory    FOR ALL USING (false);
CREATE POLICY "Backend only" ON offer_memory          FOR ALL USING (false);
CREATE POLICY "Backend only" ON website_memory        FOR ALL USING (false);
CREATE POLICY "Backend only" ON visibility_memory     FOR ALL USING (false);
CREATE POLICY "Backend only" ON outreach_memory       FOR ALL USING (false);
CREATE POLICY "Backend only" ON kpi_memory            FOR ALL USING (false);
CREATE POLICY "Backend only" ON performance_memory    FOR ALL USING (false);
CREATE POLICY "Backend only" ON optimizer_memory      FOR ALL USING (false);
CREATE POLICY "Backend only" ON result_memory         FOR ALL USING (false);
CREATE POLICY "Backend only" ON prospect_memory       FOR ALL USING (false);
CREATE POLICY "Backend only" ON autonomous_plan_memory FOR ALL USING (false);
CREATE POLICY "Backend only" ON social_intel_memory   FOR ALL USING (false);
CREATE POLICY "Backend only" ON creative_director_memory FOR ALL USING (false);
CREATE POLICY "Backend only" ON ad_creative_memory    FOR ALL USING (false);
CREATE POLICY "Backend only" ON creative_studio_memory FOR ALL USING (false);


-- ── Checklist before running ──────────────────────────────────────────────────
-- 1. Confirm KRISH_USER_ID migration ran successfully (check Render logs)
-- 2. Run this script in Supabase Dashboard → SQL Editor
-- 3. Test: sign in as a non-Krish account, confirm no data visible
-- 4. Set SUPABASE_JWT_SECRET on Render (get from Supabase → Settings → API → JWT Secret)
-- 5. Frontend: set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY on Netlify
