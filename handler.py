"""AWS Lambda entrypoint — Telegram webhook + pg_cron meal prompts.

Two entry paths:
  1. Telegram webhook POST  → parse event.body as Telegram update
  2. pg_cron meal_prompt    → event.body = {"type":"meal_prompt","meal_type":"X"}

Always returns HTTP 200.
"""

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from extract import parse_meal_input
from db import (
    insert_meal,
    get_daily_context,
    get_meals_logged_today,
    get_profile,
    update_onboarding_step,
    store_onboarding_field,
    complete_onboarding,
    get_chat_id,
    check_meal_logged_today,
    get_weekly_summary,
    use_recipe,
    list_recipes,
    get_gap_nudge,
    mark_gap_nudge_sent,
    undo_last_meal,
    get_week_plants,
    record_weekly_weight,
    set_awaiting_weight,
    set_last_update_id,
    calc_targets,
    get_personal_ingredients,
    get_today_first_time_plants,
    get_week_patterns,
)
from models import LoggedMeal, MealType, PlantCategory

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TELEGRAM_API = "https://api.telegram.org/bot"
TZ = ZoneInfo("Asia/Hong_Kong")

# ── Telegram helpers ──────────────────────────────────────────────────────────

def _md_escape(text: str) -> str:
    """Escape Telegram Markdown-v1 specials in dynamic values. Pure function — no I/O."""
    for ch in ("_", "*", "[", "`"):
        text = text.replace(ch, "\\" + ch)
    return text


def _send(chat_id: int, text: str) -> None:
    """Send a Markdown message; swallows errors so Lambda always returns 200.

    On HTTPError (e.g. malformed Markdown), retries once as plain text so the
    user always gets a message.
    """
    token = os.environ["TELEGRAM_TOKEN"]
    url = f"{TELEGRAM_API}{token}/sendMessage"
    for parse_mode in ("Markdown", None):
        body = {"chat_id": chat_id, "text": text}
        if parse_mode:
            body["parse_mode"] = parse_mode
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
            return
        except urllib.error.HTTPError as exc:
            logger.error(
                "Telegram sendMessage HTTP %s (parse_mode=%s): %s",
                exc.code, parse_mode, exc.read(),
            )


def _photo_file_id(message: dict) -> str | None:
    """file_id of the largest photo size, or None. Telegram orders sizes ascending. Pure function — no I/O."""
    photos = message.get("photo") or []
    return photos[-1]["file_id"] if photos else None


def _get_photo_b64(file_id: str) -> str:
    """Download a Telegram photo and return it base64-encoded (always JPEG)."""
    token = os.environ["TELEGRAM_TOKEN"]
    url = f"{TELEGRAM_API}{token}/getFile?file_id={urllib.parse.quote(file_id)}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        file_path = json.load(resp)["result"]["file_path"]
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=20) as resp:
        return base64.b64encode(resp.read()).decode()


def _known_ingredients() -> list:
    """Personal-ingredient corrections; never blocks logging on failure."""
    try:
        return get_personal_ingredients()
    except Exception:
        logger.exception("personal_ingredients fetch failed — parsing without them")
        return []


def _first_time_plants(meal: LoggedMeal) -> list[str]:
    """First-ever plants in this meal — flair only, never blocks the reply.

    Canonical names are matched to the LLM's plant_name case-insensitively;
    fuzzy-resolution mismatches just mean no flair (best-effort by design).
    """
    if not meal.plants_detected:
        return []
    try:
        today_firsts = {n.lower() for n in get_today_first_time_plants()}
    except Exception:
        logger.exception("first-time plant fetch failed — skipping flair")
        return []
    return [p.plant_name for p in meal.plants_detected
            if p.plant_name.lower() in today_firsts]


def _week_patterns_safe() -> dict:
    """Weekly patterns; check-in must render even if the RPC fails."""
    try:
        return get_week_patterns()
    except Exception:
        logger.exception("week patterns fetch failed — check-in without them")
        return {}


