-- Migration 2026-06-12: RLS deny-all + Telegram update_id dedup column
--
-- 1. RLS: enable on all app tables with NO policies = deny-all for the
--    anon/authenticated PostgREST roles. The app uses the service-role key,
--    which bypasses RLS — app behavior is unchanged.
-- 2. Belt and braces: revoke all anon/authenticated privileges on existing
--    and future tables, sequences, and functions in public.
-- 3. user_profiles.last_update_id: highest processed Telegram update_id —
--    duplicate webhook deliveries (Telegram retries) are dropped by the handler.

ALTER TABLE user_profiles    ENABLE ROW LEVEL SECURITY;
ALTER TABLE canonical_plants ENABLE ROW LEVEL SECURITY;
ALTER TABLE meals            ENABLE ROW LEVEL SECURITY;
ALTER TABLE meal_items       ENABLE ROW LEVEL SECURITY;
ALTER TABLE weekly_check_ins ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_summaries  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated;

ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES    FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM anon, authenticated;

ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS last_update_id BIGINT;
