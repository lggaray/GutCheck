-- Migration 2026-06-11: gap-nudge today-exclusion fix, undo/week-plants/weight
-- functions, auto_added plant flag, awaiting_weight flag, sanity constraints.
--
-- Idempotent: safe to run twice. Apply in one pass against the live DB.
-- Note: constraint ADDs validate existing rows and will error if legacy data
-- violates them — handled at apply time.

-- ═══════════════════════════════════════════════════════════════════════════
-- 1. Columns
-- ═══════════════════════════════════════════════════════════════════════════

-- canonical_plants.auto_added: marks plants inserted on the fly by log_meal()
-- (unknown plant names) vs. curated seed entries.
ALTER TABLE canonical_plants
    ADD COLUMN IF NOT EXISTS auto_added BOOLEAN NOT NULL DEFAULT false;

-- user_profiles.awaiting_weight: bot asked for a weekly weigh-in; the next
-- bare number should be treated as weight (Phase 4).
ALTER TABLE user_profiles
    ADD COLUMN IF NOT EXISTS awaiting_weight BOOLEAN NOT NULL DEFAULT false;

-- ═══════════════════════════════════════════════════════════════════════════
-- 2. Constraints (DROP IF EXISTS + ADD, matching schema.sql migration blocks)
-- ═══════════════════════════════════════════════════════════════════════════

-- user_profiles: enforce the singleton row (id = 1 only).
ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_singleton;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_singleton CHECK (id = 1);

-- user_profiles: sanity bounds on targets and biometrics. NULLs pass.
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

-- meals: totals can never go negative. NULLs pass.
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

-- meal_items: macros/quantities can never go negative, fraction_eaten in (0, 1].
-- NULLs pass.
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

-- ═══════════════════════════════════════════════════════════════════════════
-- 3. Functions
-- ═══════════════════════════════════════════════════════════════════════════

-- get_gap_nudge: FIX — exclude today's (HKT) partial-day summary row from the
-- 3-day streak window in all three subqueries.
CREATE OR REPLACE FUNCTION get_gap_nudge()
RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
  v_fiber_target     NUMERIC;
  v_protein_target   NUMERIC;
  v_last_sent        TIMESTAMPTZ;
  v_enabled          BOOLEAN;
  v_low_fiber_days   INTEGER;
  v_low_protein_days INTEGER;
  v_fiber_gap        BOOLEAN := false;
  v_protein_gap      BOOLEAN := false;
