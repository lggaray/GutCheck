"""End-to-end tests: meal reply redesign + 'extra' classification.

All tests run without a real DB or Gemini connection — external calls are mocked.

Test plan:
  a. Full breakfast → two-section reply (This meal + Today so far)
  b. "just had a banana" → 'extra' classification, concise reply
  c. 1:30pm cron prompt fires after only an 'extra' was logged
  d. After lunch is logged, lunch cron prompt is suppressed
"""

import json
import unittest
from unittest.mock import patch, MagicMock

from models import LoggedMeal, MealType, MealItem, PlantItem, MacroEstimation, PlantCategory
from handler import (
    _format_reply, _format_weekly_checkin, _handle_meal_prompt, lambda_handler,
    _format_recipe_reply, _format_list_recipes, _meal_type_from_time,
    _format_gap_nudge, _handle_gap_nudge,
    _md_escape, _format_plants_reply, _format_undo_reply,
    _format_weight_confirmation, _parse_weight,
    _photo_file_id, _handle_photo_meal, _format_help, _format_today_reply,
)
from db import calc_targets
from extract import _format_known_ingredients


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _make_meal(
    meal_type: MealType,
    cal: int = 500,
    protein: float = 35.0,
    carbs: float = 45.0,
    fat: float = 15.0,
    fiber: float = 6.0,
    plants: list[str] | None = None,
) -> LoggedMeal:
    plant_items = []
    meal_items = []

    if plants:
        for p in plants:
            meal_items.append(MealItem(
                food_name=p,
                raw_description=p,
                quantity=100,
                unit="g",
                fraction_eaten=1.0,
                macros=MacroEstimation(
                    calories=cal // len(plants),
                    protein_g=protein / len(plants),
                    carbs_g=carbs / len(plants),
                    fat_g=fat / len(plants),
                    fiber_g=fiber / len(plants),
                ),
                is_plant=True,
                plant_name=p,
                plant_category=PlantCategory.vegetable,
            ))
            plant_items.append(PlantItem(plant_name=p, category=PlantCategory.vegetable))
    else:
        meal_items.append(MealItem(
            food_name="chicken",
            raw_description="grilled chicken",
            quantity=150,
            unit="g",
            fraction_eaten=1.0,
            macros=MacroEstimation(
                calories=cal,
                protein_g=protein,
                carbs_g=carbs,
                fat_g=fat,
                fiber_g=fiber,
            ),
            is_plant=False,
        ))

    return LoggedMeal(
        meal_type=meal_type,
        raw_user_string="test",
        items=meal_items,
        plants_detected=plant_items,
        total_calories=cal,
        total_protein_g=protein,
        total_carbs_g=carbs,
        total_fat_g=fat,
        total_fiber_g=fiber,
    )


MOCK_CTX = {
    "day_calories": 800,
    "day_protein_g": 55.0,
    "day_carbs_g": 70.0,
    "day_fat_g": 25.0,
    "day_fiber_g": 10.0,
    "day_unique_plants": 2,
    "target_calories": 2000,
    "target_protein_g": 150.0,
    "weekly_plants": 4,
    "day_of_week": 2,
}

# Weekly check-in mock contexts
MOCK_WEEKLY_CTX = {
    "days_logged":     5,
    "weekly_plants":   22,
    "avg_calories":    1950,  "target_calories":  2384,
    "avg_protein_g":   100,   "target_protein_g": 140,   # 71% — GAP
    "avg_carbs_g":     240,   "target_carbs_g":   285,   # 84% — ok
    "avg_fat_g":       58,    "target_fat_g":     65,    # 89% — ok
    "avg_fiber_g":     22,    "target_fiber_g":   30,    # 73% — GAP
}

MOCK_WEEKLY_CTX_ZERO = {
    "days_logged":     0,
    "weekly_plants":   0,
    "avg_calories":    0,   "target_calories":  2384,
    "avg_protein_g":   0,   "target_protein_g": 140,
    "avg_carbs_g":     0,   "target_carbs_g":   285,
    "avg_fat_g":       0,   "target_fat_g":     65,
    "avg_fiber_g":     0,   "target_fiber_g":   30,
}

MOCK_RECIPE_WITH_PLANTS = {
    "matched_name": "Avocado Toast",
    "meal_id": "abc-123",
    "total_calories": 450,
    "total_protein_g": 12.0,
    "total_carbs_g": 38.0,
    "total_fat_g": 28.0,
    "total_fiber_g": 8.0,
    "plants": ["avocado", "whole grain bread"],
}

MOCK_RECIPE_NO_PLANTS = {
    "matched_name": "Chicken Rice",
    "meal_id": "def-456",
    "total_calories": 620,
    "total_protein_g": 45.0,
    "total_carbs_g": 68.0,
    "total_fat_g": 12.0,
    "total_fiber_g": 2.0,
    "plants": [],
}


class TestRecipeReplyFormatter(unittest.TestCase):
    def test_with_plants_has_all_sections(self):
        """Recipe reply keeps name, recipe macros, plants, weekly pace — no daily macros."""
        reply = _format_recipe_reply(MOCK_RECIPE_WITH_PLANTS, MOCK_CTX)
        self.assertIn("Recipe logged: *Avocado Toast*", reply)
        self.assertIn("This meal:", reply)
        self.assertIn("450 kcal", reply)
        self.assertIn("avocado", reply)
        self.assertIn("whole grain bread", reply)
        self.assertIn("4/30 plants this week", reply)
        self.assertNotIn("Today so far:", reply)
        self.assertNotIn("800 / 2000 kcal", reply)

    def test_macros_formatted_correctly(self):
        """All five macro fields appear in the reply."""
        reply = _format_recipe_reply(MOCK_RECIPE_WITH_PLANTS, MOCK_CTX)
        self.assertIn("Protein: 12g", reply)
        self.assertIn("Carbs: 38g", reply)
        self.assertIn("Fat: 28g", reply)
        self.assertIn("Fiber: 8g", reply)

    def test_no_plants_shows_no_plants_message(self):
        """Recipe with empty plants list shows 'No plants in this recipe.'"""
        reply = _format_recipe_reply(MOCK_RECIPE_NO_PLANTS, MOCK_CTX)
        self.assertIn("No plants in this recipe", reply)
        self.assertNotIn("🌿 chicken", reply)


