-- Migration: expand canonical_plants and relax category CHECK constraints.
-- Run once in Supabase SQL editor.

-- 1. Add 'other' to category constraints
ALTER TABLE canonical_plants
    DROP CONSTRAINT IF EXISTS canonical_plants_category_check;
ALTER TABLE canonical_plants
    ADD CONSTRAINT canonical_plants_category_check
        CHECK (category IN ('leaf','legume','nut','seed','whole_grain',
                            'fruit','vegetable','tuber','herb','spice','other'));

ALTER TABLE meal_items
    DROP CONSTRAINT IF EXISTS meal_items_plant_category_check;
ALTER TABLE meal_items
    ADD CONSTRAINT meal_items_plant_category_check
        CHECK (plant_category IN ('leaf','legume','nut','seed','whole_grain',
                                  'fruit','vegetable','tuber','herb','spice','other'));

-- 2. Insert new plants (idempotent)
INSERT INTO canonical_plants (name, category) VALUES
-- legume additions
('butter beans',          'legume'),
('soybeans',              'legume'),
('tofu',                  'legume'),
('tempeh',                'legume'),
-- nut additions
('pine nuts',             'nut'),
('chestnuts',             'nut'),
-- whole_grain additions
('rye',                   'whole_grain'),
('whole wheat',           'whole_grain'),
('wild rice',             'whole_grain'),
('sorghum',               'whole_grain'),
('teff',                  'whole_grain'),
('amaranth',              'whole_grain'),
-- fruit additions
('kiwi',                  'fruit'),
('figs',                  'fruit'),
('grapes',                'fruit'),
('cherries',              'fruit'),
('pear',                  'fruit'),
('watermelon',            'fruit'),
('papaya',                'fruit'),
('dragon fruit',          'fruit'),
-- vegetable additions
('cabbage',               'vegetable'),
('napa cabbage',          'vegetable'),
('artichoke',             'vegetable'),
('chives',                'vegetable'),
('radish',                'vegetable'),
('daikon',                'vegetable'),
('corn',                  'vegetable'),
('okra',                  'vegetable'),
('bitter melon',          'vegetable'),
('lotus root',            'vegetable'),
('water chestnut',        'vegetable'),
-- tuber additions
('taro',                  'tuber'),
('purple yam',            'tuber'),
('cassava',               'tuber'),
-- herb additions (plain names for fresh/dried matching)
('basil',                 'herb'),
('cilantro',              'herb'),
('parsley',               'herb'),
('mint',                  'herb'),
('dill',                  'herb'),
('oregano',               'herb'),
('rosemary',              'herb'),
('sage',                  'herb'),
('tarragon',              'herb'),
-- spice additions
('paprika',               'spice'),
('allspice',              'spice'),
('nutmeg',                'spice'),
('star anise',            'spice'),
('five-spice',            'spice'),
('sichuan pepper',        'spice'),
('coriander seed',        'spice'),
('chili flakes',          'spice'),
-- other: ZOE-counted plant compounds
('coffee',                'other'),
('green tea',             'other'),
('black tea',             'other'),
('herbal tea',            'other'),
('extra virgin olive oil','other'),
('dark chocolate',        'other')
ON CONFLICT (name) DO NOTHING;
