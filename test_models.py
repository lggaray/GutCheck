"""Unit tests for models.py — compute_totals and validator behavior. No network."""

import unittest

from models import (
    LoggedMeal, MealItem, MealType, MacroEstimation,
    PlantItem, PlantCategory, compute_totals,
)


def _item(cal=100, pro=10.0, carb=10.0, fat=5.0, fib=2.0, fraction=1.0,
          is_plant=False, plant_name=None):
    return MealItem(
        food_name=plant_name or "food",
        raw_description="desc",
        quantity=100,
        unit="g",
        fraction_eaten=fraction,
        macros=MacroEstimation(
            calories=cal, protein_g=pro, carbs_g=carb, fat_g=fat, fiber_g=fib
        ),
        is_plant=is_plant,
        plant_name=plant_name,
        plant_category=PlantCategory.vegetable if is_plant else None,
    )


class TestComputeTotals(unittest.TestCase):
    def test_sums_scaled_by_fraction(self):
        totals = compute_totals([_item(cal=200, pro=20.0, fraction=0.5),
                                 _item(cal=100, pro=10.0)])
        self.assertEqual(totals["total_calories"], 200)   # 200*0.5 + 100
        self.assertEqual(totals["total_protein_g"], 20.0) # 20*0.5 + 10

    def test_calories_rounded_to_int(self):
        totals = compute_totals([_item(cal=95, fraction=0.3)])  # 28.5 → 28 (banker's)
        self.assertIsInstance(totals["total_calories"], int)

    def test_grams_rounded_one_decimal(self):
        totals = compute_totals([_item(pro=7.77, fraction=0.5)])
        self.assertEqual(totals["total_protein_g"], 3.9)

    def test_empty_items_all_zero(self):
        totals = compute_totals([])
        self.assertEqual(totals["total_calories"], 0)
        self.assertEqual(totals["total_fiber_g"], 0)


class TestValidatorBehavior(unittest.TestCase):
    def test_mismatched_totals_no_longer_rejected(self):
        """LLM totals are advisory now — construction must succeed."""
        meal = LoggedMeal(
            meal_type=MealType.lunch,
            raw_user_string="x",
            items=[_item(cal=100)],
            plants_detected=[],
            total_calories=999,      # wildly off on purpose
            total_protein_g=0.0,
            total_carbs_g=0.0,
            total_fat_g=0.0,
            total_fiber_g=0.0,
        )
        self.assertEqual(meal.total_calories, 999)

    def test_plant_consistency_still_enforced(self):
        """The plant-mirror validator must survive the change."""
        with self.assertRaises(ValueError):
            LoggedMeal(
                meal_type=MealType.lunch,
                raw_user_string="x",
                items=[_item(is_plant=True, plant_name="broccoli")],
                plants_detected=[],   # missing broccoli → must raise
                total_calories=100,
                total_protein_g=10.0,
                total_carbs_g=10.0,
                total_fat_g=5.0,
                total_fiber_g=2.0,
            )


class TestTotalsOverridePattern(unittest.TestCase):
    def test_model_copy_override_replaces_llm_totals(self):
        """The extract.py post-parse pattern must yield computed totals."""
        meal = LoggedMeal(
            meal_type=MealType.dinner,
            raw_user_string="placeholder",
            items=[_item(cal=200, pro=20.0, carb=30.0, fat=8.0, fib=4.0, fraction=0.5)],
            plants_detected=[],
            total_calories=555,  # wrong on purpose — must be overwritten
            total_protein_g=1.0,
            total_carbs_g=1.0,
            total_fat_g=1.0,
            total_fiber_g=1.0,
        )
        fixed = meal.model_copy(
            update={"raw_user_string": "2 eggs", **compute_totals(meal.items)}
        )
        self.assertEqual(fixed.total_calories, 100)
        self.assertEqual(fixed.total_protein_g, 10.0)
        self.assertEqual(fixed.raw_user_string, "2 eggs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