MOCK_RECIPES_LIST = [
    {
        "name": "Avocado Toast",
        "meal_type": "breakfast",
        "calories": 450,
        "protein_g": 12.0,
        "carbs_g": 38.0,
        "fat_g": 28.0,
        "fiber_g": 8.0,
    },
    {
        "name": "Chicken Rice Bowl",
        "meal_type": "lunch",
        "calories": 620,
        "protein_g": 45.0,
        "carbs_g": 68.0,
        "fat_g": 12.0,
        "fiber_g": 5.0,
    },
]


class TestListRecipesFormatter(unittest.TestCase):
    def test_empty_list_message(self):
        """Empty list returns 'No recipes saved yet.'"""
        reply = _format_list_recipes([])
        self.assertIn("No recipes saved yet", reply)

    def test_single_recipe_shown(self):
        """Single recipe shows name, calories, and breakfast emoji."""
        reply = _format_list_recipes([MOCK_RECIPES_LIST[0]])
        self.assertIn("Avocado Toast", reply)
        self.assertIn("450 kcal", reply)
        self.assertIn("🍳", reply)

    def test_multiple_recipes_all_shown(self):
        """Multiple recipes all appear with correct meal-type emojis."""
        reply = _format_list_recipes(MOCK_RECIPES_LIST)
        self.assertIn("Avocado Toast", reply)
        self.assertIn("Chicken Rice Bowl", reply)
        self.assertIn("🍳", reply)
        self.assertIn("🥗", reply)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestMainMealReply(unittest.TestCase):
    def test_breakfast_journal_reply(self):
        """Breakfast reply: greeting, items echo, plants line, weekly pace."""
        meal = _make_meal(MealType.breakfast, plants=["broccoli", "spinach"])
        reply = _format_reply(meal, MOCK_CTX)
        self.assertIn("Good morning!", reply)
        self.assertIn("✅ broccoli · spinach", reply)
        self.assertIn("🌿 Plants: broccoli, spinach", reply)
        self.assertIn("4/30 plants this week", reply)

    def test_no_macros_shown(self):
        """Per-meal and daily macro numbers are gone from the reply."""
        meal = _make_meal(MealType.breakfast, plants=["broccoli"])
        reply = _format_reply(meal, MOCK_CTX)
        self.assertNotIn("kcal", reply)
        self.assertNotIn("This meal:", reply)
        self.assertNotIn("Today so far:", reply)
        self.assertNotIn("Protein", reply)

    def test_no_plants_nudge(self):
        """Meal with no plants echoes items and shows the nudge line."""
        meal = _make_meal(MealType.lunch)
        reply = _format_reply(meal, MOCK_CTX)
        self.assertIn("✅ chicken", reply)
        self.assertIn("No plants this meal", reply)

    def test_item_names_escaped(self):
        """Markdown specials in food names are escaped."""
        meal = _make_meal(MealType.lunch)
        meal.items[0].food_name = "chicken_satay*special"
        reply = _format_reply(meal, MOCK_CTX)
        self.assertIn("chicken\\_satay\\*special", reply)

    def test_first_time_flair_shown(self):
        meal = _make_meal(MealType.dinner, plants=["kohlrabi", "rice"])
        reply = _format_reply(meal, MOCK_CTX, first_time_plants=["kohlrabi"])
        self.assertIn("✨ First time logged: kohlrabi!", reply)

    def test_no_flair_when_none(self):
        meal = _make_meal(MealType.dinner, plants=["rice"])
        reply = _format_reply(meal, MOCK_CTX)
        self.assertNotIn("✨", reply)


class TestExtraReply(unittest.TestCase):
    def test_banana_journal_reply(self):
        """'extra' meal gets the same journal reply with generic greeting."""
        banana_item = MealItem(
            food_name="Banana",
            raw_description="banana",
            quantity=1,
            unit="medium",
            fraction_eaten=1.0,
            macros=MacroEstimation(
                calories=89, protein_g=1.1, carbs_g=23.0, fat_g=0.3, fiber_g=2.6
            ),
            is_plant=True,
            plant_name="Banana",
            plant_category=PlantCategory.fruit,
        )
        meal = LoggedMeal(
            meal_type=MealType.extra,
            raw_user_string="just had a banana",
            items=[banana_item],
            plants_detected=[PlantItem(plant_name="Banana", category=PlantCategory.fruit)],
            total_calories=89,
            total_protein_g=1.1,
            total_carbs_g=23.0,
            total_fat_g=0.3,
            total_fiber_g=2.6,
        )
        reply = _format_reply(meal, MOCK_CTX)
        self.assertIn("Meal logged. 🍽️", reply)
        self.assertIn("✅ Banana", reply)
        self.assertIn("🌿 Plants: Banana", reply)
        self.assertIn("4/30 plants this week", reply)
        self.assertNotIn("kcal", reply)

    def test_extra_no_plants(self):
        """Extra meal with no plants shows the nudge, no macros."""
        meal = _make_meal(MealType.extra, cal=120, protein=2.0, carbs=15.0, fat=6.0, fiber=0.0)
        reply = _format_reply(meal, MOCK_CTX)
        self.assertIn("No plants this meal", reply)
        self.assertNotIn("120 kcal", reply)


