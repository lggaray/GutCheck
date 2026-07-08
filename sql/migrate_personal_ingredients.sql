-- Migration 2026-06-12 (2): personal_ingredients correction table
--
-- Label-exact macros per unit for products the LLM estimates badly.
-- Injected into the extraction system prompt — the model uses these EXACT
-- values scaled by quantity instead of estimating.
-- Reactive table: add a row when a bad estimate is caught.

CREATE TABLE IF NOT EXISTS personal_ingredients (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    unit_desc   TEXT NOT NULL,                -- e.g. '1 wedge (17.5g)', 'per 100g', '200ml bottle'
    calories    NUMERIC NOT NULL CHECK (calories >= 0 AND calories <= 2000),
    protein_g   NUMERIC NOT NULL CHECK (protein_g >= 0 AND protein_g <= 200),
    carbs_g     NUMERIC NOT NULL CHECK (carbs_g >= 0 AND carbs_g <= 300),
    fat_g       NUMERIC NOT NULL CHECK (fat_g >= 0 AND fat_g <= 200),
    fiber_g     NUMERIC NOT NULL DEFAULT 0 CHECK (fiber_g >= 0 AND fiber_g <= 100),
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE personal_ingredients ENABLE ROW LEVEL SECURITY;

INSERT INTO personal_ingredients (name, unit_desc, calories, protein_g, carbs_g, fat_g, fiber_g, notes) VALUES
('Laughing Cow original spreadable cheese', '1 wedge (17.5g)', 35, 2.0, 1.0, 2.7, 0,
 'Asia pack label, confirmed by user Jun 12 2026'),
('Red Tractor rolled oats', 'per 100g', 375, 13.0, 55.0, 10.0, 10.0,
 'Regular rolled oats line, AU label; fiber is typical-value estimate — verify from pack'),
('Chobani Greek yogurt plain whole milk', 'per 100g', 86, 8.7, 3.5, 4.3, 0,
 'Plain whole-milk tub'),
('Kagome vegetable and fruit juice', '200ml serving', 70, 0.7, 16.5, 0, 0.8,
 'Yasai Seikatsu mixed line (carrot/grape/mango), averaged across flavors — verify per bottle'),
('Go Good whey protein chocolate', '1 scoop (15g)', 60, 11.7, 0.75, 1.1, 0.25,
 'WPC, official NZ panel; label serve = 30g (2 scoops), stored per scoop so "one scoop" scales right')
ON CONFLICT (name) DO NOTHING;
