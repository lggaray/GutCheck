-- Fix 2026-06-12: Go Good unit was '1 serve (30g / 2 scoops)' — model applied
-- full-serve macros to "one scoop". Store per-scoop so scaling is natural.
UPDATE personal_ingredients
SET unit_desc = '1 scoop (15g)',
    calories  = 60,
    protein_g = 11.7,
    carbs_g   = 0.75,
    fat_g     = 1.1,
    fiber_g   = 0.25,
    notes     = 'WPC, official NZ panel; label serve = 30g (2 scoops), stored per scoop'
WHERE name = 'Go Good whey protein chocolate';