class TestTodayReply(unittest.TestCase):
    def test_shows_meals_and_plants_no_macros(self):
        """/today lists main meals logged, today's plant count, weekly pace — no macros."""
        reply = _format_today_reply(MOCK_CTX, ["breakfast", "lunch"])
        self.assertIn("Main meals logged: breakfast, lunch", reply)
        self.assertIn("🌿 2 plants today", reply)
        self.assertIn("4/30 plants this week", reply)
        self.assertNotIn("kcal", reply)
        self.assertNotIn("Today so far:", reply)

    def test_no_meals_yet(self):
        """Empty meals list reads 'none yet'."""
        reply = _format_today_reply(MOCK_CTX, [])
        self.assertIn("Main meals logged: none yet", reply)


class TestCronSmartSkip(unittest.TestCase):
    """(c) and (d): cron smart-skip only triggered by main meal types."""

    def _run_prompt(self, meal_type: str, logged_today: bool) -> str:
        """Run _handle_meal_prompt and return the message sent."""
        sent = []

        def fake_send(chat_id, text):
            sent.append(text)

        with patch("handler.get_chat_id", return_value=12345), \
             patch("handler.check_meal_logged_today", return_value=logged_today), \
             patch("handler.get_daily_context", return_value=MOCK_CTX), \
             patch("handler._send", side_effect=fake_send):
            _handle_meal_prompt(meal_type)

        return sent[0] if sent else ""

    def test_c_lunch_prompt_fires_after_extra(self):
        """(c) 1:30pm lunch prompt fires even when an 'extra' was logged."""
        # 'extra' logging never sets check_meal_logged_today("lunch") to True
        msg = self._run_prompt("lunch", logged_today=False)
        self.assertIn("Lunch", msg)
        self.assertNotIn("Already logged", msg)

    def test_d_lunch_prompt_suppressed_after_lunch(self):
        """(d) After lunch is logged, lunch cron prompt is suppressed."""
        msg = self._run_prompt("lunch", logged_today=True)
        self.assertIn("Already logged", msg)
        self.assertNotIn("Lunchtime", msg)

    def test_breakfast_prompt_fires_when_not_logged(self):
        """Breakfast prompt fires when breakfast not yet logged."""
        msg = self._run_prompt("breakfast", logged_today=False)
        self.assertIn("breakfast", msg.lower())


class TestMealTypeEnum(unittest.TestCase):
    def test_extra_in_enum(self):
        """MealType.extra exists and has value 'extra'."""
        self.assertEqual(MealType.extra.value, "extra")

    def test_all_four_types(self):
        values = {m.value for m in MealType}
        self.assertEqual(values, {"breakfast", "lunch", "dinner", "extra"})


class TestWeeklyCheckinFormatter(unittest.TestCase):

    def test_normal_week_sections_present(self):
        """Normal week reply has plants and days-logged lines."""
        reply = _format_weekly_checkin(MOCK_WEEKLY_CTX)
        self.assertIn("Week in review", reply)
        self.assertIn("22/30 this week", reply)
        self.assertIn("Logged 5 of 7 days", reply)

    def test_no_macro_averages_or_gaps(self):
        """Macro averages and gap callouts are gone."""
        reply = _format_weekly_checkin(MOCK_WEEKLY_CTX)
        self.assertNotIn("Macro averages", reply)
        self.assertNotIn("kcal", reply)
        self.assertNotIn("Gaps", reply)
        self.assertNotIn("Greek yogurt", reply)

    def test_zero_log_week(self):
        """Zero logged days shows the short fresh-start message."""
        reply = _format_weekly_checkin(MOCK_WEEKLY_CTX_ZERO)
        self.assertIn("No meals logged this week", reply)
        self.assertIn("0/30", reply)
        self.assertNotIn("Logged 0 of 7 days", reply)

    def test_patterns_enrich_the_report(self):
        patterns = {
            "new_plants": ["kohlrabi", "black beans"],
            "top_meals": [{"name": "overnight oats", "count": 4},
                          {"name": "avocado toast", "count": 2}],
            "streak_days": 12,
        }
        reply = _format_weekly_checkin(MOCK_WEEKLY_CTX, patterns)
        self.assertIn("✨ New this week: kohlrabi, black beans", reply)
        self.assertIn("12-day streak", reply)
        self.assertIn("🍽️ Repeats: overnight oats (×4), avocado toast (×2)", reply)

    def test_empty_patterns_add_nothing(self):
        patterns = {"new_plants": [], "top_meals": [], "streak_days": 3}
        reply = _format_weekly_checkin(MOCK_WEEKLY_CTX, patterns)
        self.assertNotIn("✨", reply)
        self.assertNotIn("Repeats", reply)
        self.assertNotIn("streak", reply)   # streaks ≤ 7 are just "days logged"

    def test_none_patterns_backward_compatible(self):
        reply = _format_weekly_checkin(MOCK_WEEKLY_CTX)
        self.assertIn("Logged 5 of 7 days", reply)


class TestWeeklyCheckinRoute(unittest.TestCase):

    def _call(self, body_dict: dict, cron_secret_env: str = "test-secret"):
        """Invoke lambda_handler with a weekly_checkin body, mocking _handle_weekly_checkin."""
        event = {"body": json.dumps(body_dict), "headers": {}}
        env_patch = {"CRON_SECRET": cron_secret_env}
        with patch.dict("os.environ", env_patch), \
             patch("handler._handle_weekly_checkin") as mock_fn:
            result = lambda_handler(event, None)
        return result, mock_fn

    def test_wrong_secret_rejected(self):
        """Wrong secret: handler not called, 200 returned."""
        result, mock_fn = self._call(
            {"type": "weekly_checkin", "secret": "wrong"},
            cron_secret_env="test-secret",
        )
        self.assertEqual(result["statusCode"], 200)
        mock_fn.assert_not_called()

    def test_missing_env_secret_rejected(self):
        """Empty CRON_SECRET in env: handler not called."""
        result, mock_fn = self._call(
            {"type": "weekly_checkin", "secret": "anything"},
            cron_secret_env="",
        )
        mock_fn.assert_not_called()

    def test_correct_secret_dispatches(self):
        """Correct secret: _handle_weekly_checkin called exactly once."""
        result, mock_fn = self._call(
            {"type": "weekly_checkin", "secret": "test-secret"},
            cron_secret_env="test-secret",
        )
        self.assertEqual(result["statusCode"], 200)
        mock_fn.assert_called_once()


