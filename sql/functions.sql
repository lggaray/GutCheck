-- Helper functions called from Lambda via Supabase REST RPC.

-- check_meal_logged_today: returns true if a main meal (not 'extra') of this
-- type was already logged today in Asia/Hong_Kong time.
CREATE OR REPLACE FUNCTION check_meal_logged_today(p_meal_type TEXT)
RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM logged_meals
        WHERE meal_type = p_meal_type
          AND meal_type IN ('breakfast', 'lunch', 'dinner')
          AND date(logged_at AT TIME ZONE 'Asia/Hong_Kong') =
              date(now() AT TIME ZONE 'Asia/Hong_Kong')
    );
$$;

-- get_meals_logged_today: returns JSON array of main meal types logged today.
-- 'extra' is excluded so it never affects cron smart-skip or classification context.
CREATE OR REPLACE FUNCTION get_meals_logged_today()
RETURNS JSONB LANGUAGE SQL STABLE AS $$
    SELECT COALESCE(jsonb_agg(DISTINCT meal_type), '[]'::JSONB)
    FROM logged_meals
    WHERE meal_type IN ('breakfast', 'lunch', 'dinner')
      AND date(logged_at AT TIME ZONE 'Asia/Hong_Kong') =
          date(now() AT TIME ZONE 'Asia/Hong_Kong');
$$;

-- get_daily_context: returns macros, targets, plant counts, and day-of-week
-- as a single JSON object for the reply formatter.
CREATE OR REPLACE FUNCTION get_daily_context()
RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_date   DATE;
    v_result JSONB;
BEGIN
    v_date := date(now() AT TIME ZONE 'Asia/Hong_Kong');

    SELECT jsonb_build_object(
        'day_calories',      COALESCE(ds.total_calories, 0),
        'day_protein_g',     COALESCE(ds.total_protein_g, 0),
        'day_carbs_g',       COALESCE(ds.total_carbs_g, 0),
        'day_fat_g',         COALESCE(ds.total_fat_g, 0),
        'day_fiber_g',       COALESCE(ds.total_fiber_g, 0),
        'day_unique_plants', COALESCE(ds.unique_plant_count, 0),
        'target_calories',   COALESCE(up.target_calories, 2000),
        'target_protein_g',  COALESCE(up.target_protein_g, 150),
        'weekly_plants',     get_weekly_plant_count(),
        'day_of_week',
            CASE WHEN EXTRACT(ISODOW FROM now() AT TIME ZONE 'Asia/Hong_Kong') = 7
                 THEN 7
                 ELSE EXTRACT(ISODOW FROM now() AT TIME ZONE 'Asia/Hong_Kong')::INT
            END
    )
    INTO v_result
    FROM user_profiles up
    LEFT JOIN daily_summaries ds ON ds.summary_date = v_date
    LIMIT 1;

    RETURN COALESCE(v_result, '{}'::JSONB);
END;
$$;

-- get_weekly_plant_count: distinct canonical plants logged this ISO week.
CREATE OR REPLACE FUNCTION get_weekly_plant_count()
RETURNS INT LANGUAGE SQL STABLE AS $$
    SELECT COUNT(DISTINCT mi.canonical_plant_id)::INT
    FROM meal_items mi
    JOIN meals m ON mi.meal_id = m.id
    WHERE date_trunc('week', m.logged_at AT TIME ZONE 'Asia/Hong_Kong') =
          date_trunc('week', now() AT TIME ZONE 'Asia/Hong_Kong')
      AND m.is_template_recipe = false
      AND mi.canonical_plant_id IS NOT NULL;
$$;

-- log_meal: atomic meal insert called from Lambda via a single RPC.
-- Handles plant resolution (pg_trgm) and meal_items in one transaction.
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

-- get_weekly_summary: weekly macro averages and targets for the current HKT ISO week.
-- Averages are over days_logged (days with at least one meal in daily_summaries).
-- Targets come from the singleton user_profiles row.
CREATE OR REPLACE FUNCTION get_weekly_summary()
RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_week_start DATE;
    v_week_end   DATE;
    v_result     JSONB;
BEGIN
    v_week_start := date_trunc('week', now() AT TIME ZONE 'Asia/Hong_Kong')::DATE;
    v_week_end   := v_week_start + interval '6 days';

    WITH week_agg AS (
        SELECT
            COUNT(*)                              AS days_logged,
            ROUND(AVG(total_calories))::INT       AS avg_calories,
            ROUND(AVG(total_protein_g))::INT      AS avg_protein_g,
            ROUND(AVG(total_carbs_g))::INT        AS avg_carbs_g,
            ROUND(AVG(total_fat_g))::INT          AS avg_fat_g,
            ROUND(AVG(total_fiber_g))::INT        AS avg_fiber_g
        FROM daily_summaries
        WHERE summary_date BETWEEN v_week_start AND v_week_end
    )
    SELECT jsonb_build_object(
        'days_logged',      wa.days_logged,
        'weekly_plants',    get_weekly_plant_count(),
        'avg_calories',     COALESCE(wa.avg_calories, 0),
        'target_calories',  COALESCE(up.target_calories, 2384),
        'avg_protein_g',    COALESCE(wa.avg_protein_g, 0),
        'target_protein_g', COALESCE(up.target_protein_g::INT, 140),
        'avg_carbs_g',      COALESCE(wa.avg_carbs_g, 0),
        'target_carbs_g',   COALESCE(up.target_carbs_g::INT, 285),
        'avg_fat_g',        COALESCE(wa.avg_fat_g, 0),
        'target_fat_g',     COALESCE(up.target_fat_g::INT, 65),
        'avg_fiber_g',      COALESCE(wa.avg_fiber_g, 0),
        'target_fiber_g',   COALESCE(up.target_fiber_g::INT, 30)
    )
    INTO v_result
    FROM week_agg wa, user_profiles up
    LIMIT 1;

    RETURN COALESCE(v_result, '{}'::JSONB);