def _handle_photo_meal(chat_id: int, file_id: str, caption: str) -> None:
    """Download a meal photo and run it through the normal logging pipeline."""
    try:
        image_b64 = _get_photo_b64(file_id)
        caption   = caption.strip()
        user_text = f"📷 {caption}" if caption else "📷 photo meal"
        meal = parse_meal_input(
            user_text,
            hkt_time=datetime.now(TZ).strftime("%H:%M"),
            meals_logged=get_meals_logged_today(),
            image_b64=image_b64,
            known_ingredients=_known_ingredients(),
        )
        if not meal.items:
            _send(chat_id, "Sorry, I couldn't find food in that photo — try a clearer shot or describe the meal in text.")
            return
        insert_meal(meal)
        _send(chat_id, _format_reply(meal, get_daily_context(), _first_time_plants(meal)))
    except Exception:
        logger.exception("Photo pipeline error for chat %s", chat_id)
        _send(chat_id, "⚠️ Sorry, something went wrong with that photo. Please try again.")


# ── Onboarding ────────────────────────────────────────────────────────────────

ONBOARDING_PROMPTS = {
    1: "👋 Let's set up your personal targets!\n\nWhat's your current *weight in kg*? (e.g. `78`)",
    2: "Got it! What's your *body fat %*?\n_(Reply `skip` to use 20% as default)_",
    3: "Almost there! What's your *activity level*?\nReply: `sedentary` / `light` / `moderate` / `active`",
    4: "Last one — what's your *goal*?\nReply: `maintain` / `lose` / `gain`",
}

ACTIVITY_VALID = {"sedentary", "light", "moderate", "active"}
GOAL_VALID = {"maintain", "lose", "gain"}


def _handle_onboarding(chat_id: int, text: str, step: int, profile: dict) -> None:
    """Drive the onboarding state machine one step forward."""
    text = text.strip().lower()

    if step == 0:
        # First contact — start onboarding
        update_onboarding_step(chat_id, 1)
        _send(chat_id, ONBOARDING_PROMPTS[1])
        return

    if step == 1:
        try:
            weight = float(text)
            assert 30 < weight < 300
        except (ValueError, AssertionError):
            _send(chat_id, "Please enter a valid weight in kg, e.g. `78`")
            return
        store_onboarding_field("weight_kg", weight)
        update_onboarding_step(chat_id, 2)
        _send(chat_id, ONBOARDING_PROMPTS[2])

    elif step == 2:
        if text == "skip":
            bf = 20.0
        else:
            try:
                bf = float(text.replace("%", ""))
                assert 3 < bf < 60
            except (ValueError, AssertionError):
                _send(chat_id, "Please enter body fat % (e.g. `18`) or reply `skip`")
                return
        store_onboarding_field("body_fat_pct", bf)
        update_onboarding_step(chat_id, 3)
        _send(chat_id, ONBOARDING_PROMPTS[3])

    elif step == 3:
        if text not in ACTIVITY_VALID:
            _send(chat_id, "Please reply: `sedentary` / `light` / `moderate` / `active`")
            return
        store_onboarding_field("activity_level", text)
        update_onboarding_step(chat_id, 4)
        _send(chat_id, ONBOARDING_PROMPTS[4])

    elif step == 4:
        if text not in GOAL_VALID:
            _send(chat_id, "Please reply: `maintain` / `lose` / `gain`")
            return
        store_onboarding_field("goal_type", text)
        # All data collected — calculate and store targets
        p = get_profile()
        weight = p["weight_kg"]
        bf     = p["body_fat_pct"]
        act    = p["activity_level"]
        complete_onboarding(weight, bf, act, text, chat_id)
        t = calc_targets(weight, bf, act, text)
        _send(chat_id, (
            f"✅ *Targets set!*\n\n"
            f"TDEE: {t['tdee']} kcal → Goal target: *{t['target_calories']} kcal/day*\n"
            f"Protein: *{t['target_protein_g']}g* · Fat: *{t['target_fat_g']}g* · "
            f"Carbs: *{t['target_carbs_g']}g* · Fiber: {t['target_fiber_g']}g\n\n"
            f"Start logging meals — just describe what you ate!"
        ))