MOCK_PROFILE = {
    "onboarding_step": 5,
    "telegram_chat_id": 99999,
    "awaiting_weight": False,
}


class TestRecipeRouting(unittest.TestCase):
    """Verify /recipe and /recipes slash commands route correctly."""

    def _call(self, text: str):
        """Invoke lambda_handler with a Telegram message, mocking recipe handlers and _send."""
        event = {
            "body": json.dumps({"message": {"chat": {"id": 99999}, "text": text}}),
            "headers": {"x-telegram-bot-api-secret-token": "test-secret"},
        }
        sent = []
        with patch.dict("os.environ", {"WEBHOOK_SECRET": "test-secret", "TELEGRAM_TOKEN": "tok"}), \
             patch("handler.get_profile", return_value=dict(MOCK_PROFILE)), \
             patch("handler._handle_recipe") as mock_recipe, \
             patch("handler._handle_list_recipes") as mock_list, \
             patch("handler._send", side_effect=lambda cid, msg: sent.append(msg)):
            result = lambda_handler(event, None)
        return result, sent, mock_recipe, mock_list

    def test_recipe_with_name_dispatches(self):
        """/recipe <name> calls _handle_recipe with correct args."""
        _, _, mock_recipe, mock_list = self._call("/recipe avocado toast")
        mock_recipe.assert_called_once_with(99999, "avocado toast", "/recipe avocado toast")
        mock_list.assert_not_called()

    def test_recipe_no_name_sends_usage(self):
        """/recipe (no name) sends usage hint, does not call _handle_recipe."""
        _, sent, mock_recipe, _ = self._call("/recipe")
        mock_recipe.assert_not_called()
        self.assertTrue(any("Usage" in m for m in sent))

    def test_recipes_dispatches_to_list(self):
        """/recipes calls _handle_list_recipes, not _handle_recipe."""
        _, _, mock_recipe, mock_list = self._call("/recipes")
        mock_list.assert_called_once_with(99999)
        mock_recipe.assert_not_called()

    def test_unknown_slash_falls_through(self):
        """Unknown slash command sends the available-commands message."""
        _, sent, mock_recipe, mock_list = self._call("/unknown")
        mock_recipe.assert_not_called()
        mock_list.assert_not_called()
        self.assertTrue(any("meal description" in m for m in sent))
        for cmd in ("/today", "/week", "/plants", "/undo", "/recipe", "/recipes"):
            self.assertTrue(any(cmd in m for m in sent), f"{cmd} not listed")


class TestMealTypeFromTime(unittest.TestCase):
    def test_breakfast_window(self):
        self.assertEqual(_meal_type_from_time(5),  "breakfast")
        self.assertEqual(_meal_type_from_time(7),  "breakfast")
        self.assertEqual(_meal_type_from_time(10), "breakfast")

    def test_lunch_window(self):
        self.assertEqual(_meal_type_from_time(11), "lunch")
        self.assertEqual(_meal_type_from_time(13), "lunch")
        self.assertEqual(_meal_type_from_time(16), "lunch")

    def test_dinner_window(self):
        self.assertEqual(_meal_type_from_time(17), "dinner")
        self.assertEqual(_meal_type_from_time(20), "dinner")
        self.assertEqual(_meal_type_from_time(23), "dinner")

    def test_late_night_falls_back_to_dinner(self):
        self.assertEqual(_meal_type_from_time(0),  "dinner")
        self.assertEqual(_meal_type_from_time(4),  "dinner")


class TestGapNudgeFormatter(unittest.TestCase):

    def test_fiber_gap_only(self):
        """fiber_gap=True, protein_gap=False: fiber line present, protein line absent."""
        reply = _format_gap_nudge({"fiber_gap": True, "protein_gap": False})
        self.assertIn("Nutrition heads-up", reply)
        self.assertIn("Low fiber", reply)
        self.assertIn("lentils", reply)
        self.assertNotIn("Low protein", reply)

    def test_protein_gap_only(self):
        """fiber_gap=False, protein_gap=True: protein line present, fiber line absent."""
        reply = _format_gap_nudge({"fiber_gap": False, "protein_gap": True})
        self.assertIn("Nutrition heads-up", reply)
        self.assertIn("Low protein", reply)
        self.assertIn("Greek yogurt", reply)
        self.assertNotIn("Low fiber", reply)

    def test_both_gaps(self):
        """Both gaps true: both lines present."""
        reply = _format_gap_nudge({"fiber_gap": True, "protein_gap": True})
        self.assertIn("Low fiber", reply)
        self.assertIn("lentils", reply)
        self.assertIn("Low protein", reply)
        self.assertIn("Greek yogurt", reply)

    def test_no_gaps(self):
        """Empty result: returns header only, no gap lines."""
        reply = _format_gap_nudge({})
        self.assertIn("Nutrition heads-up", reply)
        self.assertNotIn("Low fiber", reply)
        self.assertNotIn("Low protein", reply)


