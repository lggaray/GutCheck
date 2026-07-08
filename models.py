"""Pydantic schemas for the nutrition tracker parsing contract."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class MealType(str, Enum):
    breakfast = "breakfast"
    lunch = "lunch"
    dinner = "dinner"
    extra = "extra"


class PlantCategory(str, Enum):
    leaf = "leaf"
    legume = "legume"
    nut = "nut"
    seed = "seed"
    whole_grain = "whole_grain"
    fruit = "fruit"
    vegetable = "vegetable"
    tuber = "tuber"
    herb = "herb"
    spice = "spice"


class MacroEstimation(BaseModel):
    calories: int = Field(ge=0, le=5000)
    protein_g: float = Field(ge=0, le=500)
    carbs_g: float = Field(ge=0, le=500)
    fat_g: float = Field(ge=0, le=500)
    fiber_g: float = Field(ge=0, le=200)


class MealItem(BaseModel):
    food_name: str = Field(min_length=1, max_length=200)
    raw_description: str
    quantity: float = Field(gt=0, le=10000)
    unit: str
    fraction_eaten: float = Field(default=1.0, gt=0, le=1)
    macros: MacroEstimation
    is_plant: bool
    plant_name: Optional[str] = None
    plant_category: Optional[PlantCategory] = None


class PlantItem(BaseModel):
    plant_name: str
    category: PlantCategory


class LoggedMeal(BaseModel):
    meal_type: MealType
    raw_user_string: str
    items: list[MealItem]
    plants_detected: list[PlantItem]
    total_calories: int = Field(ge=0, le=10000)
    total_protein_g: float = Field(ge=0, le=1500)
    total_carbs_g: float = Field(ge=0, le=1500)
    total_fat_g: float = Field(ge=0, le=1500)
    total_fiber_g: float = Field(ge=0, le=400)

    @model_validator(mode="after")
    def check_plant_consistency(self) -> "LoggedMeal":
        """plants_detected must mirror plant-flagged items exactly."""
        from_items = {
            item.plant_name
            for item in self.items
            if item.is_plant and item.plant_name
        }
        from_detected = {p.plant_name for p in self.plants_detected}
        if from_items != from_detected:
            raise ValueError(
                f"Plant mismatch — items: {from_items}, detected: {from_detected}"
            )
        return self


def compute_totals(items: list[MealItem]) -> dict:
    """Server-side totals from per-item macros × fraction_eaten.

    The LLM's total_* fields are advisory only; these computed values are
    what gets stored, so totals are always internally consistent and a
    totals mismatch can never fail a parse.
    """
    return {
        "total_calories":  round(sum(i.macros.calories  * i.fraction_eaten for i in items)),
        "total_protein_g": round(sum(i.macros.protein_g * i.fraction_eaten for i in items), 1),
        "total_carbs_g":   round(sum(i.macros.carbs_g   * i.fraction_eaten for i in items), 1),
        "total_fat_g":     round(sum(i.macros.fat_g     * i.fraction_eaten for i in items), 1),
        "total_fiber_g":   round(sum(i.macros.fiber_g   * i.fraction_eaten for i in items), 1),
    }