# ── Meal prompt (from pg_cron) ────────────────────────────────────────────────

MEAL_PROMPTS = {
    "breakfast": "🍳 Good morning! What did you have for breakfast?",
    "lunch":     "🥗 Lunchtime! What did you eat?",
    "dinner":    "🍽️ Good evening! What did you have for dinner?",
}


def _handle_meal_prompt(meal_type: str) -> None:
    """Send a proactive meal prompt to the user's Telegram chat."""
    chat_id = get_chat_id()
    if not chat_id:
        logger.warning("No chat_id in user_profiles — onboarding not done")
        return

    if check_meal_logged_today(meal_type):
        ctx = get_daily_context()
        weekly = ctx.get("weekly_plants", 0)
        _send(chat_id, f"Already logged — nice work! 🌿 {weekly}/30 plants this week")
        return

    _send(chat_id, MEAL_PROMPTS.get(meal_type, "Time to log a meal!"))


def _handle_weekly_checkin() -> None:
    """Fetch weekly summary and send it to the user's Telegram chat."""
    chat_id = get_chat_id()
    if not chat_id:
        logger.warning("weekly_checkin: no chat_id in user_profiles — onboarding not done")
        return
    ctx   = get_weekly_summary()
    reply = _format_weekly_checkin(ctx, _week_patterns_safe())
    reply += "\n\n⚖️ Reply with your current weight in kg to log it (or ignore)."
    _send(chat_id, reply)
    set_awaiting_weight(True)


def _handle_gap_nudge() -> None:
    """Send proactive gap nudge if streak detected."""
    chat_id = get_chat_id()
    if not chat_id:
        logger.warning("gap_nudge: no chat_id in user_profiles — onboarding not done")
        return
    result = get_gap_nudge()
    if not result:
        return
    _send(chat_id, _format_gap_nudge(result))
    mark_gap_nudge_sent()


def _meal_type_from_time(hour: int) -> str:
    """Map HKT hour to meal type. Recipes are never 'extra' — outside windows fall back to dinner."""
    if 5 <= hour < 11:
        return "breakfast"
    if 11 <= hour < 17:
        return "lunch"
    return "dinner"


def _handle_recipe(chat_id: int, name: str, raw_text: str) -> None:
    """Fuzzy-match a saved recipe template and log it as a real meal."""
    meal_type = _meal_type_from_time(datetime.now(TZ).hour)
    result = use_recipe(name, raw_text, meal_type)
    if not result.get("matched_name"):
        _send(chat_id, f"No recipe found matching '{_md_escape(name)}'.")
        return
    ctx   = get_daily_context()
    reply = _format_recipe_reply(result, ctx)
    _send(chat_id, reply)


def _handle_list_recipes(chat_id: int) -> None:
    """Send the list of saved recipe templates."""
    recipes = list_recipes()
    _send(chat_id, _format_list_recipes(recipes))


def _handle_undo(chat_id: int) -> None:
    """Undo the most recently logged meal and show updated daily totals."""
    result = undo_last_meal()
    if not result.get("deleted"):
        _send(chat_id, "Nothing to undo.")
        return
    ctx = get_daily_context()
    _send(chat_id, _format_undo_reply(result, ctx))


def _handle_today(chat_id: int) -> None:
    """Send today's journal snapshot + weekly plant pace on demand."""
    _send(chat_id, _format_today_reply(get_daily_context(), get_meals_logged_today()))


# ── Reply formatting ──────────────────────────────────────────────────────────