class TestHandleGapNudge(unittest.TestCase):

    def test_no_send_when_rpc_returns_none(self):
        """get_gap_nudge() returns None: _send and mark_gap_nudge_sent not called."""
        with patch("handler.get_chat_id", return_value=12345), \
             patch("handler.get_gap_nudge", return_value=None), \
             patch("handler._send") as mock_send, \
             patch("handler.mark_gap_nudge_sent") as mock_mark:
            _handle_gap_nudge()
        mock_send.assert_not_called()
        mock_mark.assert_not_called()

    def test_send_and_mark_when_gap_found(self):
        """get_gap_nudge() returns gaps: _send and mark_gap_nudge_sent each called once."""
        with patch("handler.get_chat_id", return_value=12345), \
             patch("handler.get_gap_nudge", return_value={"fiber_gap": True, "protein_gap": False}), \
             patch("handler._send") as mock_send, \
             patch("handler.mark_gap_nudge_sent") as mock_mark:
            _handle_gap_nudge()
        mock_send.assert_called_once()
        mock_mark.assert_called_once()

    def test_no_send_when_no_chat_id(self):
        """No chat_id stored: _send never called."""
        with patch("handler.get_chat_id", return_value=None), \
             patch("handler.get_gap_nudge") as mock_rpc, \
             patch("handler._send") as mock_send:
            _handle_gap_nudge()
        mock_rpc.assert_not_called()
        mock_send.assert_not_called()


class TestGapNudgeRoute(unittest.TestCase):

    def _call(self, body_dict: dict, cron_secret_env: str = "test-secret"):
        """Invoke lambda_handler with a gap_nudge body, mocking _handle_gap_nudge."""
        event = {"body": json.dumps(body_dict), "headers": {}}
        with patch.dict("os.environ", {"CRON_SECRET": cron_secret_env}), \
             patch("handler._handle_gap_nudge") as mock_fn:
            result = lambda_handler(event, None)
        return result, mock_fn

    def test_wrong_secret_rejected(self):
        """Wrong secret: handler not called, 200 returned."""
        result, mock_fn = self._call(
            {"type": "gap_nudge", "secret": "wrong"},
            cron_secret_env="test-secret",
        )
        self.assertEqual(result["statusCode"], 200)
        mock_fn.assert_not_called()

    def test_missing_env_secret_rejected(self):
        """Empty CRON_SECRET in env: handler not called."""
        result, mock_fn = self._call(
            {"type": "gap_nudge", "secret": "anything"},
            cron_secret_env="",
        )
        mock_fn.assert_not_called()

    def test_correct_secret_dispatches(self):
        """Correct secret: _handle_gap_nudge called exactly once, 200 returned."""
        result, mock_fn = self._call(
            {"type": "gap_nudge", "secret": "test-secret"},
            cron_secret_env="test-secret",
        )
        self.assertEqual(result["statusCode"], 200)
        mock_fn.assert_called_once()


class TestMdEscape(unittest.TestCase):

    def test_escapes_all_specials(self):
        """Underscore, asterisk, bracket, and backtick are backslash-escaped."""
        self.assertEqual(_md_escape("a_b"), "a\\_b")
        self.assertEqual(_md_escape("a*b"), "a\\*b")
        self.assertEqual(_md_escape("a[b"), "a\\[b")
        self.assertEqual(_md_escape("a`b"), "a\\`b")

    def test_multiple_and_repeated_specials(self):
        """All occurrences escaped, mixed specials handled in one pass."""
        self.assertEqual(_md_escape("_*[`_"), "\\_\\*\\[\\`\\_")

    def test_plain_text_unchanged(self):
        """Text without specials passes through untouched."""
        self.assertEqual(_md_escape("bok choy & rice (1 cup)"), "bok choy & rice (1 cup)")


MOCK_WEEK_PLANTS = [
    {"name": "banana",   "category": "fruit",     "auto_added": False},
    {"name": "lentils",  "category": "legume",    "auto_added": False},
    {"name": "bok choy", "category": "vegetable", "auto_added": False},
    {"name": "broccoli", "category": "vegetable", "auto_added": True},
]


class TestPlantsFormatter(unittest.TestCase):

    def test_header_count_matches_list_length(self):
        """Header shows len(list)/30."""
        reply = _format_plants_reply(MOCK_WEEK_PLANTS)
        self.assertIn("*4/30 plants this week*", reply)

    def test_grouped_by_category(self):
        """Names of the same category share one line, comma-separated."""
        reply = _format_plants_reply(MOCK_WEEK_PLANTS)
        self.assertIn("fruit: banana", reply)
        self.assertIn("legume: lentils", reply)
        self.assertIn("vegetable: bok choy, broccoli ⚠️", reply)

    def test_auto_added_marker(self):
        """auto_added plants get the warning suffix; others do not."""
        reply = _format_plants_reply(MOCK_WEEK_PLANTS)
        self.assertIn("broccoli ⚠️", reply)
        self.assertNotIn("bok choy ⚠️", reply)

    def test_missing_categories_listed(self):
        """Categories with zero entries this week appear on the Missing line."""
        reply = _format_plants_reply(MOCK_WEEK_PLANTS)
        missing_line = [l for l in reply.splitlines() if l.startswith("Missing:")][0]
        for cat in ("leaf", "nut", "seed", "whole_grain", "tuber", "herb", "spice"):
            self.assertIn(cat, missing_line)
        for cat in ("fruit", "legume", "vegetable"):
            self.assertNotIn(cat, missing_line)

    def test_no_missing_line_when_all_covered(self):
        """All 10 categories present: no Missing line."""
        plants = [
            {"name": f"p{i}", "category": c.value, "auto_added": False}
            for i, c in enumerate(PlantCategory)
        ]
        reply = _format_plants_reply(plants)
        self.assertNotIn("Missing:", reply)

    def test_empty_list_friendly_message(self):
        """Empty list returns the friendly no-plants message."""
        reply = _format_plants_reply([])
        self.assertEqual(reply, "No plants logged yet this week.")

    def test_names_are_escaped(self):
        """Markdown specials in plant names are escaped."""
        plants = [{"name": "star*fruit_x", "category": "fruit", "auto_added": False}]
        reply = _format_plants_reply(plants)
        self.assertIn("star\\*fruit\\_x", reply)