END;
$$;

-- use_recipe: fuzzy-match a template by name, copy it as a real meal, return macros.
-- Returns {"matched_name": null} if no template matches (similarity > 0.3).
-- p_meal_type overrides the template's meal_type (pass NULL to use template default).
CREATE OR REPLACE FUNCTION use_recipe(p_name TEXT, p_user_string TEXT, p_meal_type TEXT DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    v_template    meals%ROWTYPE;
    v_new_meal_id UUID;
    v_plants      TEXT[];
BEGIN
    SELECT * INTO v_template
    FROM meals
    WHERE is_template_recipe = true
      AND similarity(raw_user_string, p_name) > 0.3
    ORDER BY similarity(raw_user_string, p_name) DESC
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('matched_name', NULL);
    END IF;

    INSERT INTO meals (
        id, logged_at, meal_type, raw_user_string, is_template_recipe,
        total_calories, total_protein_g, total_carbs_g, total_fat_g, total_fiber_g
    ) VALUES (
        gen_random_uuid(),
        now(),
        COALESCE(p_meal_type, v_template.meal_type),
        p_user_string,
        false,
        v_template.total_calories,
        v_template.total_protein_g,
        v_template.total_carbs_g,
        v_template.total_fat_g,
        v_template.total_fiber_g
    ) RETURNING id INTO v_new_meal_id;

    INSERT INTO meal_items (
        meal_id, food_name, raw_description, quantity, unit, fraction_eaten,
        calories, protein_g, carbs_g, fat_g, fiber_g,
        is_plant, plant_name, plant_category, canonical_plant_id
    )
    SELECT
        v_new_meal_id, mi.food_name, mi.raw_description, mi.quantity, mi.unit, mi.fraction_eaten,
        mi.calories, mi.protein_g, mi.carbs_g, mi.fat_g, mi.fiber_g,
        mi.is_plant, mi.plant_name, mi.plant_category,
        COALESCE(
            mi.canonical_plant_id,
            CASE WHEN mi.is_plant AND mi.plant_name IS NOT NULL THEN
                (SELECT id FROM canonical_plants cp
                 WHERE similarity(cp.name, lower(mi.plant_name)) > 0.3
                 ORDER BY similarity(cp.name, lower(mi.plant_name)) DESC
                 LIMIT 1)
            END
        )
    FROM meal_items mi
    WHERE mi.meal_id = v_template.id;

    SELECT COALESCE(array_agg(plant_name ORDER BY plant_name), '{}')
    INTO v_plants
    FROM meal_items
    WHERE meal_id = v_new_meal_id
      AND is_plant = true
      AND plant_name IS NOT NULL;

    RETURN jsonb_build_object(
        'matched_name',    v_template.raw_user_string,
        'meal_id',         v_new_meal_id,
        'total_calories',  v_template.total_calories,
        'total_protein_g', v_template.total_protein_g,
        'total_carbs_g',   v_template.total_carbs_g,
        'total_fat_g',     v_template.total_fat_g,
        'total_fiber_g',   v_template.total_fiber_g,
        'plants',          to_jsonb(v_plants)
    );
END;
$$;

-- list_recipes: return all template meals ordered by meal_type then name.
CREATE OR REPLACE FUNCTION list_recipes()
RETURNS JSONB LANGUAGE SQL STABLE AS $$
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'name',      raw_user_string,
                'meal_type', meal_type,
                'calories',  total_calories,
                'protein_g', total_protein_g,
                'carbs_g',   total_carbs_g,
                'fat_g',     total_fat_g,
                'fiber_g',   total_fiber_g
            )
            ORDER BY meal_type, raw_user_string
        ),
        '[]'::JSONB
    )
    FROM meals
    WHERE is_template_recipe = true;
$$;

-- get_gap_nudge: check if fiber and/or protein have been low for the last 3 logged days.
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

-- undo_last_meal: deletes the most recently logged real meal and returns its
-- details. meal_items cascade via FK; the update_daily_summary trigger
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

-- get_week_plants: distinct canonical plants logged this ISO week (HKT),
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

-- record_weekly_weight: upsert this ISO week's (HKT) weight check-in.
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
