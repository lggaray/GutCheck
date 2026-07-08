-- Pattern insight functions (journal-first reframe, Phase C).
-- Both are read-only helpers called from Lambda via Supabase REST RPC.

-- get_today_first_time_plants: canonical plants whose FIRST-EVER logged
-- occurrence (HKT date) is today. Used for "✨ first time" reply flair.
-- Called right after log_meal(), so the just-inserted meal is included.
CREATE OR REPLACE FUNCTION get_today_first_time_plants()
RETURNS JSONB LANGUAGE SQL STABLE AS $$
    SELECT COALESCE(jsonb_agg(s.name ORDER BY s.name), '[]'::JSONB)
    FROM (
        SELECT cp.name
        FROM meal_items mi
        JOIN logged_meals m     ON m.id = mi.meal_id
        JOIN canonical_plants cp ON cp.id = mi.canonical_plant_id
        GROUP BY cp.name
        HAVING MIN(date(m.logged_at AT TIME ZONE 'Asia/Hong_Kong')) =
               date(now() AT TIME ZONE 'Asia/Hong_Kong')
    ) s;
$$;

-- get_week_patterns: weekly pattern report for the Sunday check-in.
--   new_plants  — plants first logged (ever) during the current HKT ISO week
--   top_meals   — most repeated raw_user_strings, last 28 days, 2+ occurrences
--   streak_days — consecutive days with a daily_summaries row, anchored at
--                 today (or yesterday if today has no meals yet)
CREATE OR REPLACE FUNCTION get_week_patterns()
RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_today      DATE := date(now() AT TIME ZONE 'Asia/Hong_Kong');
    v_new_plants JSONB;
    v_top_meals  JSONB;
    v_streak     INT := 0;
    v_anchor     DATE;
BEGIN
    SELECT COALESCE(jsonb_agg(s.name ORDER BY s.name), '[]'::JSONB)
    INTO v_new_plants
    FROM (
        SELECT cp.name
        FROM meal_items mi
        JOIN logged_meals m      ON m.id = mi.meal_id
        JOIN canonical_plants cp ON cp.id = mi.canonical_plant_id
        GROUP BY cp.name
        HAVING date_trunc('week', MIN(m.logged_at AT TIME ZONE 'Asia/Hong_Kong')) =
               date_trunc('week', now() AT TIME ZONE 'Asia/Hong_Kong')
    ) s;

    SELECT COALESCE(
        jsonb_agg(jsonb_build_object('name', s.raw, 'count', s.c)), '[]'::JSONB)
    INTO v_top_meals
    FROM (
        SELECT lower(raw_user_string) AS raw, COUNT(*) AS c
        FROM logged_meals
        WHERE logged_at >= now() - interval '28 days'
        GROUP BY lower(raw_user_string)
        HAVING COUNT(*) >= 2
        ORDER BY COUNT(*) DESC, lower(raw_user_string)
        LIMIT 3
    ) s;

    SELECT MAX(summary_date) INTO v_anchor
    FROM daily_summaries
    WHERE summary_date <= v_today;

    IF v_anchor IS NOT NULL AND v_anchor >= v_today - 1 THEN
        -- Count rows while summary_date stays consecutive from the anchor.
        -- rn must live in a subquery: window functions can't be filtered in WHERE.
        SELECT COUNT(*) INTO v_streak
        FROM (
            SELECT summary_date,
                   ROW_NUMBER() OVER (ORDER BY summary_date DESC) AS rn
            FROM daily_summaries
            WHERE summary_date <= v_anchor
        ) t
        WHERE t.summary_date = v_anchor - (t.rn - 1)::INT;
    END IF;

    RETURN jsonb_build_object(
        'new_plants',  v_new_plants,
        'top_meals',   v_top_meals,
        'streak_days', v_streak
    );
END;
$$;