def _greeting(meal_type: MealType) -> str:
    if meal_type == MealType.breakfast:
        return "Good morning! 🍳 Breakfast logged."
    if meal_type == MealType.lunch:
        return "Good afternoon! 🥗 Lunch logged."
    if meal_type == MealType.dinner:
        return "Good evening! 🍽️ Dinner logged."
    return "Meal logged. 🍽️"


def _pace_line(weekly_plants: int, day_of_week: int) -> str:
    if weekly_plants >= 30:
        return "📅 30-plant goal hit this week! 🎉"
    days_done = day_of_week
    days_left = 7 - days_done
    rate = weekly_plants / days_done if days_done else 0
    if rate >= 4.3:
        return f"📅 Day {days_done} of 7 — on track"
    needed = 30 - weekly_plants
    if days_left == 0:
        return f"📅 Day 7 of 7 — {needed} plants short this week"
    return f"📅 Day {days_done} of 7 — need {needed} more plants over {days_left} days to hit 30"


MEAL_TYPE_EMOJI = {
    "breakfast": "🍳",
    "lunch":     "🥗",
    "dinner":    "🍽️",
    "extra":     "🍽️",
}


def _format_plants_pace_section(ctx: dict) -> str:
    """Weekly plant count + pace block. Pure function — no I/O."""
    weekly = ctx["weekly_plants"]
    pace   = _pace_line(weekly, ctx["day_of_week"])
    return f"🌿 {weekly}/30 plants this week\n{pace}"


def _format_today_reply(ctx: dict, meals_logged: list[str]) -> str:
    """Reply for /today — journal snapshot, no macros. Pure function — no I/O."""
    logged_line = ", ".join(meals_logged) if meals_logged else "none yet"
    plants_today = ctx.get("day_unique_plants", 0)
    return (
        f"*Today:*\n"
        f"Main meals logged: {logged_line}\n"
        f"🌿 {plants_today} plants today\n\n"
        f"{_format_plants_pace_section(ctx)}"
    )


def _format_help() -> str:
    """Command reference for /help and unknown commands. Pure function — no I/O."""
    return (
        "🤖 *Commands:*\n"
        "/today — today's meals + weekly plant pace\n"
        "/week — week in review (plants + consistency)\n"
        "/plants — this week's plants by category\n"
        "/undo — delete the last logged meal\n"
        "/recipe <name> — log a saved recipe\n"
        "/recipes — list saved recipes\n"
        "/help — this message\n\n"
        "*Logging:*\n"
        "Send a meal description (e.g. '2 eggs and toast') or a photo of your "
        "plate — captions help with portions and brands.\n"
        "A bare number right after the Sunday check-in logs your weight."
    )


def _format_undo_reply(result: dict, ctx: dict) -> str:
    """Reply for a successful /undo. Pure function — no I/O."""
    raw       = _md_escape(result.get("raw_user_string") or "")
    meal_type = result.get("meal_type", "meal")
    return (
        f"↩️ Removed: *{raw}* ({meal_type})\n\n"
        f"{_format_plants_pace_section(ctx)}"
    )


def _format_plants_reply(plants: list) -> str:
    """Reply for /plants — grouped by category. Pure function — no I/O."""
    if not plants:
        return "No plants logged yet this week."

    # Input is ordered by category, name — dict preserves that order.
    by_cat: dict[str, list[str]] = {}
    for p in plants:
        name = _md_escape(p["name"])
        if p.get("auto_added"):
            name += " ⚠️"
        by_cat.setdefault(p["category"], []).append(name)

    lines = [f"🌿 *{len(plants)}/30 plants this week*\n"]
    for cat, names in by_cat.items():
        lines.append(f"{cat}: {', '.join(names)}")

    missing = [c.value for c in PlantCategory if c.value not in by_cat]
    if missing:
        lines.append(f"\nMissing: {', '.join(missing)}")
    return "\n".join(lines)


def _format_weight_confirmation(result: dict) -> str:
    """Confirmation for a recorded weekly weight. Pure function — no I/O."""
    weight = float(result["weight_kg"])
    week   = result["week_start"]
    return f"⚖️ Logged *{weight:g} kg* for the week starting {week}."