MOCK_UNDO_RESULT = {
    "deleted": True,
    "raw_user_string": "2 eggs and toast",
    "meal_type": "breakfast",
    "total_calories": 320,
    "total_protein_g": 18.0,
    "total_carbs_g": 25.0,
    "total_fat_g": 16.0,
    "total_fiber_g": 3.0,
}


class TestUndoFormatter(unittest.TestCase):

    def test_removed_line_names_meal_no_macros(self):
        """Reply names the removed meal and its type; macro numbers are gone."""
        reply = _format_undo_reply(MOCK_UNDO_RESULT, MOCK_CTX)
        self.assertIn("↩️ Removed: *2 eggs and toast* (breakfast)", reply)
        self.assertNotIn("kcal", reply)
        self.assertNotIn("P 18g", reply)

    def test_includes_fresh_pace_section(self):
        """Reply appends the refreshed weekly plant pace."""
        reply = _format_undo_reply(MOCK_UNDO_RESULT, MOCK_CTX)
        self.assertIn("4/30 plants this week", reply)
        self.assertNotIn("Today so far:", reply)

    def test_raw_string_is_escaped(self):
        """Markdown specials in raw_user_string are escaped."""
        result = dict(MOCK_UNDO_RESULT, raw_user_string="eggs_with*toast")
        reply = _format_undo_reply(result, MOCK_CTX)
        self.assertIn("eggs\\_with\\*toast", reply)


class TestWeightConfirmationFormatter(unittest.TestCase):

    def test_confirmation_has_weight_and_week(self):
        """Confirmation shows the recorded weight and week start."""
        reply = _format_weight_confirmation({"week_start": "2026-06-08", "weight_kg": 78.5})
        self.assertIn("78.5 kg", reply)
        self.assertIn("2026-06-08", reply)
        self.assertIn("⚖️", reply)

    def test_integer_weight_no_trailing_zero(self):
        """Whole-number weights render without a trailing .0."""
        reply = _format_weight_confirmation({"week_start": "2026-06-08", "weight_kg": 80.0})
        self.assertIn("80 kg", reply)
        self.assertNotIn("80.0 kg", reply)


class TestParseWeight(unittest.TestCase):

    def test_valid_weights(self):
        self.assertEqual(_parse_weight("78"), 78.0)
        self.assertEqual(_parse_weight(" 78.5 "), 78.5)
        self.assertEqual(_parse_weight("30"), 30.0)
        self.assertEqual(_parse_weight("300"), 300.0)

    def test_out_of_range_rejected(self):
        self.assertIsNone(_parse_weight("29.9"))
        self.assertIsNone(_parse_weight("301"))
        self.assertIsNone(_parse_weight("0"))

    def test_non_numeric_rejected(self):
        self.assertIsNone(_parse_weight("chicken and rice"))
        self.assertIsNone(_parse_weight("78kg"))
        self.assertIsNone(_parse_weight(""))


class TestPhotoFileId(unittest.TestCase):
    """_photo_file_id picks the largest Telegram photo size."""

    def test_picks_largest_size(self):
        """Telegram sends sizes ascending — last element is the largest."""
        message = {"photo": [
            {"file_id": "small", "width": 90},
            {"file_id": "medium", "width": 320},
            {"file_id": "large", "width": 1280},
        ]}
        self.assertEqual(_photo_file_id(message), "large")

    def test_no_photo_returns_none(self):
        self.assertIsNone(_photo_file_id({"text": "hello"}))

    def test_empty_photo_list_returns_none(self):
        self.assertIsNone(_photo_file_id({"photo": []}))


class TestPhotoRouting(unittest.TestCase):
    """Photo updates dispatch to _handle_photo_meal; other non-text types get a hint."""

    def _call(self, message: dict, profile: dict | None = None):
        event = {
            "body": json.dumps({"message": message}),
            "headers": {"x-telegram-bot-api-secret-token": "test-secret"},
        }
        sent = []
        with patch.dict("os.environ", {"WEBHOOK_SECRET": "test-secret", "TELEGRAM_TOKEN": "tok"}), \
             patch("handler.get_profile", return_value=dict(profile or MOCK_PROFILE)), \
             patch("handler._handle_photo_meal") as mock_photo, \
             patch("handler._send", side_effect=lambda cid, msg: sent.append(msg)):
            result = lambda_handler(event, None)
        return result, sent, mock_photo

    def test_photo_dispatches_with_caption(self):
        message = {
            "chat": {"id": 99999},
            "photo": [{"file_id": "s"}, {"file_id": "big"}],
            "caption": "my lunch",
        }
        _, _, mock_photo = self._call(message)
        mock_photo.assert_called_once_with(99999, "big", "my lunch")

    def test_photo_without_caption_dispatches_empty_caption(self):
        message = {"chat": {"id": 99999}, "photo": [{"file_id": "big"}]}
        _, _, mock_photo = self._call(message)
        mock_photo.assert_called_once_with(99999, "big", "")

    def test_sticker_gets_hint_reply(self):
        """Non-text, non-photo message: polite hint, no photo handler call."""
        message = {"chat": {"id": 99999}, "sticker": {"file_id": "x"}}
        _, sent, mock_photo = self._call(message)
        mock_photo.assert_not_called()
        self.assertTrue(any("text" in m.lower() and "photo" in m.lower() for m in sent))

    def test_photo_from_unknown_chat_rejected(self):
        """Allowlist applies to photos: unknown chat_id gets no dispatch, no reply."""
        message = {"chat": {"id": 12345}, "photo": [{"file_id": "big"}]}
        _, sent, mock_photo = self._call(message)
        mock_photo.assert_not_called()
        self.assertEqual(sent, [])


