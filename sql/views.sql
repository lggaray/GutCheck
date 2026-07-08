-- logged_meals: all analytical queries use this view, never the raw table.
-- Filters out recipe templates so only real eating events are returned.
CREATE OR REPLACE VIEW logged_meals AS
    SELECT *
    FROM meals
    WHERE is_template_recipe = false
      AND logged_at IS NOT NULL;
