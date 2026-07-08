DO $$
DECLARE
    v_meal_id UUID;
BEGIN
    INSERT INTO meals (
        logged_at, meal_type, raw_user_string, is_template_recipe,
        total_calories, total_protein_g, total_carbs_g, total_fat_g, total_fiber_g
    ) VALUES (
        NULL, 'breakfast', 'Overnight oatmeal', true,
        640, 44.4, 59.5, 25.7, 11.6
    ) RETURNING id INTO v_meal_id;

    INSERT INTO meal_items (
        meal_id, food_name, quantity, unit, fraction_eaten,
        calories, protein_g, carbs_g, fat_g, fiber_g,
        is_plant, plant_name, plant_category, canonical_plant_id
    ) VALUES
        (v_meal_id, 'Rolled oats',           62,  'g',  1.0, 235, 8.2,  42.0, 4.0, 6.3, true,  'Rolled oats',   'whole_grain', (SELECT id FROM canonical_plants WHERE similarity(name, 'rolled oats') > 0.3 ORDER BY similarity(name, 'rolled oats') DESC LIMIT 1)),
        (v_meal_id, 'Pumpkin seeds',         20,  'g',  1.0, 112, 6.0,  2.2,  9.2, 1.2, true,  'Pumpkin seeds', 'seed',        (SELECT id FROM canonical_plants WHERE similarity(name, 'pumpkin seeds') > 0.3 ORDER BY similarity(name, 'pumpkin seeds') DESC LIMIT 1)),
        (v_meal_id, 'Chia seeds',            10,  'g',  1.0, 49,  1.7,  4.2,  3.1, 3.4, true,  'Chia seeds',    'seed',        (SELECT id FROM canonical_plants WHERE similarity(name, 'chia seeds') > 0.3 ORDER BY similarity(name, 'chia seeds') DESC LIMIT 1)),
        (v_meal_id, 'Go Good WPC chocolate', 30,  'g',  1.0, 120, 22.5, 1.9,  2.2, 0.7, false, NULL,            NULL,          NULL),
        (v_meal_id, 'Whole cow milk',        200, 'ml', 1.0, 124, 6.0,  9.2,  7.2, 0.0, false, NULL,            NULL,          NULL);
END;
$$;
