-- Enable fuzzy matching for plant name resolution (Phase 3)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── user_profiles ──────────────────────────────────────────────────────────
-- Single-row table. Calorie/macro targets derived from biometrics + TDEE.
CREATE TABLE IF NOT EXISTS user_profiles (
    id                  SERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    age                 INT,
    height_cm           NUMERIC(5,1),
    weight_kg           NUMERIC(5,2),
    body_fat_pct        NUMERIC(5,2),
    activity_level      TEXT CHECK (activity_level IN ('sedentary', 'light', 'moderate', 'active')),
    goal_type           TEXT CHECK (goal_type IN ('maintain', 'lose', 'gain')),
    tdee                INT,
    telegram_chat_id    BIGINT UNIQUE,
    onboarding_step     INT NOT NULL DEFAULT 0,
    target_calories     INT,
    target_protein_g    NUMERIC(6,1),
    target_carbs_g      NUMERIC(6,1),
    target_fat_g        NUMERIC(6,1),
    target_fiber_g      NUMERIC(6,1),
    last_gap_nudge_sent_at TIMESTAMPTZ,
    gap_nudge_enabled   BOOLEAN NOT NULL DEFAULT true
);

-- Seed singleton profile row if absent.
INSERT INTO user_profiles (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Idempotent column migration: awaiting_weight flags that the bot asked for a
-- weekly weigh-in and the next bare number should be treated as weight (Phase 4).
ALTER TABLE user_profiles
    ADD COLUMN IF NOT EXISTS awaiting_weight BOOLEAN NOT NULL DEFAULT false;

-- Idempotent constraint migration: enforce the singleton row (id = 1 only).
ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_singleton;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_singleton CHECK (id = 1);

-- Idempotent constraint migration: sanity bounds on targets and biometrics.
-- NULLs pass (targets unset until onboarding completes).
ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_target_calories_check;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_target_calories_check CHECK (target_calories >= 0);
ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_target_protein_g_check;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_target_protein_g_check CHECK (target_protein_g >= 0);
ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_target_carbs_g_check;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_target_carbs_g_check CHECK (target_carbs_g >= 0);
ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_target_fat_g_check;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_target_fat_g_check CHECK (target_fat_g >= 0);
ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_target_fiber_g_check;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_target_fiber_g_check CHECK (target_fiber_g >= 0);
ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_weight_kg_check;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_weight_kg_check CHECK (weight_kg BETWEEN 30 AND 300);
ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_body_fat_pct_check;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_body_fat_pct_check CHECK (body_fat_pct BETWEEN 3 AND 60);

-- ── canonical_plants ───────────────────────────────────────────────────────
-- Reference dictionary. pg_trgm index enables fuzzy name resolution.
CREATE TABLE IF NOT EXISTS canonical_plants (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    category    TEXT NOT NULL CHECK (category IN (
                    'leaf', 'legume', 'nut', 'seed', 'whole_grain',
                    'fruit', 'vegetable', 'tuber', 'herb', 'spice', 'other'))
);

CREATE INDEX IF NOT EXISTS canonical_plants_name_trgm
    ON canonical_plants USING gin (name gin_trgm_ops);

-- Idempotent column migration: auto_added marks plants inserted on the fly by
-- log_meal() (unknown plant names) vs. curated seed entries.
ALTER TABLE canonical_plants
    ADD COLUMN IF NOT EXISTS auto_added BOOLEAN NOT NULL DEFAULT false;

-- ── meals ──────────────────────────────────────────────────────────────────
-- Each row is one eating event OR a saved recipe template.
-- Recipes: is_template_recipe = true, logged_at = NULL.
CREATE TABLE IF NOT EXISTS meals (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    logged_at           TIMESTAMPTZ,                      -- NULL for templates
    meal_type           TEXT NOT NULL CHECK (meal_type IN
                            ('breakfast', 'lunch', 'dinner', 'extra')),
    raw_user_string     TEXT NOT NULL,
    is_template_recipe  BOOLEAN NOT NULL DEFAULT false,
    total_calories      INT,
    total_protein_g     NUMERIC(7,2),
    total_carbs_g       NUMERIC(7,2),
    total_fat_g         NUMERIC(7,2),
    total_fiber_g       NUMERIC(7,2)
);

-- Idempotent constraint migration: ensures 'extra' is included even when the
-- table already existed with the old four-value constraint.
ALTER TABLE meals
    DROP CONSTRAINT IF EXISTS meals_meal_type_check;
ALTER TABLE meals
    ADD CONSTRAINT meals_meal_type_check
        CHECK (meal_type IN ('breakfast', 'lunch', 'dinner', 'extra'));

-- Idempotent constraint migration: totals can never go negative. NULLs pass.
ALTER TABLE meals
    DROP CONSTRAINT IF EXISTS meals_total_calories_check;
ALTER TABLE meals
    ADD CONSTRAINT meals_total_calories_check CHECK (total_calories >= 0);
ALTER TABLE meals
    DROP CONSTRAINT IF EXISTS meals_total_protein_g_check;
ALTER TABLE meals
    ADD CONSTRAINT meals_total_protein_g_check CHECK (total_protein_g >= 0);
ALTER TABLE meals
    DROP CONSTRAINT IF EXISTS meals_total_carbs_g_check;
ALTER TABLE meals
    ADD CONSTRAINT meals_total_carbs_g_check CHECK (total_carbs_g >= 0);
ALTER TABLE meals
    DROP CONSTRAINT IF EXISTS meals_total_fat_g_check;
ALTER TABLE meals
    ADD CONSTRAINT meals_total_fat_g_check CHECK (total_fat_g >= 0);
ALTER TABLE meals
    DROP CONSTRAINT IF EXISTS meals_total_fiber_g_check;
ALTER TABLE meals
    ADD CONSTRAINT meals_total_fiber_g_check CHECK (total_fiber_g >= 0);

-- ── meal_items ─────────────────────────────────────────────────────────────
-- Individual ingredients within a meal.
-- canonical_plant_id resolved in Phase 3 via pg_trgm; NULL until then.
CREATE TABLE IF NOT EXISTS meal_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meal_id             UUID NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
    food_name           TEXT NOT NULL,
    raw_description     TEXT,
    quantity            NUMERIC(8,2),
    unit                TEXT,
    fraction_eaten      NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    calories            INT,
    protein_g           NUMERIC(7,2),
    carbs_g             NUMERIC(7,2),
    fat_g               NUMERIC(7,2),
    fiber_g             NUMERIC(7,2),
    is_plant            BOOLEAN NOT NULL DEFAULT false,
    plant_name          TEXT,
    plant_category      TEXT CHECK (plant_category IN (
                            'leaf', 'legume', 'nut', 'seed', 'whole_grain',
                            'fruit', 'vegetable', 'tuber', 'herb', 'spice', 'other')),
    canonical_plant_id  INT REFERENCES canonical_plants(id)
);

