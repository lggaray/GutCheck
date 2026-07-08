# GutCheck
A zero-friction, single-user nutrition pipeline powered by Telegram, OpenRouter, and Supabase.
Log meals by text or photo, track your weekly plant diversity, and see your eating patterns —
all from your phone.

Journal-first (Jul 2026): replies show what you ate, which plants, and consistency over time.
Macros are still recorded on every meal but not surfaced per-meal — per-item LLM estimates were
too noisy to trust to the gram, so display leans on what's actually reliable.

---

## Why I Built This

Inspired by Jeremy Ethier's **Built With Science+ (BWS+)** app, which proved that the secret
to real nutrition results isn't a restrictive diet. It's an easy, accurate way to stay
accountable to your targets.

The commercial app is great, but it's built for a general audience. This is built for one
person, with one set of goals, and zero subscription fees beyond what I already pay for.

**Personal goals this system is designed to hit:**

- 🌿 **30 unique plants per week** — the gut microbiome diversity benchmark from the 2018
  American Gut Project. Variety matters as much as quantity.
- 📊 **Calorie + macro awareness** — not obsessive tracking, but enough signal to know when
  I'm consistently under on protein or over on simple carbs.
- 🔍 **Nutritional gap identification** — proactive nudges when I've been missing greens,
  fiber, legumes, etc. for several days.
- 🍳 **Recipe memory** — log "I made that lentil soup again" and have the system know exactly
  what that means nutritionally.
- 📈 **Adaptive TDEE coaching** — weekly weight check-ins that adjust calorie targets based on
  real trend data, not a static formula.

---

## MVP Scope

The MVP is deliberately narrow. It does one thing well before growing.

**Shipped:**
- Text and photo meal logging via Telegram
- Per-meal macro/calorie estimation, recorded but not displayed (journal-first, Jul 2026)
- Running weekly plant count with progress toward 30, new-plant/repeat/streak insights
- On-demand commands: `/undo` `/today` `/week` `/plants` `/recipe` `/recipes` `/help`
- Sunday Telegram check-in: weekly summary, weight capture
- Recipe template logging ("I made that lentil soup again")
- `personal_ingredients` corrections for foods the LLM/fuzzy-match estimates badly

**Still out of scope:**
- Adaptive TDEE coaching (needs more weeks of weight trend data)
- Smart scale integration (hardware)
- Any web UI or dashboard

---

## Architecture

```
[ Telegram App ]
      |
      | POST (webhook)
      v
[ AWS Lambda (Python) ]
      |
      +---> [ OpenRouter LLM API ]  # structured meal parsing
      |           |
      |           | LoggedMeal (Pydantic)
      |           v
      +---> [ Supabase (PostgreSQL) ]  # insert meals + items
                  |
                  | AFTER trigger (automatic)
                  v
            [ daily_summaries ]   # precomputed aggregations
```

**Design principles:**
- The Lambda is dumb and stateless. Its only job: text → JSON → INSERT.
- All aggregation happens in the database via triggers. Never in app code.
- The LLM never generates raw SQL. It populates typed Pydantic schemas only.
- `is_plant` flagging and `canonical_plant_id` resolution happen at ingest time
  via `pg_trgm` fuzzy matching against the `canonical_plants` reference table.

---

## Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Interface | Telegram Bot | Text and photo (vision model) |
| Compute | AWS Lambda (Python 3.14) | Stateless, 128MB |
| LLM | OpenRouter (`qwen/qwen3-vl-235b-a22b-instruct`) | Paid, vision-capable. Via OpenAI SDK, structured outputs. Overridable via `LLM_MODEL` |
| Database | Supabase (PostgreSQL) | pg_trgm, pg_cron, triggers, RLS deny-all |

---

## Data Model

```
user_profiles (1)
    └── meals (N)  [is_template_recipe flag doubles as recipe store]
            └── meal_items (N)
                    └── canonical_plants (0..1 FK)

weekly_check_ins    — standalone, weight log for adaptive TDEE
daily_summaries     — precomputed cache, maintained by DB trigger only
```

**Key decisions:**
- No `meal_plants_junction` table. `meal_items.canonical_plant_id` is the single
  source of truth. Weekly count = `COUNT(DISTINCT canonical_plant_id)` via JOIN.
- Recipes are not a separate table. A recipe is a `meals` row with
  `is_template_recipe = true` and `logged_at = NULL`.
- `daily_summaries` is never written by the Lambda. A PostgreSQL AFTER trigger
  on `meals` and `meal_items` recomputes it automatically.