class TestHandlePhotoMeal(unittest.TestCase):
    """_handle_photo_meal downloads the photo and runs the meal pipeline."""

    def test_happy_path_logs_meal_and_replies(self):
        meal = _make_meal(MealType.lunch, plants=["broccoli"])
        with patch("handler._get_photo_b64", return_value="B64DATA") as mock_dl, \
             patch("handler.get_meals_logged_today", return_value=[]), \
             patch("handler.parse_meal_input", return_value=meal) as mock_parse, \
             patch("handler.insert_meal") as mock_insert, \
             patch("handler.get_daily_context", return_value=MOCK_CTX), \
             patch("handler._send") as mock_send:
            _handle_photo_meal(99999, "big", "my lunch")
        mock_dl.assert_called_once_with("big")
        self.assertEqual(mock_parse.call_args.kwargs.get("image_b64"), "B64DATA")
        mock_insert.assert_called_once_with(meal)
        mock_send.assert_called_once()
        self.assertIn("✅ broccoli", mock_send.call_args.args[1])

    def test_caption_passed_to_parser(self):
        meal = _make_meal(MealType.lunch, plants=["broccoli"])
        with patch("handler._get_photo_b64", return_value="B64DATA"), \
             patch("handler.get_meals_logged_today", return_value=[]), \
             patch("handler.parse_meal_input", return_value=meal) as mock_parse, \
             patch("handler.insert_meal"), \
             patch("handler.get_daily_context", return_value=MOCK_CTX), \
             patch("handler._send"):
            _handle_photo_meal(99999, "big", "half eaten only")
        self.assertIn("half eaten only", mock_parse.call_args.args[0])

    def test_pipeline_error_sends_apology(self):
        with patch("handler._get_photo_b64", side_effect=RuntimeError("boom")), \
             patch("handler._send") as mock_send:
            _handle_photo_meal(99999, "big", "")
        mock_send.assert_called_once()
        self.assertIn("wrong", mock_send.call_args.args[1].lower())

    def test_first_time_plants_never_blocks_reply(self):
        """RPC failure in flair lookup must not kill the logging pipeline."""
        meal = _make_meal(MealType.lunch, plants=["broccoli"])
        with patch("handler._get_photo_b64", return_value="B64DATA"), \
             patch("handler.get_meals_logged_today", return_value=[]), \
             patch("handler.parse_meal_input", return_value=meal), \
             patch("handler.insert_meal"), \
             patch("handler.get_today_first_time_plants", side_effect=RuntimeError("db down")), \
             patch("handler.get_daily_context", return_value=MOCK_CTX), \
             patch("handler._send") as mock_send:
            _handle_photo_meal(99999, "big", "lunch")
        sent = mock_send.call_args.args[1]
        self.assertIn("✅ broccoli", sent)
        self.assertNotIn("wrong", sent.lower())


class TestUpdateIdDedup(unittest.TestCase):
    """Telegram retries (same update_id) must not be processed twice."""

    def _call(self, update_id, stored_last):
        profile = dict(MOCK_PROFILE, last_update_id=stored_last)
        body = {"message": {"chat": {"id": 99999}, "text": "/today"}}
        if update_id is not None:
            body["update_id"] = update_id
        event = {
            "body": json.dumps(body),
            "headers": {"x-telegram-bot-api-secret-token": "test-secret"},
        }
        with patch.dict("os.environ", {"WEBHOOK_SECRET": "test-secret", "TELEGRAM_TOKEN": "tok"}), \
             patch("handler.get_profile", return_value=profile), \
             patch("handler.set_last_update_id") as mock_set, \
             patch("handler._handle_today") as mock_today, \
             patch("handler._send"):
            lambda_handler(event, None)
        return mock_set, mock_today

    def test_duplicate_update_dropped(self):
        mock_set, mock_today = self._call(update_id=100, stored_last=100)
        mock_today.assert_not_called()
        mock_set.assert_not_called()

    def test_older_update_dropped(self):
        mock_set, mock_today = self._call(update_id=99, stored_last=100)
        mock_today.assert_not_called()
        mock_set.assert_not_called()

    def test_new_update_processed_and_recorded(self):
        mock_set, mock_today = self._call(update_id=101, stored_last=100)
        mock_set.assert_called_once_with(101)
        mock_today.assert_called_once()

    def test_first_update_with_no_stored_value(self):
        mock_set, mock_today = self._call(update_id=50, stored_last=None)
        mock_set.assert_called_once_with(50)
        mock_today.assert_called_once()

    def test_missing_update_id_still_processed(self):
        mock_set, mock_today = self._call(update_id=None, stored_last=100)
        mock_set.assert_not_called()
        mock_today.assert_called_once()


class TestCalcTargets(unittest.TestCase):
    """Macro target formula — protein must use 2.0 g/kg bodyweight for 'lose' (recomp)."""

    def test_lose_goal_uses_bodyweight_protein(self):
        t = calc_targets(weight_kg=72, body_fat_pct=20, activity_level="active", goal_type="lose")
        self.assertEqual(t["target_protein_g"], 144)  # 72 × 2.0

    def test_maintain_goal_uses_lbm_protein(self):
        t = calc_targets(weight_kg=72, body_fat_pct=20, activity_level="active", goal_type="maintain")
        self.assertEqual(t["target_protein_g"], 104)  # 57.6 LBM × 1.8

    def test_tdee_and_calories(self):
        t = calc_targets(weight_kg=72, body_fat_pct=20, activity_level="active", goal_type="lose")
        # Katch-McArdle: (370 + 21.6 × 57.6) × 1.725 = 2784 ; lose = −400
        self.assertEqual(t["tdee"], 2784)
        self.assertEqual(t["target_calories"], 2384)

    def test_fat_and_fiber(self):
        t = calc_targets(weight_kg=72, body_fat_pct=20, activity_level="active", goal_type="lose")
        self.assertEqual(t["target_fat_g"], 65)   # 72 × 0.9
        self.assertEqual(t["target_fiber_g"], 30)


