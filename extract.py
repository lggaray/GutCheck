"""Meal parsing via OpenRouter structured outputs."""

import os
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # Lambda: env vars set via function configuration

from openai import OpenAI
from pydantic import ValidationError

from models import LoggedMeal, compute_totals

# Vision-capable model with structured-output support (photo input planned).
# Google models are region-blocked for this OpenRouter account (HK ToS).
# Override via LLM_MODEL env var without redeploying code changes.
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen/qwen3-vl-235b-a22b-instruct")

SYSTEM_PROMPT = """You are a rigorous nutrition scientist parsing meal descriptions.

Portion rules:
- Every message describes ONE single-person meal — the user's own plate — unless stated otherwise.
- Multiple foods listed together ("rice + shrimp with corn + scrambled eggs") are components
  sharing that one plate: scale each to its share of a normal single serving.
  Never assume one full portion of each component.
- Explicit quantities ("3 cookies", "150g beef", "half avocado") always override these defaults.
- If a photo is provided, identify the foods and estimate portions from what is visible
  (plate size, food volume, piece counts); the text is added context, not a separate meal.

Nutrition rules:
- Break the meal into discrete, individually-weighable ingredients.
- Estimate realistic raw quantities and per-item macros (calories, protein_g, carbs_g, fat_g, fiber_g).
- Apply fraction_eaten for partial portions ("a bit of" → 0.3, "half" → 0.5).
- Flag whole plant foods (is_plant=true) and assign the correct plant_category.
- Do NOT count processed/derived items as plants (flour, oil, sugar, cheese, butter).
- plants_detected must exactly mirror the plant-flagged items (same plant_name values, no extras, no omissions).
- total_* fields: best-effort sums of item macros × fraction_eaten (recomputed server-side).
- Use realistic USDA-reference macro values per ingredient weight.

Meal type classification (a [Context] block may be provided with current time and meals already logged today):
- breakfast: message logged 05:00–11:00 HKT and no breakfast yet; or clearly describes a morning meal
- lunch:     message logged 11:00–17:00 HKT and no lunch yet; or clearly describes a midday meal
- dinner:    message logged after 17:00 HKT and no dinner yet; or clearly describes an evening meal
- extra:     everything else — small/incidental items, snacks, items outside main meal windows,
             or any item logged when the matching main-meal type was already logged today
             (e.g. "just had a banana", "handful of nuts", "grabbed a coffee and cookie")
Only four meal types exist: breakfast, lunch, dinner, extra. There is no 'snack' type.
When in doubt, use extra."""

def _format_known_ingredients(rows: list | None) -> str:
    """Prompt block listing personal-ingredient label values. Pure function — no I/O."""
    if not rows:
        return ""
    lines = [
        "\n\nKnown ingredients (user-verified label data) — when one of these appears,"
        "\nuse these EXACT per-unit values scaled by quantity, never estimate:"
    ]
    for r in rows:
        lines.append(
            f"- {r['name']} [{r['unit_desc']}]: {r['calories']} kcal, "
            f"protein {r['protein_g']}g, carbs {r['carbs_g']}g, "
            f"fat {r['fat_g']}g, fiber {r['fiber_g']}g"
        )
    return "\n".join(lines)


TEST_CASES = [
    "grilled chicken with broccoli and rice",
    "I had a bit of lentil soup",
    "stir fry with snap peas, bok choy, shiitake mushrooms",
    "pad thai from the place downstairs",
    "cheese pizza",
]

# Module-level client — created once per Lambda container, reused across invocations.
_client = None


def _get_client() -> OpenAI:
    """Return the shared OpenAI client, building it lazily on first use."""
    global _client
    if _client is None:
        _client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
        )
    return _client


def parse_meal_input(
    user_text: str,
    hkt_time: str | None = None,
    meals_logged: list[str] | None = None,
    image_b64: str | None = None,
    known_ingredients: list | None = None,
) -> LoggedMeal:
    """Parse a freeform meal description into a validated LoggedMeal.

    Uses OpenRouter structured outputs so the model populates the schema directly.
    Raises ValidationError if the model output violates the schema constraints.

    hkt_time: current time in HKT as "HH:MM", used for meal-type classification.
    meals_logged: list of main meal types already logged today (excludes 'extra').
    image_b64: optional base64 JPEG of the meal photo — sent as a multimodal part.
    known_ingredients: personal_ingredients rows — label-exact values appended to
        the system prompt so the model uses them instead of estimating.
    """
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise EnvironmentError("OPENROUTER_API_KEY not set")

    client = _get_client()

    system_prompt = SYSTEM_PROMPT + _format_known_ingredients(known_ingredients)

    if hkt_time is not None or meals_logged is not None:
        ctx_lines = []
        if hkt_time:
            ctx_lines.append(f"Current time (HKT): {hkt_time}")
        if meals_logged is not None:
            logged_str = ", ".join(meals_logged) if meals_logged else "none"
            ctx_lines.append(f"Main meals logged today so far: {logged_str}")
        user_text_full = "[Context]\n" + "\n".join(ctx_lines) + "\n\n[Meal]\n" + user_text
    else:
        user_text_full = user_text

    if image_b64:
        user_content = [
            {"type": "text", "text": user_text_full},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]
    else:
        user_content = user_text_full

    # Retry up to 4 times on transient errors.
    # Budget: 4 × 20s timeout + 2s + 4s + 8s backoff ≈ 94s worst case.
    for attempt in range(4):
        try:
            response = client.beta.chat.completions.parse(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format=LoggedMeal,
                timeout=20,
            )
            break
        except Exception as exc:
            if attempt == 3:
                raise
            wait = 2 ** (attempt + 1)
            print(f"  [retry {attempt + 1}/3 after {wait}s: {exc}]")
            time.sleep(wait)

    result = response.choices[0].message.parsed
    # Inject raw_user_string (prompt hygiene) and overwrite the LLM's advisory
    # total_* fields with server-computed sums (see models.compute_totals).
    return result.model_copy(
        update={"raw_user_string": user_text, **compute_totals(result.items)}
    )


if __name__ == "__main__":
    import sys
    for text in TEST_CASES:
        print(f"\n{'=' * 60}")
        print(f"INPUT: {text}")
        print("=" * 60)
        try:
            meal = parse_meal_input(text)
            print(meal.model_dump_json(indent=2))
        except ValidationError as exc:
            print(f"[ValidationError] {exc}")
        except Exception as exc:
            print(f"[Error] {type(exc).__name__}: {exc}")
    sys.exit(0)