def _parse_weight(text: str) -> float | None:
    """Parse a bare number 30–300 as kg, else None. Pure function — no I/O."""
    try:
        weight = float(text.strip())
    except ValueError:
        return None
    return weight if 30 <= weight <= 300 else None


def _format_recipe_reply(recipe: dict, ctx: dict) -> str:
    """3-section reply for a recipe log. Pure function — no I/O."""
    name   = recipe["matched_name"]
    m_cal  = recipe["total_calories"]
    m_pro  = float(recipe["total_protein_g"])
    m_carb = float(recipe["total_carbs_g"])
    m_fat  = float(recipe["total_fat_g"])
    m_fib  = float(recipe["total_fiber_g"])
    plants = recipe.get("plants") or []

    if plants:
        plant_line = "🌿 " + ", ".join(_md_escape(p) for p in plants)
    else:
        plant_line = "No plants in this recipe."

    section1 = (
        f"*This meal:*\n"
        f"Calories: {m_cal} kcal · Protein: {m_pro:.0f}g · "
        f"Carbs: {m_carb:.0f}g · Fat: {m_fat:.0f}g · Fiber: {m_fib:.0f}g\n"
        f"{plant_line}"
    )

    return (
        f"Recipe logged: *{_md_escape(name)}* 🍽️\n\n"
        f"{section1}\n\n"
        f"{_format_plants_pace_section(ctx)}"
    )


def _format_list_recipes(recipes: list) -> str:
    """Format saved recipe list. Pure function — no I/O."""
    if not recipes:
        return "No recipes saved yet."
    lines = ["📋 *Saved recipes:*\n"]
    for r in recipes:
        emoji = MEAL_TYPE_EMOJI.get(r["meal_type"], "🍽️")
        lines.append(
            f"{emoji} {_md_escape(r['name'])} — {r['calories']} kcal · "
            f"P: {r['protein_g']:.0f}g · C: {r['carbs_g']:.0f}g · "
            f"F: {r['fat_g']:.0f}g · Fiber: {r['fiber_g']:.0f}g"
        )
    return "\n".join(lines)


def _format_gap_nudge(result: dict) -> str:
    """Proactive gap nudge message. Pure function — no I/O."""
    lines = ["⚠️ *Nutrition heads-up*\n"]
    if result.get("fiber_gap"):
        lines.append("Low fiber 3 days running — try lentils, oats, or avocado.")
    if result.get("protein_gap"):
        lines.append("Low protein 3 days running — try eggs, Greek yogurt, or chicken.")
    return "\n".join(lines)


def _format_weekly_checkin(ctx: dict, patterns: dict | None = None) -> str:
    """Weekly check-in — plants, consistency, patterns. Pure function — no I/O.

    Macro averages intentionally dropped: LLM per-meal estimates are too
    noisy to report against targets. Recipes remain the accurate-macro path.
    """
    days   = ctx.get("days_logged", 0)
    plants = ctx.get("weekly_plants", 0)

    if days == 0:
        return (
            "📊 *Week in review*\n\n"
            "No meals logged this week — fresh start Monday! 💪\n"
            f"🌿 Plants: {plants}/30"
        )

    p = patterns or {}
    lines = [
        "📊 *Week in review*\n",
        f"🌿 Plants: {plants}/30 this week",
    ]

    new_plants = p.get("new_plants") or []
    if new_plants:
        lines.append("✨ New this week: " + ", ".join(_md_escape(n) for n in new_plants))

    streak   = p.get("streak_days") or 0
    day_line = f"📅 Logged {days} of 7 days"
    if streak > 7:
        day_line += f" · {streak}-day streak"
    lines.append(day_line)

    top_meals = p.get("top_meals") or []
    if top_meals:
        tops = ", ".join(f"{_md_escape(m['name'])} (×{m['count']})" for m in top_meals)
        lines.append(f"🍽️ Repeats: {tops}")

    return "\n".join(lines)