MOCK_INGREDIENTS = [
    {"name": "Laughing Cow original wedge", "unit_desc": "1 wedge (17.5g)",
     "calories": 35, "protein_g": 2.0, "carbs_g": 1.0, "fat_g": 2.7, "fiber_g": 0},
    {"name": "Go Good whey protein chocolate", "unit_desc": "1 serve (30g / 2 scoops)",
     "calories": 120, "protein_g": 23.4, "carbs_g": 1.5, "fat_g": 2.2, "fiber_g": 0.5},
]


class TestKnownIngredientsPromptBlock(unittest.TestCase):
    """_format_known_ingredients builds the exact-values prompt block."""

    def test_block_contains_names_units_and_values(self):
        block = _format_known_ingredients(MOCK_INGREDIENTS)
        self.assertIn("Laughing Cow original wedge", block)
        self.assertIn("1 wedge (17.5g)", block)
        self.assertIn("35", block)
        self.assertIn("23.4", block)

    def test_block_instructs_exact_use(self):
        block = _format_known_ingredients(MOCK_INGREDIENTS)
        self.assertIn("EXACT", block)

    def test_empty_list_returns_empty_string(self):
        self.assertEqual(_format_known_ingredients([]), "")
        self.assertEqual(_format_known_ingredients(None), "")


class TestMealLogPassesKnownIngredients(unittest.TestCase):
    """Both meal-log paths fetch personal ingredients and pass them to the parser."""

    def test_text_meal_passes_known_ingredients(self):
        meal = _make_meal(MealType.lunch, plants=["broccoli"])
        event = {
            "body": json.dumps({"message": {"chat": {"id": 99999}, "text": "a wedge of laughing cow"}}),
            "headers": {"x-telegram-bot-api-secret-token": "test-secret"},
        }
        with patch.dict("os.environ", {"WEBHOOK_SECRET": "test-secret", "TELEGRAM_TOKEN": "tok"}), \
             patch("handler.get_profile", return_value=dict(MOCK_PROFILE)), \
             patch("handler.get_personal_ingredients", return_value=MOCK_INGREDIENTS), \
             patch("handler.get_meals_logged_today", return_value=[]), \
             patch("handler.parse_meal_input", return_value=meal) as mock_parse, \
             patch("handler.insert_meal"), \
             patch("handler.get_daily_context", return_value=MOCK_CTX), \
             patch("handler._send"):
            lambda_handler(event, None)
        self.assertEqual(mock_parse.call_args.kwargs.get("known_ingredients"), MOCK_INGREDIENTS)

    def test_photo_meal_passes_known_ingredients(self):
        meal = _make_meal(MealType.lunch, plants=["broccoli"])
        with patch("handler._get_photo_b64", return_value="B64"), \
             patch("handler.get_personal_ingredients", return_value=MOCK_INGREDIENTS), \
             patch("handler.get_meals_logged_today", return_value=[]), \
             patch("handler.parse_meal_input", return_value=meal) as mock_parse, \
             patch("handler.insert_meal"), \
             patch("handler.get_daily_context", return_value=MOCK_CTX), \
             patch("handler._send"):
            _handle_photo_meal(99999, "fid", "")
        self.assertEqual(mock_parse.call_args.kwargs.get("known_ingredients"), MOCK_INGREDIENTS)

    def test_ingredient_fetch_failure_does_not_block_logging(self):
        """DB hiccup on the corrections table must not stop meal logging."""
        meal = _make_meal(MealType.lunch, plants=["broccoli"])
        with patch("handler._get_photo_b64", return_value="B64"), \
             patch("handler.get_personal_ingredients", side_effect=RuntimeError("db down")), \
             patch("handler.get_meals_logged_today", return_value=[]), \
             patch("handler.parse_meal_input", return_value=meal) as mock_parse, \
             patch("handler.insert_meal") as mock_insert, \
             patch("handler.get_daily_context", return_value=MOCK_CTX), \
             patch("handler._send"):
            _handle_photo_meal(99999, "fid", "")
        self.assertEqual(mock_parse.call_args.kwargs.get("known_ingredients"), [])
        mock_insert.assert_called_once()


class TestHelpCommand(unittest.TestCase):
    """/help lists every command with a description."""

    def test_help_text_lists_all_commands(self):
        text = _format_help()
        for cmd in ("/today", "/week", "/plants", "/undo", "/recipe", "/recipes", "/help"):
            self.assertIn(cmd, text)

    def test_help_mentions_photo_and_text_logging(self):
        text = _format_help()
        self.assertIn("photo", text.lower())

    def test_help_route_sends_help(self):
        event = {
            "body": json.dumps({"message": {"chat": {"id": 99999}, "text": "/help"}}),
            "headers": {"x-telegram-bot-api-secret-token": "test-secret"},
        }
        sent = []
        with patch.dict("os.environ", {"WEBHOOK_SECRET": "test-secret", "TELEGRAM_TOKEN": "tok"}), \
             patch("handler.get_profile", return_value=dict(MOCK_PROFILE)), \
             patch("handler._send", side_effect=lambda cid, msg: sent.append(msg)):
            lambda_handler(event, None)
        self.assertEqual(len(sent), 1)
        self.assertIn("/plants", sent[0])

    def test_unknown_slash_points_to_help(self):
        event = {
            "body": json.dumps({"message": {"chat": {"id": 99999}, "text": "/bogus"}}),
            "headers": {"x-telegram-bot-api-secret-token": "test-secret"},
        }
        sent = []
        with patch.dict("os.environ", {"WEBHOOK_SECRET": "test-secret", "TELEGRAM_TOKEN": "tok"}), \
             patch("handler.get_profile", return_value=dict(MOCK_PROFILE)), \
             patch("handler._send", side_effect=lambda cid, msg: sent.append(msg)):
            lambda_handler(event, None)
        self.assertTrue(any("/help" in m for m in sent))

    def test_help_reflects_journal_framing(self):
        text = _format_help()
        self.assertIn("today's meals", text)
        self.assertNotIn("macros", text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
