# Project State — Smart Nutrition Tracker

Single-user nutrition pipeline: Telegram → AWS Lambda → OpenRouter LLM → Supabase.
Track 30 plants/week, daily macros vs targets, proactive meal prompts.

---

## Current Status — Jul 6, 2026

**Phase 4 complete.** Journal-first reframe shipped Jul 6 — replies now show items/plants/patterns, not per-meal macro numbers (macros still recorded, just not displayed). Remaining backlog: timezone column, smart scale.

| Layer | State |
|---|---|
| Telegram bot | ✅ Live |
| AWS Lambda | ✅ Deployed (python3.14, timeout 120s) |
| LLM parsing | ✅ OpenRouter, qwen/qwen3-vl-235b-a22b-instruct (paid, vision-capable) |
| Supabase schema | ✅ Full schema live (profile columns fixed) |
| Meal logging (text) | ✅ Working end-to-end |
| Meal replies | ✅ Journal-first (Jul 6) — items/plants/patterns shown; macros recorded but not displayed except in recipe replies |
| 30-plants tracking + pace | ✅ Working, HKT timezone fixed |
| Proactive meal prompts (pg_cron) | ✅ Working — now with CRON_SECRET auth |
| Webhook auth | ✅ WEBHOOK_SECRET + chat_id allowlist (fail-closed) |
| Onboarding flow | ✅ Complete |
| Sunday weekly check-in | ✅ Live — pg_cron 20:00 HKT Sunday |
| Recipe templates | ✅ Live — /recipe <name> + /recipes |
| Nutritional gap nudges | ⏸️ Disabled (Jul 6) — `gap_nudge_enabled = false`; route/formatter/cron left in place for a possible future plant-based nudge |
| On-demand commands | ✅ Live — /undo /today /week /plants (Jun 11) + /help & Telegram command menu (Jun 12) |
| Sunday weight capture | ✅ Live — check-in asks weight → weekly_check_ins (Jun 11) |
| Deploy script + monitoring | ✅ deploy.sh, backup.sh, CloudWatch alarm → SNS email (Jun 11) |
| Photo input | ✅ Live — Telegram photo (+optional caption) → multimodal LLM → same pipeline (Jun 12) |
| Personal ingredient corrections | ✅ Live — `personal_ingredients` table, label-exact macros injected into LLM prompt (Jun 12) |
| Pattern insights (new plants, repeats, streak) | ✅ Live — `/week` gained new-plants/repeats/streak lines (Jul 6) |
| Smart scale integration | ❌ Future (weight + BF% auto-logging) |

---

## What's Built (Phase 1–3 complete)

### Parsing contract (`models.py`)
- `LoggedMeal` Pydantic schema — the contract between LLM and DB
- Two validators: plant consistency (plants_detected must mirror is_plant items exactly) + macro totals (sum of items × fraction_eaten within tolerances)
- MealType: `breakfast` / `lunch` / `dinner` / `extra` (snack was removed — see history)

