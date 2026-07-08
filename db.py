"""Supabase insert helpers via REST API — no binary dependencies.

log_meal() calls the log_meal() PostgreSQL function via a single RPC request.
Plant resolution (pg_trgm) and all inserts run in one DB transaction.
"""

import json
import os
import urllib.request
from datetime import datetime, timezone

from models import LoggedMeal


def _rpc(function_name: str, params: dict):
    """POST to Supabase RPC endpoint and return parsed JSON."""
    url = f"{os.environ['SUPABASE_URL']}/rest/v1/rpc/{function_name}"
    payload = json.dumps(params).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={
            "Content-Type": "application/json",
            "apikey": os.environ["SUPABASE_KEY"],
            "Authorization": f"Bearer {os.environ['SUPABASE_KEY']}",
            "Prefer": "return=representation",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _rest_get(table: str, select: str = "*", limit: int = 1):
    """GET rows from a Supabase table via REST."""
    url = f"{os.environ['SUPABASE_URL']}/rest/v1/{table}?select={select}&limit={limit}"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": os.environ["SUPABASE_KEY"],
            "Authorization": f"Bearer {os.environ['SUPABASE_KEY']}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _rest_patch(table: str, payload: dict, match: str = "id=eq.1"):
    """PATCH rows in a Supabase table via REST."""
    url = f"{os.environ['SUPABASE_URL']}/rest/v1/{table}?{match}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "apikey": os.environ["SUPABASE_KEY"],
            "Authorization": f"Bearer {os.environ['SUPABASE_KEY']}",
            "Prefer": "return=minimal",
        },
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


def insert_meal(meal: LoggedMeal) -> str:
    """Insert a LoggedMeal atomically via the log_meal DB function."""
    payload = meal.model_dump(mode="json")
    result = _rpc("log_meal", {"p_data": payload})
    return result


def get_daily_context() -> dict:
    """Return today's macro totals, targets, weekly plant count, and day-of-week."""
    result = _rpc("get_daily_context", {})
    return result if isinstance(result, dict) else {}


def get_profile() -> dict | None:
    """Return the single user_profiles row, or None if empty."""
    rows = _rest_get("user_profiles", select="*", limit=1)
    return rows[0] if rows else None


def update_onboarding_step(chat_id: int, step: int) -> None:
    """Set onboarding_step and persist telegram_chat_id."""
    _rest_patch("user_profiles", {"onboarding_step": step, "telegram_chat_id": chat_id})


def store_onboarding_field(field: str, value) -> None:
    """Persist a single onboarding field to DB."""
    _rest_patch("user_profiles", {field: value})


def calc_targets(
    weight_kg: float, body_fat_pct: float,
    activity_level: str, goal_type: str,
) -> dict:
    """TDEE (Katch-McArdle) + macro targets. Pure function — no I/O.

    Protein: 2.0 g/kg bodyweight for 'lose' (recomposition, ISSN/ACSM),
    1.8 g/kg LBM otherwise — so re-onboarding can't regress a recomp target.
    """
    lbm = weight_kg * (1 - body_fat_pct / 100.0)
    bmr = 370 + 21.6 * lbm
    mult = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "active": 1.725}[activity_level]
    tdee = round(bmr * mult)
    cal_adj = tdee + {"maintain": 0, "lose": -400, "gain": 250}[goal_type]
    protein = round(weight_kg * 2.0) if goal_type == "lose" else round(lbm * 1.8)
    fat = round(weight_kg * 0.9)
    carbs = max(round((cal_adj - protein * 4 - fat * 9) / 4), 50)
    return {
        "tdee":             tdee,
        "target_calories":  cal_adj,
        "target_protein_g": protein,
        "target_carbs_g":   carbs,
        "target_fat_g":     fat,
        "target_fiber_g":   30,
    }


def complete_onboarding(
    weight_kg: float, body_fat_pct: float,
    activity_level: str, goal_type: str, chat_id: int,
) -> None:
    """Calculate TDEE + macros in Python and write all targets via REST PATCH."""
    targets = calc_targets(weight_kg, body_fat_pct, activity_level, goal_type)
    _rest_patch("user_profiles", {
        "weight_kg":        weight_kg,
        "body_fat_pct":     body_fat_pct,
        "activity_level":   activity_level,
        "goal_type":        goal_type,
        **targets,
        "telegram_chat_id": chat_id,
        "onboarding_step":  5,
    })