-- Idempotent constraint migration: macros/quantities can never go negative,
-- fraction_eaten must be in (0, 1]. NULLs pass.
ALTER TABLE meal_items
    DROP CONSTRAINT IF EXISTS meal_items_calories_check;
ALTER TABLE meal_items
    ADD CONSTRAINT meal_items_calories_check CHECK (calories >= 0);
ALTER TABLE meal_items
    DROP CONSTRAINT IF EXISTS meal_items_protein_g_check;
ALTER TABLE meal_items
    ADD CONSTRAINT meal_items_protein_g_check CHECK (protein_g >= 0);
ALTER TABLE meal_items
    DROP CONSTRAINT IF EXISTS meal_items_carbs_g_check;
ALTER TABLE meal_items
    ADD CONSTRAINT meal_items_carbs_g_check CHECK (carbs_g >= 0);
ALTER TABLE meal_items
    DROP CONSTRAINT IF EXISTS meal_items_fat_g_check;
ALTER TABLE meal_items
    ADD CONSTRAINT meal_items_fat_g_check CHECK (fat_g >= 0);
ALTER TABLE meal_items
    DROP CONSTRAINT IF EXISTS meal_items_fiber_g_check;
ALTER TABLE meal_items
    ADD CONSTRAINT meal_items_fiber_g_check CHECK (fiber_g >= 0);
ALTER TABLE meal_items
    DROP CONSTRAINT IF EXISTS meal_items_quantity_check;
ALTER TABLE meal_items
    ADD CONSTRAINT meal_items_quantity_check CHECK (quantity >= 0);
ALTER TABLE meal_items
    DROP CONSTRAINT IF EXISTS meal_items_fraction_eaten_check;
ALTER TABLE meal_items
    ADD CONSTRAINT meal_items_fraction_eaten_check
        CHECK (fraction_eaten > 0 AND fraction_eaten <= 1);

-- ── weekly_check_ins ───────────────────────────────────────────────────────
-- Weekly weight log. Drives adaptive TDEE coaching (Phase 4).
CREATE TABLE IF NOT EXISTS weekly_check_ins (
    id              SERIAL PRIMARY KEY,
    checked_in_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    week_start      DATE NOT NULL UNIQUE,
    weight_kg       NUMERIC(5,2) NOT NULL,
    notes           TEXT
);

-- ── daily_summaries ────────────────────────────────────────────────────────
-- Precomputed cache. NEVER written by application code.
-- Maintained exclusively by the update_daily_summary trigger.
CREATE TABLE IF NOT EXISTS daily_summaries (
    summary_date        DATE PRIMARY KEY,
    total_calories      INT NOT NULL DEFAULT 0,
    total_protein_g     NUMERIC(7,2) NOT NULL DEFAULT 0,
    total_carbs_g       NUMERIC(7,2) NOT NULL DEFAULT 0,
    total_fat_g         NUMERIC(7,2) NOT NULL DEFAULT 0,
    total_fiber_g       NUMERIC(7,2) NOT NULL DEFAULT 0,
    unique_plant_count  INT NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