### LLM extraction (`extract.py`)
- `parse_meal_input(text, hkt_time, meals_logged)` → validated `LoggedMeal`
- OpenRouter via OpenAI SDK, `client.beta.chat.completions.parse`, structured output
- System prompt classifies meal type using time context + already-logged meals
- 4-attempt retry with exponential backoff
- `raw_user_string` injected post-parse (model doesn't echo it)

### Lambda handler (`handler.py`)
- Two entry paths: Telegram webhook POST and pg_cron `{"type":"meal_prompt","meal_type":"X"}`
- Always returns HTTP 200 (prevents Telegram retries)
- Onboarding state machine (steps 0–5): collects weight, body fat %, activity, goal → calculates TDEE + macro targets via Katch-McArdle
- `_format_reply`: 3-section reply for main meals (this meal / today so far / weekly plants)
- `_format_extra_reply`: concise one-liner for `extra` logs

### Database (`db.py` + `sql/`)
- All DB calls via `urllib.request` only — no binary deps (intentional for Lambda)
- `insert_meal` → single `log_meal()` RPC (handles plant resolution + all inserts atomically)
- `get_daily_context()` RPC → returns macro totals, targets, weekly plant count, day-of-week
- `daily_summaries` written only by PostgreSQL AFTER trigger — never by app code
- Plant resolution via `pg_trgm` inside `log_meal()` DB function — not in Python

### Schema
- Tables: `user_profiles`, `canonical_plants` (156 plants, 11 categories including new `other`), `meals`, `meal_items`, `weekly_check_ins`, `daily_summaries`
- View: `logged_meals` (filters `is_template_recipe = false`) — used by all queries
- RPC functions: `log_meal`, `get_daily_context`, `get_meals_logged_today`, `check_meal_logged_today`, `get_weekly_plant_count`

### User profile (live)
- 72kg · ~20% BF · Active · Goal: lose (recomposition)
- Targets: 2384 kcal · 140g protein · 285g carbs · 65g fat · 30g fiber
- Protein raised from 104g → 140g based on ISSN/ACSM research (2.0g/kg bodyweight for recomp)

---

## What Was Changed / Why

### Session: Journal-first reframe (Jul 6, 2026)

**Rationale:** ~3 weeks of live use showed per-meal LLM macro estimates are too noisy to trust displayed to the second decimal — arithmetic drift between items and totals kept triggering paid validator retries, and the numbers gave a false sense of precision. Reframed the bot around what's actually reliable: what was eaten, which plants, and consistency over time. Macro data keeps accumulating in the DB unchanged — only presentation and validation changed.

**Phase A — presentation (`handler.py`, no schema change):**
- `_format_reply` (main + extra meals), `_format_today_reply`, `_format_undo_reply`, `_format_weekly_checkin`: macro numbers removed, replaced with items echoed back + `🌿 Plants:` + weekly plants pace / consistency. `_format_recipe_reply` keeps its own macros (recipes are pre-calculated, trusted) but drops the daily "Today so far" section.
- Deleted `_format_today_section` (dead after the above) and `GAP_NUDGES` dict (dead after the weekly rewrite).
- `/help` copy updated to match.
- Nightly macro-based gap nudge disabled via `gap_nudge_enabled = false` (user-run SQL in Supabase dashboard) — route/formatter/cron job left in place for a possible future plant-based nudge.

**Phase B — server-side totals (`models.py`, `extract.py`):**
- Removed `check_macro_totals` validator (was rejecting parses on ±15 kcal/±3g mismatches — a paid-retry class). Added `models.compute_totals(items)` — sums `macros.* × fraction_eaten` per item, rounds calories to int and grams to 1dp.
- `extract.py` now overwrites the LLM's advisory `total_*` fields with `compute_totals()` post-parse via `model_copy`; `LoggedMeal` schema shape unchanged (no DB/prompt schema migration needed). Live parser run (5/5 test cases) confirmed totals are always the computed sum, not the LLM's raw output.
- `check_plant_consistency` validator untouched.

**Phase C — pattern insights (one migration, two RPCs):**
- New `sql/migrate_2026_07_05_patterns.sql`: `get_today_first_time_plants()` (plants whose first-ever log is today, HKT) and `get_week_patterns()` (`{new_plants, top_meals, streak_days}` — repeated meals over 28 days, consecutive-day streak via `daily_summaries`).
- `db.py` wrappers + `handler._first_time_plants()` / `_week_patterns_safe()` — both best-effort, never raise, fall back to `[]`/`{}` on RPC failure so a DB hiccup never blocks a reply.
- Meal reply gained "✨ First time logged: …!" flair; `/week` gained new-plants line, repeats, and streak (only shown when > 7 days, otherwise redundant with days-logged).

**Verification:** offline suites `test_meal_reply.py` (97 tests) and `test_models.py` (7 tests, new file) both green throughout. Executed via subagent-driven-development (implementer + task-reviewer per task); one task review caught a real defect (duplicate shadowed test method with dead assertions from an incomplete edit, masked by "all tests pass") — fixed and re-reviewed clean. All three phases deployed (`./deploy.sh`) and live-smoke-tested via Telegram with clean CloudWatch logs.

**Backlog:** future — observed-TDEE from `weekly_check_ins` weight trend + calorie averages (macro data still collected, just not surfaced per-meal).

### Session: LLM model migration gemma free → qwen3-vl paid (Jun 12, 2026)

**Goal:** one model for JSON structured outputs + good food reasoning + image processing (photo input prerequisite), cheap.

**Chosen: `qwen/qwen3-vl-235b-a22b-instruct`** — $0.20/M in, $0.88/M out ≈ $0.25/month at current volume. 6 providers on OpenRouter (failover headroom). Vision verified via base64 image test (correctly identified fried rice ingredients in both test images).

**Code change:** `extract.py` — model now `LLM_MODEL` env var with code default (no Lambda env change needed); hardcoded string removed. Enables per-path model override later (photo vs text).

**Models evaluated and rejected:**
- `google/gemini-3.1-flash-lite` (first pick) — **403 "violation of provider Terms Of Service" on ALL Google models**: OpenRouter account region (HK) blocked by Google Gemini API ToS. Permanent — avoid Google models on this account
- `deepseek` (original plan) — text-only, no vision
- `minimax/minimax-01` — vision but no structured-output support
- `qwen/qwen3.5-flash-02-23` — `.parse` returns None (broken structured outputs at provider)
- `bytedance-seed/seed-1.6-flash` — provider rejects `maxLength` in JSON schema (conflicts with Jun 11 Pydantic bounds)
- `qwen/qwen3-vl-32b-instruct` — passed but single provider (Alibaba), hit upstream 429 during testing; also repeated macro-totals validation failures on "pad thai" (arithmetic drift)

**Verification:** 5/5 parse test cases pass on 235b (zero retries), meal-type context test pass (breakfast @ 08:30), 58 unit tests pass, deployed via deploy.sh, smoke test PASS, logs clean.

**Prompt addition (same session):** explicit portion rules — every message = ONE single-person plate; listed foods are components sharing that plate (never one full portion each); explicit quantities override. Verified: "rice + shrimp with corn + scrambled eggs" → 475 kcal (matches comparable history log 487); multi-component plate 639 kcal with per-component scaling. Multi-component parses sometimes take 1 validation retry (arithmetic drift, validator catches) — acceptable.

**Notes:**
- OpenRouter account topped up (paid tier; key check: `is_free_tier: false`)
- Vision via OpenRouter: base64 data URL `data:image/jpeg;base64,...` works; some providers flaky on images (504/400) — when building photo input, consider `provider.order` pinning if errors recur
- `test-image1.jpeg` / `test-image2.png` in repo root = vision test fixtures

### Session: personal_ingredients correction table (Jun 12, 2026)

Label-exact macro overrides for products the LLM estimates badly. Design: **prompt injection, not post-parse override** — rows are appended to the extraction system prompt ("use these EXACT per-unit values, never estimate"), so the model still handles language/portions ("2 wedges", "half") while using label numbers.

**New DB:** `personal_ingredients` (name unique, unit_desc, macros + CHECK bounds, notes; RLS enabled) — `sql/migrate_personal_ingredients.sql`. Seeded 5 user staples: Laughing Cow wedge (35 kcal/17.5g, Asia label), Red Tractor rolled oats (375/100g; fiber=typical estimate), Chobani plain whole milk (86/100g), Kagome veg-fruit juice (70/200ml, averaged across user's flavors), Go Good whey chocolate (60/scoop).

**New Python:** `db.get_personal_ingredients()`; `extract._format_known_ingredients()` (pure) + `known_ingredients` param on `parse_meal_input`; handler `_known_ingredients()` wrapper (fetch failure never blocks logging) wired into both text + photo paths.

**Verified live:** "2 wedges of laughing cow" → exactly 70 kcal / P4.0 (label × 2). Caught unit trap: per-serve row made "one scoop" = full serve; fixed by storing per-scoop (`sql/migrate_gogood_per_scoop.sql`) → "one scoop" = exactly 60 kcal / P11.7. **Lesson: store rows in the smallest natural unit (per scoop / per wedge / per 100g), not per multi-unit serving.**

83 unit tests pass (+6); deployed, smoke PASS. Future: add rows when bad estimates caught (tell Claude or insert via dashboard).

**Also same day: `/help` command.** `_format_help()` lists all commands + logging tips; unknown slash commands now send the same reference. Registered Telegram `setMyCommands` — typing `/` in chat pops the autocomplete menu. 87 unit tests (+4); deployed.

### Session: Hardening — RLS, dedup, formula fix, live DB test (Jun 12, 2026)

**1. `test_db.py` first clean live run.** Found why it never ran: naive `urlparse` broke on raw special chars in the DB password — fixed with the right-side parse from `apply_migration.py`. All 3 tests pass; cleanup verified (0 residue rows). Same parsing bug fixed in `backup.sh` (now passes discrete `PG*` vars to pg_dump). `pg_dump` installed into multiagent conda env (no system package); backup.sh prepends that bin dir.

**2. RLS deny-all live** (`sql/migrate_2026_06_12.sql`): RLS enabled on all 6 tables, no policies; `REVOKE ALL` from anon/authenticated on tables/sequences/functions + default privileges. **Critical discovery:** `SUPABASE_KEY` had been the `sb_publishable_` (anon) key all along — CLAUDE.md wrongly said service-role. RLS briefly broke the bot (401s) until user swapped in the real `sb_secret_` key (.env + Lambda console). Architecture now matches docs; publishable key is dead weight.

**3. Telegram update_id dedup.** `user_profiles.last_update_id BIGINT` (same migration); handler drops `update_id <= stored`, records before processing (at-most-once). Kills the duplicate-meal-on-retry class.

**4. Protein formula regression fixed.** New pure `calc_targets()` in db.py (single source — handler display + complete_onboarding both use it): protein = 2.0 g/kg bodyweight when goal=lose (ISSN recomp), 1.8 g/kg LBM otherwise. Re-onboarding now yields 144g, not 104g.

**Verification:** 77 unit tests pass (+9); migration applied; RLS confirmed ON via pg_class; service-key REST path verified; deployed, smoke PASS; live `/today` confirmed end-to-end with new key through RLS.

### Session: Photo input shipped (Jun 12, 2026)

User sent lunch photo → silence (handler ignored non-text updates, 2ms exit in logs). Built the feature (TDD, 10 new tests → 68 total).

**New Python:**
- `extract.py`: `parse_meal_input(..., image_b64=None)` — multimodal user content (text part + base64 JPEG data URL) when image present; system prompt gained photo rule (estimate portions from plate size/volume/piece counts; caption = context, not separate meal)
- `handler.py`: `_photo_file_id()` (pure — largest Telegram photo size), `_get_photo_b64()` (getFile → download → base64), `_handle_photo_meal()` (full pipeline: download → parse → guard → insert → 3-section reply)
- Routing: photo (+optional caption) dispatches after allowlist check; photos during onboarding get "finish setup" reply; stickers/voice/etc. get "text or photos only" hint instead of silence

**Verification:** 68 unit tests pass; live photo parse on test images (shrimp/eggs/rice correctly itemized, 465 kcal, plants detected); deployed, smoke test PASS.

**Known quirk:** food detection on tiny images (<300px) can miss items (one run missed rice on 259px test image, caught on retry). Real Telegram photos are ≥800px — handler picks the largest size. Watch early logs.

### Session: Hardening batch + UX commands (Jun 11, 2026)

Full codebase review → 3 parallel subagents (SQL / Python / infra) → applied live + deployed. Deferred by decision: LLM model migration, photo input.

**Bug fixes:**
- `get_gap_nudge()`: today's partial-day summary was included in the 3-day streak window, violating spec — now excluded (`summary_date < today HKT`) in all three subqueries
- Deploy zip appended to stale archive forever (deleted `resolver.py` persisted inside) — `deploy.sh` now rebuilds from scratch; zip shrank 37.6MB → 11MB (`.pyc`/`__pycache__` excluded)
- Meal-window inconsistency: LLM prompt aligned to canonical map (breakfast 05–11, lunch 11–17, dinner 17+ HKT), same as recipe path

**New features:**
- `/undo` — deletes most recent logged meal via new `undo_last_meal()` RPC (cascade + trigger recompute), replies with removed meal + fresh Today section
- `/today`, `/week` — on-demand daily/weekly status (reuse existing RPCs)
- `/plants` — week's distinct plants grouped by category via new `get_week_plants()` RPC; ⚠️ marks auto-added names; lists missing categories
- Sunday weight capture — weekly check-in sets `awaiting_weight` flag + asks for weight; next bare number (30–300) → `record_weekly_weight()` RPC upserts `weekly_check_ins`; non-numeric clears flag, falls through to meal logging

**Hardening:**
- Pydantic `Field` bounds on all macros/quantities/fraction_eaten (LLM can no longer write negative or absurd values)
- DB CHECK constraints mirroring the bounds (meals, meal_items, user_profiles targets/biometrics)
- `user_profiles` singleton enforced (`CHECK (id = 1)`); `_rest_patch` default `id=gte.1` → `id=eq.1`
- Markdown escaping (`_md_escape`) on all dynamic names in replies; `_send` retries once without parse_mode on HTTPError (no more silent reply loss)
- Slash commands now behind the chat-id allowlist (profile check moved before command dispatch)
- `canonical_plants.auto_added` flag — `log_meal()` marks on-the-fly inserts; 5 historical auto-adds identified (multigrain sourdough, purple rice, rice noodles, seeds mix, white rice) — flag UPDATE pending (permission-blocked, run manually)
- extract.py: module-level client reuse; retry budget capped ≈94s (< 120s Lambda timeout)

**Infra/ops:**
- `deploy.sh` — rm zip → rebuild → upload → wait → smoke test → log reminder
- `backup.sh` — pg_dump via .env DATABASE_URL → `backups/*.sql.gz` (run weekly)
- `apply_migration.py` — psycopg2 migration runner (edit MIGRATION_FILE for next one)
- requirements.txt pinned to lambda_package versions (openai 2.41.0, pydantic 2.13.4; conda env slightly behind — note)
- CloudWatch alarm `nutrition-tracker-errors` → SNS `nutrition-tracker-alerts` → email (subscription confirm pending)
- 6 stale docs moved to `docs/archive/`; README Gemini references fixed
- Plant count reconciled: live DB = 157 (152 seeded + 5 auto-added); the 149/155 numbers were both wrong

**Verification:** 58 unit tests pass (was 40); migration applied + committed live; new RPCs verified against live DB; Lambda deployed, smoke test PASS, logs clean (init 1.6s, 96/128MB).

### Session: Plant DB expansion + /recipe plant bug fix (Jun 8, 2026)

**Bug fixed:**
- `/recipe` logged meals correctly but plants never counted toward the weekly 30. Root cause: template `meal_items` seeded without `canonical_plant_id`; `use_recipe()` copied items verbatim → trigger counted 0 (filters `WHERE canonical_plant_id IS NOT NULL`).
- Fixed `use_recipe()` DB function to resolve `canonical_plant_id` via `similarity()` lookup when copying template items.
- Backfilled all existing non-template meal items that had `plant_name` but NULL `canonical_plant_id`.
- Merged duplicate `soybean` → `soybeans` canonical plant entry (1 meal_item repointed).

**Plant DB expansion (90 → 156 plants):**
- Added `other` category: coffee, green tea, black tea, herbal tea, extra virgin olive oil, dark chocolate, matcha powder — ZOE-counted plant compounds now trackable
- Added HK staples: taro, daikon, bitter melon, lotus root, water chestnut, napa cabbage — were appearing in logs untracked
- Added plain herb names (`basil`, `cilantro`, etc.) alongside `fresh basil` etc. — fixes fuzzy match for dried variants (similarity `'dried basil'` ↔ `'fresh basil'` = 0.29, below threshold; ↔ `'basil'` = 0.45, passes)
- Full additions: legumes +4, nuts +2, whole grains +6, fruits +8, vegetables +11, tubers +3, herbs +9, spices +8, other +7
- Schema: `'other'` added to CHECK constraints on `canonical_plants.category` and `meal_items.plant_category`

**Files changed:** `sql/functions.sql`, `sql/schema.sql`, `sql/seed_plants.sql`, `sql/seed_recipe_overnight_oatmeal.sql`, `sql/migrate_plants_expansion.sql` (new)

---

### Session: Phase 4 — Nutritional gap nudges (Jun 7, 2026)

**New features:**
- Nightly cron (22:00 HKT, `0 14 * * *`) → Lambda `gap_nudge` → Telegram nudge when fiber or protein low 3 consecutive logged days
- Once-per-ISO-week cooldown via `last_gap_nudge_sent_at` on `user_profiles`
- Kill switch: `gap_nudge_enabled` column (default `true`) — flip to `false` in Supabase dashboard to silence
- Tracks fiber (target 30g) and protein (target 140g); fires if either < 80% for last 3 logged days
- Hardcoded suggestions: lentils/oats/avocado for fiber, eggs/Greek yogurt/chicken for protein

**New DB objects:**
- `user_profiles` columns: `last_gap_nudge_sent_at TIMESTAMPTZ`, `gap_nudge_enabled BOOLEAN NOT NULL DEFAULT true`
- `get_gap_nudge()` RPC — streak detection + cooldown + kill switch, returns `{fiber_gap, protein_gap}` or null
- pg_cron job `gap-nudge` registered (job id 10)

**New Python:**
- `db.py`: `get_gap_nudge()`, `mark_gap_nudge_sent()`
- `handler.py`: `_format_gap_nudge()`, `_handle_gap_nudge()`, `gap_nudge` routing block
- `test_meal_reply.py`: 10 new tests — `TestGapNudgeFormatter` (4), `TestHandleGapNudge` (3), `TestGapNudgeRoute` (3). Total: 40 tests.

**First run result:** `{fiber_gap: true, protein_gap: false}` — fiber was genuinely low 3 days running on deploy day.

---

### Session: Phase 4 — Recipe templates (Jun 6, 2026)

**New features:**
- `/recipe <name>` command: fuzzy-matches saved template (pg_trgm > 0.3), copies meal + all items atomically via `use_recipe()` RPC, replies with full 3-section macro summary (this meal / today so far / weekly plants)
- `/recipes` command: lists all saved templates ordered by meal_type then name
- meal_type on recipe logs determined from HKT time-of-day, not copied from template — `_meal_type_from_time(hour)` maps 05–11→breakfast, 11–17→lunch, 17+→dinner (never extra; recipes are always proper meals)
- Admin recipe insert path: offline macro research → Claude writes SQL INSERT block → user runs in Supabase SQL editor; `canonical_plant_id` resolved inline at insert time

**New DB objects:**
- `use_recipe(p_name, p_user_string, p_meal_type DEFAULT NULL)` — fuzzy match + atomic copy + return macros/plants
- `list_recipes()` — returns JSONB array of all templates

**New Python:**
- `db.py`: `use_recipe()`, `list_recipes()` wrappers
- `handler.py`: `_meal_type_from_time()`, `_format_recipe_reply()`, `_format_list_recipes()`, `_handle_recipe()`, `_handle_list_recipes()`, updated slash routing
- `test_meal_reply.py`: 30 tests total (+10 new: TestRecipeReplyFormatter, TestListRecipesFormatter, TestRecipeRouting, TestMealTypeFromTime)

**Recipes added:**
- Avocado Toast (breakfast): 324 kcal · P 12.6g · C 28.6g · F 18.5g · Fiber 6.4g
- Avocado Wrap (lunch): 522 kcal · P 32.1g · C 35.4g · F 25.5g · Fiber 10.1g

**Known limitation logged:** Timezone hardcoded to HKT — affects recipe meal_type classification and cron prompts when traveling.

---

### Session: Hardening + pre-Phase 4 cleanup (Jun 4, 2026)

**Security:**
- Added `WEBHOOK_SECRET` header verification on Telegram webhook path
- Added `CRON_SECRET` body field verification on pg_cron meal_prompt path
- Added chat_id allowlist — rejects messages from unknown Telegram users after onboarding
- Rotated all exposed secrets (Telegram token, OpenRouter key, DB password, webhook re-registered)
- Removed `SUPABASE_CONN_STRING` from Lambda — only `SUPABASE_URL` + `SUPABASE_KEY` needed

**Bug fixes:**
- Weekly plant count used UTC week boundary → fixed to `Asia/Hong_Kong`
- Daily summary trigger used `date(logged_at)` without timezone → fixed to HKT everywhere
- `extra` meal type got terse one-liner reply → now gets full reply (macros + daily totals + pace)
- Zero-calorie guard was rejecting valid logs (coffee, sprite) → removed `total_calories == 0` check
- Misleading `+{n} plants today` display → replaced with simple `🌿 X/30 plants this week`

**Schema:**
- Added missing `user_profiles` columns: `body_fat_pct`, `activity_level`, `goal_type`, `tdee`, `telegram_chat_id`, `onboarding_step`
- Added singleton row seed (`INSERT ... ON CONFLICT DO NOTHING`)

**Cleanup:**
- Removed `snack` from `get_meals_logged_today()` filter (dead code)
- Deleted `resolver.py` (called non-existent RPC, logic lives in `log_meal()` DB function)
- Added 30-plant goal-hit celebration message in `_pace_line()`
- Lambda timeout raised 60s → 120s
- Outer exception handler now sends error message to Telegram instead of going silent

**Deferred (logged for later):**
- RLS policies on Supabase tables (HIGH-2)
- Pydantic field bounds validation (HIGH-6)
- Fuzzy plant match threshold + quarantine unknowns (MED-5)
- Daily summary trigger stale rows on date-changing updates (MED-11)

---

### Session: Phase 4 — Sunday weekly check-in (Jun 4, 2026)
- Added `get_weekly_summary()` PostgreSQL RPC (aggregates `daily_summaries` for current HKT ISO week)
- Added `get_weekly_summary()` wrapper in `db.py`
- Added `_format_weekly_checkin()` pure formatter in `handler.py` (gap threshold: avg < 80% of target)
- Added `_handle_weekly_checkin()` and `weekly_checkin` routing block in `lambda_handler`
- Added `TestWeeklyCheckinFormatter` (4 tests) and `TestWeeklyCheckinRoute` (3 tests) in `test_meal_reply.py`
- Lambda deployed with weekly_checkin support
- pg_cron job `weekly-checkin` to be registered manually: `0 12 * * 0` (12:00 UTC = 20:00 HKT, Sunday)
- No recipe suggestions (deferred to recipe templates), no weight logging (deferred to smart-scale)

---

### LLM provider: Gemini → OpenRouter (Jun 4, 2026)
- **Why:** Gemini free API hit limits; OpenRouter routes to free models at zero cost
- **Model:** `google/gemma-4-31b-it:free` (~2–5s response, structured outputs work)
- **Breaking change:** `GEMINI_API_KEY` → `OPENROUTER_API_KEY` in Lambda env vars (already swapped)
- `google-genai` removed from `lambda_package/`, replaced with `openai`

### MealType: snack removed (prior session)
- **Why:** Not a snacker; snack type caused 5:30pm cron false positives and dinner suppression bugs
- `snack` → `extra` everywhere (models, DB constraint, extract prompt, handler)
- 5:30pm HKT cron job removed; 3 existing snack DB rows migrated to extra

### Bot reply redesign (prior session)
- Added per-meal macro section ("This meal") before daily summary
- Added pace signal: "need Y more plants over Z days to hit 30"
- `extra` logs get concise one-liner, never suppress main meal cron prompts

### Onboarding (prior session)
- First-contact triggers 4-question flow: weight → body fat % → activity → goal
- TDEE calculated in Python (Katch-McArdle) and stored in `user_profiles`

---

## Known Issues

| Issue | Severity | Notes |
|---|---|---|
| Timezone hardcoded to HKT | Low | `TZ`, cron jobs, DB functions, `_meal_type_from_time` all use Asia/Hong_Kong. When traveling, recipe meal_type and cron prompts fire on HKT schedule regardless of local time. Fix: `timezone` column on `user_profiles`, read in handler — non-trivial migration touching cron, DB functions, and daily summary trigger |
| Duplicate meal rows in DB | Low | Testing artifact from development; not a production bug |
| "No meal provided" 0-kcal rows | Low | Edge case: empty/unparseable messages insert a 0-kcal row. Handler has a guard (`if not meal.items or meal.total_calories == 0`) but it fires after insert — should reject before calling `parse_meal_input` |
| LLM response time | Info | ~2–15s depending on model load. Lambda timeout set adequately but worth monitoring |
| `openrouter/free` alias | Info | Returns None content for normal completions; `.parse` works fine. Switched to explicit model ID to avoid this |

## Hardening Backlog (pre-production gaps)

Not blocking Phase 4, but not production-tight. Fix before treating this as a hardened service.

| Item | Severity | Notes |
|---|---|---|
| ~~No Lambda smoke test~~ | ~~High~~ | ✅ Done Jun 11 — `deploy.sh` rebuilds zip from scratch, uploads, smoke-tests, fails nonzero |
| ~~No live DB integration test run~~ | ~~High~~ | ✅ Done Jun 12 — URL-parse bug fixed, all 3 tests pass, cleanup verified |
| ~~RLS / grants / revokes~~ | ~~High~~ | ✅ Done Jun 12 — RLS deny-all on all 6 tables + anon/authenticated revokes; SUPABASE_KEY corrected to sb_secret |
| ~~Pydantic validation bounds~~ | ~~High~~ | ✅ Done Jun 11 — `Field` bounds on macros, quantity, fraction_eaten, name lengths |
| ~~DB CHECK constraints~~ | ~~High~~ | ✅ Done Jun 11 — non-negative/range CHECKs on meals, meal_items, user_profiles; singleton `CHECK (id = 1)` |
| Plant fuzzy match threshold | Low | `similarity > 0.3` still loose and category-blind. Mitigated Jun 11: auto-inserts flagged `auto_added=true` and ⚠️-marked in /plants. Further mitigated Jun 12: `personal_ingredients` table lets bad estimates be corrected per-label |
| ~~Telegram message idempotency~~ | ~~Medium~~ | ✅ Done Jun 12 — `last_update_id` column + handler drop of stale/duplicate update_ids |
| ~~Markdown injection in replies~~ | ~~Medium~~ | ✅ Done Jun 11 — `_md_escape` on all dynamic names + plain-text retry fallback in `_send` |
| ~~Re-onboarding macro regression~~ | ~~Medium~~ | ✅ Done Jun 12 — `calc_targets()` uses 2.0 g/kg bodyweight for goal=lose (yields 144g vs stored 140g; no longer regresses to 104g) |
| ~~Profile PATCH too broad~~ | ~~Low~~ | ✅ Done Jun 11 — `id=eq.1` default + singleton CHECK |
| ~~Slash commands bypass allowlist~~ | ~~Low~~ | ✅ Done Jun 11 — profile/allowlist check moved before command dispatch |
| ~~Unpinned dependencies~~ | ~~Low~~ | ✅ Done Jun 11 — pinned to lambda_package versions |
| ~~Auto-added plant flag backfill~~ | ~~Low~~ | ✅ Done Jun 11 — user ran the UPDATE in dashboard. Decided Jun 12: white rice / rice noodles DO count as plants — no change needed |
| ~~SNS email confirmation pending~~ | ~~Low~~ | ✅ Confirmed Jun 12 — subscription shows real ARN; CloudWatch alarm → email delivery live |

---

## Phase 4 — What's Left

Priority order:

### 1. Sunday weekly check-in ✅ Complete
- pg_cron `0 12 * * 0` (20:00 HKT) → Lambda `weekly_checkin` → Telegram summary
- Content: plants (X/30), macro averages vs targets, gap callouts (avg < 80% target)
- Gap nudges hardcoded per nutrient; no recipe suggestions (deferred)
- `get_weekly_summary()` RPC live in Supabase

### 2. Recipe templates ✅ Complete
- `/recipe <name>` — fuzzy-matches template (pg_trgm similarity > 0.3), copies meal + items atomically, replies with full 3-section macro summary
- `/recipes` — lists all saved templates with macros + meal-type emoji
- Admin insert path: user researches macros → Claude writes SQL INSERT block → run in Supabase dashboard
- meal_type determined from HKT time-of-day (05–11 breakfast, 11–17 lunch, 17+ dinner). Template meal_type is display-only metadata
- Templates live: Avocado Toast (breakfast, 324 kcal), Avocado Wrap (lunch, 522 kcal)
- New DB functions: `use_recipe()`, `list_recipes()`
- New Python: `_format_recipe_reply`, `_format_list_recipes`, `_handle_recipe`, `_handle_list_recipes`, `_meal_type_from_time`
- 30 tests passing

### 3. Nutritional gap identification ✅ Complete
- Nightly cron (22:00 HKT) checks last 3 logged days — fiber + protein < 80% of target
- Sends nudge at most once per ISO week; kill switch via `gap_nudge_enabled` column

### 4. Photo input ✅ Complete (Jun 12)
- Telegram photo (+optional caption) → base64 → multimodal LLM → same `LoggedMeal` schema
- Model: `qwen/qwen3-vl-235b-a22b-instruct`
- Best for restaurant meals and complex home cooking
- Not for packaged staples (pre-stored data is more accurate — describe in text/caption instead)

### 5. `personal_ingredients` table ✅ Complete (Jun 12)
- Live with 5 seeded staples; prompt-injection design (LLM scales label values by quantity)
- Add a row whenever a bad estimate is caught — smallest natural unit per row
- Plant fuzzy-match threshold concern folded in: brand rows bypass estimation entirely

### 6. Smart scale integration (future)
- Smart scale (with BF% estimation) → auto weekly weigh-in to `weekly_check_ins`
- Logs both `weight_kg` and `body_fat_pct` without manual entry
- Enables adaptive TDEE recalibration based on weekly trend
- Deferred until hardware purchased

---

## Deployment

**Lambda:** `nutrition-tracker` (ap-southeast-2, python3.14, 128MB)

**Deploy steps:**
```bash
# 1. Install deps into lambda_package/
pip install openai -t lambda_package/ --upgrade

# 2. Zip (packages at root — NOT nested under lambda_package/)
cd lambda_package && zip -r ../lambda_deployment.zip . && cd ..
zip lambda_deployment.zip extract.py handler.py db.py models.py

# 3. Upload
aws lambda update-function-code --function-name nutrition-tracker --zip-file fileb://lambda_deployment.zip
```

**Lambda env vars required:** `OPENROUTER_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `TELEGRAM_TOKEN`, `WEBHOOK_SECRET`, `CRON_SECRET`

**Run tests locally:**
```bash
conda activate multiagent
python -u extract.py      # 5 parse test cases
python test_db.py         # DB integration (needs live Supabase)
python test_meal_reply.py # Reply formatting
```