def get_chat_id() -> int | None:
    """Return stored telegram_chat_id from user_profiles."""
    rows = _rest_get("user_profiles", select="telegram_chat_id", limit=1)
    if rows and rows[0].get("telegram_chat_id"):
        return int(rows[0]["telegram_chat_id"])
    return None


def check_meal_logged_today(meal_type: str) -> bool:
    """Return True if a main meal (not 'extra') of this type was already logged today (HKT)."""
    result = _rpc("check_meal_logged_today", {"p_meal_type": meal_type})
    return bool(result)


def get_meals_logged_today() -> list[str]:
    """Return list of main meal types (breakfast/lunch/dinner) logged today (HKT).

    'extra' is excluded — it never suppresses cron prompts or affects classification.
    """
    result = _rpc("get_meals_logged_today", {})
    if isinstance(result, list):
        return [str(m) for m in result]
    return []


def get_weekly_plant_count() -> int:
    """Return count of distinct canonical plants logged this ISO week."""
    result = _rpc("get_weekly_plant_count", {})
    return int(result) if result is not None else 0


def get_weekly_summary() -> dict:
    """Return weekly macro averages, targets, plant count, and days logged."""
    result = _rpc("get_weekly_summary", {})
    return result if isinstance(result, dict) else {}


def use_recipe(name: str, user_string: str, meal_type: str | None = None) -> dict:
    result = _rpc("use_recipe", {"p_name": name, "p_user_string": user_string, "p_meal_type": meal_type})
    return result if isinstance(result, dict) else {}


def list_recipes() -> list:
    """Return list of all recipes."""
    result = _rpc("list_recipes", {})
    return result if isinstance(result, list) else []


def get_gap_nudge() -> dict | None:
    """Return {fiber_gap, protein_gap} if a gap streak exists, else None."""
    result = _rpc("get_gap_nudge", {})
    return result if isinstance(result, dict) else None


def mark_gap_nudge_sent() -> None:
    """Record that a gap nudge was sent now (UTC)."""
    _rest_patch("user_profiles", {
        "last_gap_nudge_sent_at": datetime.now(timezone.utc).isoformat(),
    })


def undo_last_meal() -> dict:
    """Delete today's most recent meal; returns its summary or {"deleted": false}."""
    result = _rpc("undo_last_meal", {})
    return result if isinstance(result, dict) else {}


def get_week_plants() -> list:
    """Return this week's plants as [{name, category, auto_added}], ordered by category, name."""
    result = _rpc("get_week_plants", {})
    return result if isinstance(result, list) else []


def record_weekly_weight(weight_kg: float) -> dict:
    """Record this week's weight; returns {week_start, weight_kg}."""
    result = _rpc("record_weekly_weight", {"p_weight_kg": weight_kg})
    return result if isinstance(result, dict) else {}


def set_awaiting_weight(flag: bool) -> None:
    """Set the awaiting_weight flag on the single user_profiles row."""
    _rest_patch("user_profiles", {"awaiting_weight": flag})


def set_last_update_id(update_id: int) -> None:
    """Record the highest processed Telegram update_id (webhook dedup)."""
    _rest_patch("user_profiles", {"last_update_id": update_id})


def get_personal_ingredients() -> list:
    """Return personal ingredient corrections (label-exact macros per unit)."""
    rows = _rest_get(
        "personal_ingredients",
        select="name,unit_desc,calories,protein_g,carbs_g,fat_g,fiber_g",
        limit=100,
    )
    return rows if isinstance(rows, list) else []


def get_today_first_time_plants() -> list[str]:
    """Canonical plant names whose first-ever log is today (HKT). Reply flair."""
    result = _rpc("get_today_first_time_plants", {})
    return [str(p) for p in result] if isinstance(result, list) else []


def get_week_patterns() -> dict:
    """Weekly pattern report: new plants, repeated meals, logging streak."""
    result = _rpc("get_week_patterns", {})
    return result if isinstance(result, dict) else {}