- All analytical queries use the `logged_meals` VIEW (filters out templates).

---

## Pydantic Contract (Source of Truth)

The LLM must populate a `LoggedMeal` object. This schema is the contract between
the prompt and the database. It lives in `models.py`.

```
LoggedMeal
  ├── meal_type: MealType (breakfast | lunch | dinner | extra)
  ├── raw_user_string: str          # always preserved — audit trail
  ├── items: list[MealItem]
  │     ├── food_name: str
  │     ├── raw_description: str
  │     ├── quantity: float
  │     ├── unit: str
  │     ├── fraction_eaten: float   # 0.5 for "half a bowl"
  │     ├── macros: MacroEstimation (calories, protein_g, carbs_g, fat_g, fiber_g)
  │     ├── is_plant: bool
  │     ├── plant_name: Optional[str]
  │     └── plant_category: Optional[PlantCategory]
  ├── plants_detected: list[PlantItem]
  └── totals: (total_calories, total_protein_g, total_carbs_g, total_fat_g, total_fiber_g)
```

One validator runs before any DB write:
- **Plant consistency** — `plants_detected` must mirror plant-flagged items exactly.

Totals are **not** LLM-validated — `models.compute_totals()` overwrites `total_*` post-parse
with the server-side sum of `macros.* × fraction_eaten` (the LLM's totals are advisory only).

---

## Build Phases

### Phase 1 — Local Foundations ✅
Parsing contract offline. `models.py`, `extract.py`.

### Phase 2 — Supabase Schema ✅
Full SQL schema + view + trigger. Seed `canonical_plants`.

### Phase 3 — Webhook Pipeline ✅
Telegram bot + AWS Lambda handler. Plant resolution via `pg_trgm`.

### Phase 4 — Intelligence Layer ✅ complete
Photo input, Sunday check-in (`pg_cron`), recipe templates, on-demand commands,
`personal_ingredients` corrections, pattern insights. See `project-state.md` for
full history and current backlog (timezone column, smart scale, adaptive TDEE).

---

## Project Structure

```
nutrition-tracker/
├── README.md                  # this file
├── project-state.md           # full history, known issues, backlog
├── .env.example                # env var template
├── models.py                   # Pydantic schemas (source of truth)
├── extract.py                   # LLM parsing function (OpenRouter)
├── handler.py                   # Lambda entrypoint
├── db.py                        # Supabase client + insert/query helpers (urllib only)
├── apply_migration.py           # apply a SQL migration to live Supabase
├── setup_db.py                  # initial local DB setup
├── test_db.py                   # live Supabase integration tests
├── test_meal_reply.py           # reply formatting unit tests
├── test_models.py               # compute_totals + validator unit tests
├── deploy.sh                    # rebuild zip, upload to Lambda, smoke test
├── backup.sh                    # backup live DB to backups/*.sql.gz
└── sql/
    ├── schema.sql               # full table definitions
    ├── functions.sql            # log_meal, get_daily_context, etc. (RPCs)
    ├── views.sql                # logged_meals view
    ├── triggers.sql              # update_daily_summary trigger
    ├── seed_plants.sql           # canonical_plants seed data
    └── migrate_*.sql             # applied migrations, one per change
```

---

## Environment Variables

```bash
OPENROUTER_API_KEY=      # from openrouter.ai
SUPABASE_URL=            # from Supabase project settings
SUPABASE_KEY=            # service role (sb_secret_...) key — RLS deny-all blocks the publishable key
TELEGRAM_TOKEN=          # from @BotFather
WEBHOOK_SECRET=          # Telegram webhook auth (fail-closed)
CRON_SECRET=             # pg_cron entry-path auth (fail-closed)
SUPABASE_CONN_STRING=    # direct Postgres connection — local only, never in Lambda
```

---

## Local Development

```bash
conda activate multiagent

# Copy and fill env vars
cp .env.example .env

# Run the offline parser against test cases
python -u extract.py

# Run test suites
python test_db.py           # live Supabase integration tests
python test_meal_reply.py   # reply formatting (no network)
python test_models.py       # compute_totals + validators (no network)
```

---

## References

- [American Gut Project (2018)](https://doi.org/10.1016/j.chom.2018.05.035) — source of the 30-plants benchmark
- [USDA FoodData Central](https://fdc.nal.usda.gov/) — nutrition reference database
- [OpenRouter Docs](https://openrouter.ai/docs) — structured outputs
- [Supabase Docs](https://supabase.com/docs) — pg_trgm, triggers, pg_cron
- [python-telegram-bot](https://python-telegram-bot.org/) — Telegram integration