def _format_reply(meal: LoggedMeal, ctx: dict, first_time_plants: list[str] | None = None) -> str:
    """Journal-first reply for logged meals. Pure function — no I/O.

    Macros are intentionally omitted: per-meal LLM estimates are too noisy
    to display. Items are echoed back so the user can verify the parse.
    """
    items_line = "✅ " + " · ".join(_md_escape(i.food_name) for i in meal.items)

    if meal.plants_detected:
        plant_line = "🌿 Plants: " + ", ".join(
            _md_escape(p.plant_name) for p in meal.plants_detected
        )
    else:
        plant_line = "No plants this meal — try adding greens or legumes next time!"

    flair = ""
    if first_time_plants:
        names = ", ".join(_md_escape(n) for n in first_time_plants)
        flair = f"\n✨ First time logged: {names}!"

    return (
        f"{_greeting(meal.meal_type)}\n\n"
        f"{items_line}\n"
        f"{plant_line}{flair}\n\n"
        f"{_format_plants_pace_section(ctx)}"
    )



# ── Lambda entrypoint ─────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """Always returns 200 to prevent Telegram retries."""
    chat_id = None
    try:
        raw_body = event.get("body") or "{}"
        body     = json.loads(raw_body)
        headers  = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

        # pg_cron meal_prompt path — verify shared secret
        if body.get("type") == "meal_prompt":
            cron_secret = os.environ.get("CRON_SECRET", "")
            if not cron_secret or body.get("secret") != cron_secret:
                logger.warning("meal_prompt rejected: bad or missing secret")
                return {"statusCode": 200, "body": "ok"}
            _handle_meal_prompt(body.get("meal_type", ""))
            return {"statusCode": 200, "body": "ok"}

        # weekly_checkin path — verify shared secret (same pattern as meal_prompt)
        if body.get("type") == "weekly_checkin":
            cron_secret = os.environ.get("CRON_SECRET", "")
            if not cron_secret or body.get("secret") != cron_secret:
                logger.warning("weekly_checkin rejected: bad or missing secret")
                return {"statusCode": 200, "body": "ok"}
            _handle_weekly_checkin()
            return {"statusCode": 200, "body": "ok"}

        if body.get("type") == "gap_nudge":
            cron_secret = os.environ.get("CRON_SECRET", "")
            if not cron_secret or body.get("secret") != cron_secret:
                logger.warning("gap_nudge rejected: bad or missing secret")
                return {"statusCode": 200, "body": "ok"}
            _handle_gap_nudge()
            return {"statusCode": 200, "body": "ok"}

        # Telegram webhook path — verify secret token header (fail closed if unset)
        webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
        if not webhook_secret or headers.get("x-telegram-bot-api-secret-token") != webhook_secret:
            logger.warning("Telegram webhook rejected: bad or missing secret token")
            return {"statusCode": 200, "body": "ok"}

        message  = body.get("message", {})
        chat_id  = message.get("chat", {}).get("id")
        text     = (message.get("text") or "").strip()
        photo_id = _photo_file_id(message)

        if not chat_id:
            return {"statusCode": 200, "body": "ok"}

        # Check onboarding state BEFORE command dispatch so the post-onboarding
        # allowlist applies to slash commands and photos too.
        profile = get_profile()
        step    = profile.get("onboarding_step", 0) if profile else 0

        # After onboarding, reject messages from unknown chat IDs
        if step >= 5:
            stored_chat_id = profile.get("telegram_chat_id") if profile else None
            if stored_chat_id and int(chat_id) != int(stored_chat_id):
                logger.warning("Rejected message from unknown chat_id %s", chat_id)
                return {"statusCode": 200, "body": "ok"}

        # Drop Telegram webhook retries (same or older update_id). Recorded
        # before processing — at-most-once beats duplicate meal inserts.
        update_id = body.get("update_id")
        if update_id is not None and profile is not None:
            last = profile.get("last_update_id")
            if last is not None and int(update_id) <= int(last):
                logger.info("Duplicate update_id %s dropped", update_id)
                return {"statusCode": 200, "body": "ok"}
            set_last_update_id(int(update_id))

        # ── Photo meal log ──
        if photo_id and not text:
            if step < 5:
                _send(chat_id, "Let's finish your setup first — reply to the questions above!")
                return {"statusCode": 200, "body": "ok"}
            _handle_photo_meal(chat_id, photo_id, message.get("caption") or "")
            return {"statusCode": 200, "body": "ok"}

        # Unsupported message types (stickers, voice, etc.)
        if not text:
            _send(chat_id, "I can only read text or photos for now — describe your meal or send a picture of it.")
            return {"statusCode": 200, "body": "ok"}

        # ── Slash commands ──
        if text == "/recipes":
            _handle_list_recipes(chat_id)
            return {"statusCode": 200, "body": "ok"}

        if text == "/recipe" or text.startswith("/recipe "):
            name = text[len("/recipe"):].strip()
            if not name:
                _send(chat_id, "Usage: /recipe <name>  e.g. /recipe avocado toast")
                return {"statusCode": 200, "body": "ok"}
            _handle_recipe(chat_id, name, text)
            return {"statusCode": 200, "body": "ok"}

        if text == "/undo":
            _handle_undo(chat_id)
            return {"statusCode": 200, "body": "ok"}

        if text == "/today":
            _handle_today(chat_id)
            return {"statusCode": 200, "body": "ok"}

        if text == "/week":
            _send(chat_id, _format_weekly_checkin(get_weekly_summary(), _week_patterns_safe()))
            return {"statusCode": 200, "body": "ok"}

        if text == "/plants":
            _send(chat_id, _format_plants_reply(get_week_plants()))
            return {"statusCode": 200, "body": "ok"}

        # /help and any unknown command both get the command reference
        if text.startswith("/"):
            _send(chat_id, _format_help())
            return {"statusCode": 200, "body": "ok"}

        if step < 5:
            _handle_onboarding(chat_id, text, step, profile or {})
            return {"statusCode": 200, "body": "ok"}

        # Sunday weight capture — a bare number after the weekly check-in logs weight
        if profile and profile.get("awaiting_weight"):
            weight = _parse_weight(text)
            if weight is not None:
                result = record_weekly_weight(weight)
                set_awaiting_weight(False)
                _send(chat_id, _format_weight_confirmation(result))
                return {"statusCode": 200, "body": "ok"}
            # Not a bare number — clear the flag and treat as a normal meal log
            set_awaiting_weight(False)

        # Fully onboarded — log meal
        try:
            now_hkt      = datetime.now(TZ)
            hkt_time     = now_hkt.strftime("%H:%M")
            meals_logged = get_meals_logged_today()
            meal         = parse_meal_input(
                text, hkt_time=hkt_time, meals_logged=meals_logged,
                known_ingredients=_known_ingredients(),
            )
            if not meal.items or not meal.raw_user_string.strip():
                reply = "Sorry, I couldn't understand that — could you describe what you ate?\ne.g. '1 egg, 1 slice toast, half avocado'"
                _send(chat_id, reply)
                return {"statusCode": 200, "body": "ok"}
            insert_meal(meal)
            ctx          = get_daily_context()
            reply        = _format_reply(meal, ctx, _first_time_plants(meal))
        except Exception as exc:
            logger.exception("Pipeline error for chat %s", chat_id)
            reply = "⚠️ Sorry, something went wrong. Please try again."

        _send(chat_id, reply)

    except Exception:
        logger.exception("Unhandled error in lambda_handler")
        try:
            if chat_id:
                _send(chat_id, "⚠️ Something went wrong on my end. Please try again.")
        except Exception:
            pass

    return {"statusCode": 200, "body": "ok"}