BEGIN
  SELECT target_fiber_g, target_protein_g, last_gap_nudge_sent_at, gap_nudge_enabled
  INTO v_fiber_target, v_protein_target, v_last_sent, v_enabled
  FROM user_profiles LIMIT 1;

  -- Kill switch
  IF NOT v_enabled THEN
    RETURN NULL;
  END IF;

  -- Guard: targets must be set (onboarding complete)
  IF v_fiber_target IS NULL OR v_protein_target IS NULL THEN
    RETURN NULL;
  END IF;

  -- ISO week cooldown (HKT)
  IF v_last_sent IS NOT NULL AND
     date_trunc('week', v_last_sent AT TIME ZONE 'Asia/Hong_Kong') =
     date_trunc('week', NOW() AT TIME ZONE 'Asia/Hong_Kong') THEN
    RETURN NULL;
  END IF;

  -- Need at least 3 logged days (today's partial-day summary excluded)
  IF (SELECT COUNT(*) FROM (
        SELECT summary_date FROM daily_summaries
        WHERE summary_date < date(now() AT TIME ZONE 'Asia/Hong_Kong')
        ORDER BY summary_date DESC LIMIT 3
      ) sub) < 3 THEN
    RETURN NULL;
  END IF;

  -- Fiber: all 3 most recent fully-logged days below 80% of target
  SELECT COUNT(*) INTO v_low_fiber_days
  FROM (SELECT total_fiber_g FROM daily_summaries
        WHERE summary_date < date(now() AT TIME ZONE 'Asia/Hong_Kong')
        ORDER BY summary_date DESC LIMIT 3) sub
  WHERE total_fiber_g < v_fiber_target * 0.8;

  IF v_low_fiber_days = 3 THEN v_fiber_gap := true; END IF;

  -- Protein: all 3 most recent fully-logged days below 80% of target
  SELECT COUNT(*) INTO v_low_protein_days
  FROM (SELECT total_protein_g FROM daily_summaries
        WHERE summary_date < date(now() AT TIME ZONE 'Asia/Hong_Kong')
        ORDER BY summary_date DESC LIMIT 3) sub
  WHERE total_protein_g < v_protein_target * 0.8;

  IF v_low_protein_days = 3 THEN v_protein_gap := true; END IF;

  IF NOT v_fiber_gap AND NOT v_protein_gap THEN
    RETURN NULL;
  END IF;

  RETURN jsonb_build_object('fiber_gap', v_fiber_gap, 'protein_gap', v_protein_gap);
END;
$$;

-- log_meal: CHANGE — auto-inserted unknown plants are now flagged auto_added = true.
CREATE OR REPLACE FUNCTION log_meal(p_data JSONB)
RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE
    v_meal_id   UUID;
    v_item      JSONB;
    v_plant_id  INT;
    v_pname     TEXT;
BEGIN
    INSERT INTO meals (
        id, logged_at, meal_type, raw_user_string,
        total_calories, total_protein_g, total_carbs_g, total_fat_g, total_fiber_g
    ) VALUES (
        gen_random_uuid(),
        now(),
        p_data->>'meal_type',
        p_data->>'raw_user_string',
        (p_data->>'total_calories')::INT,
        (p_data->>'total_protein_g')::NUMERIC,
        (p_data->>'total_carbs_g')::NUMERIC,
        (p_data->>'total_fat_g')::NUMERIC,
        (p_data->>'total_fiber_g')::NUMERIC
    ) RETURNING id INTO v_meal_id;

    FOR v_item IN SELECT * FROM jsonb_array_elements(p_data->'items') LOOP
        v_plant_id := NULL;
        v_pname    := v_item->>'plant_name';

        IF (v_item->>'is_plant')::BOOLEAN AND v_pname IS NOT NULL THEN
            SELECT id INTO v_plant_id
            FROM canonical_plants
            WHERE similarity(name, v_pname) > 0.3
            ORDER BY similarity(name, v_pname) DESC
            LIMIT 1;

            IF v_plant_id IS NULL THEN
                INSERT INTO canonical_plants (name, category, auto_added)
                VALUES (lower(v_pname), COALESCE(v_item->>'plant_category', 'vegetable'), true)
                ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                RETURNING id INTO v_plant_id;
            END IF;
        END IF;

        INSERT INTO meal_items (
            meal_id, food_name, raw_description, quantity, unit, fraction_eaten,
            calories, protein_g, carbs_g, fat_g, fiber_g,
            is_plant, plant_name, plant_category, canonical_plant_id
        ) VALUES (
            v_meal_id,
            v_item->>'food_name',
            v_item->>'raw_description',
            (v_item->>'quantity')::NUMERIC,
            v_item->>'unit',
            COALESCE((v_item->>'fraction_eaten')::NUMERIC, 1.0),
            (v_item->'macros'->>'calories')::INT,
            (v_item->'macros'->>'protein_g')::NUMERIC,
            (v_item->'macros'->>'carbs_g')::NUMERIC,
            (v_item->'macros'->>'fat_g')::NUMERIC,
            (v_item->'macros'->>'fiber_g')::NUMERIC,
            (v_item->>'is_plant')::BOOLEAN,
            v_pname,
            v_item->>'plant_category',
            v_plant_id
        );
    END LOOP;

    RETURN v_meal_id;
END;
$$;

-- undo_last_meal: NEW — deletes the most recently logged real meal and returns
-- its details. meal_items cascade via FK; the update_daily_summary trigger
-- recomputes daily_summaries automatically. Returns {"deleted": false} if no
-- logged meals exist.
CREATE OR REPLACE FUNCTION undo_last_meal()
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    v_meal meals%ROWTYPE;
BEGIN
    SELECT * INTO v_meal
    FROM meals
    WHERE is_template_recipe = false
      AND logged_at IS NOT NULL
    ORDER BY logged_at DESC
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('deleted', false);
    END IF;

    DELETE FROM meals WHERE id = v_meal.id;

    RETURN jsonb_build_object(
        'deleted',         true,
        'raw_user_string', v_meal.raw_user_string,
        'meal_type',       v_meal.meal_type,
        'total_calories',  v_meal.total_calories,
        'total_protein_g', v_meal.total_protein_g,
        'total_carbs_g',   v_meal.total_carbs_g,
        'total_fat_g',     v_meal.total_fat_g,
        'total_fiber_g',   v_meal.total_fiber_g
    );
END;
$$;

-- get_week_plants: NEW — distinct canonical plants logged this ISO week (HKT),
-- ordered by category then name. Same week logic as get_weekly_plant_count.
CREATE OR REPLACE FUNCTION get_week_plants()
RETURNS JSONB LANGUAGE SQL STABLE AS $$
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'name',       wp.name,
                'category',   wp.category,
                'auto_added', wp.auto_added
            )
            ORDER BY wp.category, wp.name
        ),
        '[]'::JSONB
    )
    FROM (
        SELECT DISTINCT cp.name, cp.category, cp.auto_added
        FROM meal_items mi
        JOIN meals m ON mi.meal_id = m.id
        JOIN canonical_plants cp ON cp.id = mi.canonical_plant_id
        WHERE date_trunc('week', m.logged_at AT TIME ZONE 'Asia/Hong_Kong') =
              date_trunc('week', now() AT TIME ZONE 'Asia/Hong_Kong')
          AND m.is_template_recipe = false
          AND mi.canonical_plant_id IS NOT NULL
    ) wp;
$$;

-- record_weekly_weight: NEW — upsert this ISO week's (HKT) weight check-in.
CREATE OR REPLACE FUNCTION record_weekly_weight(p_weight_kg NUMERIC)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    v_week_start DATE;
BEGIN
    v_week_start := date_trunc('week', now() AT TIME ZONE 'Asia/Hong_Kong')::DATE;

    INSERT INTO weekly_check_ins (week_start, weight_kg)
    VALUES (v_week_start, p_weight_kg)
    ON CONFLICT (week_start) DO UPDATE SET
        weight_kg     = EXCLUDED.weight_kg,
        checked_in_at = now();

    RETURN jsonb_build_object(
        'week_start', v_week_start,
        'weight_kg',  p_weight_kg
    );
END;
$$;
