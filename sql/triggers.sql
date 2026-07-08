-- update_daily_summary: recomputes daily_summaries for the affected date.
-- Fires AFTER INSERT/UPDATE/DELETE on meals and meal_items.
-- Application code never writes to daily_summaries directly.

CREATE OR REPLACE FUNCTION update_daily_summary()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_date DATE;
    v_meal_id UUID;
BEGIN
    -- Resolve the meal_id and date from whichever table fired the trigger.
    IF TG_TABLE_NAME = 'meals' THEN
        v_meal_id := COALESCE(NEW.id, OLD.id);
        v_date    := COALESCE(
                        date(NEW.logged_at AT TIME ZONE 'Asia/Hong_Kong'),
                        date(OLD.logged_at AT TIME ZONE 'Asia/Hong_Kong')
                     );
    ELSE
        -- meal_items: look up the parent meal's date.
        v_meal_id := COALESCE(NEW.meal_id, OLD.meal_id);
        SELECT date(logged_at AT TIME ZONE 'Asia/Hong_Kong') INTO v_date
        FROM meals
        WHERE id = v_meal_id;
    END IF;

    -- Skip template recipes and rows with no date.
    IF v_date IS NULL THEN
        RETURN NULL;
    END IF;

    -- Recompute and upsert the summary for that date.
    INSERT INTO daily_summaries (
        summary_date,
        total_calories,
        total_protein_g,
        total_carbs_g,
        total_fat_g,
        total_fiber_g,
        unique_plant_count,
        updated_at
    )
    SELECT
        v_date,
        COALESCE(SUM(m.total_calories), 0),
        COALESCE(SUM(m.total_protein_g), 0),
        COALESCE(SUM(m.total_carbs_g), 0),
        COALESCE(SUM(m.total_fat_g), 0),
        COALESCE(SUM(m.total_fiber_g), 0),
        -- Distinct plants across all meals on this date.
        (SELECT COUNT(DISTINCT mi.canonical_plant_id)
         FROM meal_items mi
         JOIN meals m2 ON mi.meal_id = m2.id
         WHERE date(m2.logged_at AT TIME ZONE 'Asia/Hong_Kong') = v_date
           AND m2.is_template_recipe = false
           AND mi.canonical_plant_id IS NOT NULL),
        now()
    FROM logged_meals m
    WHERE date(m.logged_at AT TIME ZONE 'Asia/Hong_Kong') = v_date
    ON CONFLICT (summary_date) DO UPDATE SET
        total_calories     = EXCLUDED.total_calories,
        total_protein_g    = EXCLUDED.total_protein_g,
        total_carbs_g      = EXCLUDED.total_carbs_g,
        total_fat_g        = EXCLUDED.total_fat_g,
        total_fiber_g      = EXCLUDED.total_fiber_g,
        unique_plant_count = EXCLUDED.unique_plant_count,
        updated_at         = EXCLUDED.updated_at;

    RETURN NULL;
END;
$$;

-- Attach to meals
DROP TRIGGER IF EXISTS trg_update_daily_summary_meals ON meals;
CREATE TRIGGER trg_update_daily_summary_meals
    AFTER INSERT OR UPDATE OR DELETE ON meals
    FOR EACH ROW EXECUTE FUNCTION update_daily_summary();

-- Attach to meal_items
DROP TRIGGER IF EXISTS trg_update_daily_summary_meal_items ON meal_items;
CREATE TRIGGER trg_update_daily_summary_meal_items
    AFTER INSERT OR UPDATE OR DELETE ON meal_items
    FOR EACH ROW EXECUTE FUNCTION update_daily_summary();
