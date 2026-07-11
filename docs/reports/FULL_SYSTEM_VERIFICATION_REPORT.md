# Full System Verification Report - Noam Coach

Generated: 2026-07-01 18:35 Asia/Jerusalem  
Commit inspected: `ecc41a3`  
Scope: production code, handlers, services, DB schema/migrations, Mini App, Telegram routing, tests, local DB health, static/runtime smoke. Existing audit docs were used only as comparison context, not as source of truth.

## א. תקציר מנהלים

Noam Coach is a real, locally runnable Telegram + FastAPI + Mini App system. The strongest proven areas are: DB initialization/migrations, single active conversation flow, goal versioning, meal approval/persistence, next-meal recommendations, workout session/set logging, Health import security, Mini App auth/API guards, proactive job delivery gates, and regression/evaluation coverage.

It is not fully production-ready for additional users. The app is explicitly configured around `SETTINGS.telegram_allowed_user_id`, Mini App session auth rejects anyone else, and startup readiness stays false until live Telegram polling is up. Live Telegram polling and external bot identity were not exercised in this verification.

Main risk: some product capabilities are implemented through pragmatic JSON state (`daily_flags`, `active_flow.payload`, `conversation_state`) rather than domain tables. This is acceptable for a personal bot, but weak for multi-user product reliability, analytics, and long-term feature evolution.

One real defect was found during verification: `build_nutrition_context(..., now=...)` used the real current day for reported meals and quality assessment. This made the nutrition context time-dependent and caused `tests/test_nutrition_context.py::test_nutrition_context_counts_reported_not_planned_meals` to fail on 2026-07-01. I fixed it with a small local-day-bounds helper in `noam_coach/services/nutrition_context.py`; full pytest then passed.

Readiness verdict:

| Question | Answer |
| --- | --- |
| Personal daily use | Mostly yes, with caveats around live Telegram/external dependencies |
| Additional users | No, not without auth/user-isolation/product hardening |
| Claims fully matching original product ideal | No |
| Core nutrition/workout flows usable end-to-end | Yes, mostly proven through tests/simulated handlers |
| Fully live Telegram smoke | Not verified externally |

## ב. מפת ארכיטקטורה

| Component | Role | Main files | Dependencies | Status |
| --- | --- | --- | --- | --- |
| Telegram Bot | Commands, text, photo, document and callback routing | `coach_bot.py`, `noam_coach/app/runtime.py`, `noam_coach/bot/*` | SQLite, services, Telegram token | full locally, external polling not verified |
| FastAPI | Health, readiness, Watch, HealthKit, Mini App APIs | `coach_bot.py`, `mini_api.py`, `noam_coach/api/*` | DB, auth tokens, storage | partial: API smoke ok, readiness 503 without Telegram |
| Mini App | Dashboard/profile/plans/meals/upload UI | `mini_api.py`, `miniapp/templates/index.html`, `miniapp/static/app.js` | cookie session token, shared services | partial/full for supported features |
| SQLite | Durable source of truth | `db.py`, `noam_coach.db` | migrations, repository-like DB class | full locally |
| AI layer | Intent, meal analysis, photo analysis, planning support | `assistant.py`, `meal_intelligence.py`, `recommendations.py`, `noam_coach/services/profile.py` | OpenAI client when configured | partial; deterministic fallbacks exist |
| Health import | Apple Health ZIP/XML/API/watch ingestion | `health_import.py`, `health_service.py`, `noam_coach/api/health_routes.py`, `noam_coach/services/health_jobs.py` | storage, DB, auth token | full locally, external device sync not verified |
| Scheduler/proactive | Morning/evening, calorie watch, motivation, workout prompt, weekly summary | `noam_coach/jobs/proactive.py`, `noam_coach/app/runtime.py` | JobQueue, DB `job_state`, data quality | partial/full; live delivery not verified |
| Conversation FSM | One active flow per user with version/expiry | `conversation.py`, `db.py` | `active_flow`, callback versioning | full locally |
| Planning | Goal/nutrition/workout/unified candidate versioning | `planning.py`, `noam_coach/bot/callback_plans.py` | user facts, goal versions, active plans | partial/full |
| Audit/logging | Product events, audit rows, analytics | `event_log.py`, `noam_coach/services/core.py`, `db.py` | DB | partial |
| Release/preflight | Compile/import/settings/storage/DB checks | `scripts/preflight.py`, `scripts/run_evaluations.py`, `scripts/build_release.py` | local env | full locally |

## ג. Database Verification

Schema source: `db.py::SCHEMA` plus migrations 1-9 in `SCHEMA_MIGRATIONS`.

Local DB checks:

```text
PRAGMA quick_check: ok
PRAGMA foreign_key_check: 0 violations
schema_migrations: 9
users: 1
goals: 1
goal_versions: 0
health: 3781
routine_profile: 1
user_facts: 7
meals/sessions/sets/active_flow/job_state/plan_versions: 0 in local DB
```

Important tables and meaning:

| Entity | Stored data | Writers | Readers | Retention/expiry |
| --- | --- | --- | --- | --- |
| `users` | Telegram user identity | `ensure_user_record` | all flows | until delete/export tools |
| `goals` | legacy/current goal mirror | goal activation | daily status, legacy callers | no expiry |
| `goal_versions` | proposed/active/superseded goals | `planning`, goal callbacks | `fetch_goal`, planning, next meal | no expiry; superseded retained |
| `approvals` | pending meal/photo/edit payloads | meal analysis, edit approval | meal callbacks | operational retention scripts |
| `meals` | consumed meals/totals | approval save, next-meal save | daily status, nutrition context, weekly | no expiry |
| `meal_items` | ingredient rows | meal persistence | analysis/weekly | cascade with meal |
| `sessions` | workout session state | workout start/runtime | workout UI, next meal | no expiry |
| `sets` | actual set performance | workout callbacks/watch | progression/fatigue | no expiry |
| `health` | Apple Health/device samples | imports/API/watch | readiness, routine, status | no expiry by default |
| `daily_flags` | per-day JSON flags | checkins/next meal/status | daily context, fatigue, proactive | day-keyed JSON |
| `user_facts` | profile/preferences/allergies/availability | onboarding/Mini/text/import | planning/context | versioned in history |
| `active_flow` | current FSM flow, version, expiry | conversation engine | text/callback router | 60 min or 24h depending flow |
| `conversation_state` | legacy/microflow state | core flow helpers | next-meal active recommendation, split state | operational retention |
| `product_events` / `audit` | audit trail | handlers/services | diagnostics | no expiry shown |
| `job_state` | proactive dedupe/retry | proactive jobs | proactive delivery | per day |
| `plan_versions` / `active_plans` | plan candidates/active plan | planning | Mini/Telegram/recommendations | superseded retained |
| `mini_login_tokens` | one-time Mini login tokens | `/app` command | Mini login | TTL + consumed flag |

Key finding: `daily_flags` is doing a lot of state work: fasting, hunger, sleep, next-meal status, temporary avoided ingredients, saved next-meal fingerprints, quantity scales. This is working, but it is not a clean domain model.

## ד. Capability Catalog

Legend: `full`, `partial`, `not_connected`, `missing`, `unreliable`, `external_verification`.

| Domain | Capability | Status | production path | DB | test/smoke | Gap |
| --- | --- | --- | --- | --- | --- | --- |
| Architecture | App composition and handler registration | full | `build_telegram_app` | n/a | 13 handlers, 6 jobs smoke | live Telegram not run |
| DB | Schema, migrations, FK checks | full | `Database.init`, migrations 1-9 | all tables | preflight + quick_check | none local |
| Context | Single active flow, stale callbacks | full | `ConversationRouter`, `handle_callback` | `active_flow` | `test_conversation_v2`, regression | old `conversation_state` remains |
| Goals | Create/propose/approve/version active goal | full | `planning`, `services/goals.py`, callbacks | `goal_versions`, `goals` | DB/planning tests | local DB currently has legacy goal only |
| Goals | Manual calorie goal change 2100 | full | free text -> confirm -> goal version | `goal_versions` | `test_next_meal_callbacks`, planning tests | not live Telegram |
| Nutrition | Daily status from logged meals | full | `build_daily_status` | `meals`, `goal_versions` | `test_daily_status` | no meals in local DB |
| Nutrition | Text meal logging | partial | `log_meal_from_text` | `approvals`, `meals` | tests cover analyzer pieces | needs OpenAI for real text analysis |
| Nutrition | Photo meal logging/correction | partial | `handle_photo`, `_handle_meal_correction_text` | `approvals`, `meals`, `meal_fingerprints` | regression/evaluations | live image upload not run |
| Nutrition | Quantity edit before meal save | full | `editqty`, `qtydelta`, deterministic parser | `approvals` | meal callback tests | button matrix still non-trivial |
| Nutrition | Duplicate meal prevention | full | fingerprints + duplicate checks | `meal_fingerprints`, `meals` | evaluations duplicate cases | depends on photo hash availability |
| Nutrition | Next meal recommendation | full | `services/next_meal.py`, `menu:nextmeal`, text route | `daily_flags`, `meals`, `goals` | acceptance + RE8 regression | deterministic food catalog, not full pantry |
| Nutrition | Next meal free-text corrections | full | `handle_recommendation_correction` | `daily_flags`, `user_facts` | RE8 tests for tortilla/quantity | selected-option state in legacy `conversation_state` |
| Nutrition | NutritionContext for AI/menu | partial | `services/nutrition_context.py` | meals/plans/facts/flags | fixed failing test; now passes | several fields `not_captured` |
| Nutrition | Nutrition plan generation | partial | `planning.build_nutrition_candidates` | `plan_versions` | planning tests | generated slots, not full meal plan with grocery inventory |
| Training | Availability parsing | full | `parse_hebrew_availability_answer` | `user_facts` | `test_availability_parser`, regression | none local |
| Training | Workout plan generation | partial | `planning.build_workout_candidates` | `plan_versions`, `active_plans` | plan quality tests | deterministic templates, limited exercise library |
| Training | Active workout and set logging | full | `show_session`, `save_set`, callbacks | `sessions`, `sets` | set/session tests + evaluations | live Telegram not run |
| Training | Workout parameter text edit | full | workout_parameter_edit flow | `active_flow`, `exercise_overrides` | RE8 regression | new in working tree |
| Training | Split set, undo, pause, reopen | full | callback_session/runtime | `sets`, `sessions`, `conversation_state` | set undo/session tests | live timer not externally verified |
| Training | Pain/safety substitutions | partial | callback_session pain flow | `medical_constraints`, `audit` | regression/unit | medical guidance limited |
| Training | Readiness/fatigue | full | `services/training.py`, `training_intelligence.py` | `daily_flags`, `sets`, `sessions`, health | evaluations + unit tests | heuristic, not medical |
| Profile | Onboarding and fact capture | full | `bot/onboarding.py`, `questions.py` | `user_facts`, `active_flow` | onboarding tests | live UX not run |
| Profile | Preferences/allergies/restrictions | full | `food_preferences`, assistant routing | `user_facts` | food preference tests | canonical aliases limited |
| Health | HealthKit ZIP/XML import | partial | health routes/jobs/import service | `health`, `user_facts` | security/import tests | no fresh real import in this run |
| Mini App | Dashboard/profile/plans/today meals | partial | `mini_api.py` routes | shared DB | API/mini tests | `/mini` requires auth cookie |
| Mini App | Same next-meal service as Telegram | full | `/mini/api/next-meal` | shared `daily_flags` | acceptance Mini API tests | frontend not browser-smoked |
| Proactive | Scheduled reminders/summaries | partial | jobs/proactive + runtime schedule | `job_state` | jobs/data quality tests | no live Telegram send |
| AI | Intent + meal/photo analysis | partial | `assistant.py`, `services/profile.py` | approvals/facts | unit/eval | external model not invoked here |
| AI | Structured validation/fallback | partial | Pydantic models, deterministic post-checks | approvals/events | evaluations | still has broad `except Exception` fallbacks |
| Security | API auth/body limits/Mini tokens | full | `api/security.py`, `mini_auth.py` | tokens/users | API guard tests | multi-user not supported |
| Release | Compile/Ruff/pytest/eval/preflight | full | scripts/Makefile | n/a | all passed after fix | smoke_test needs base URL |
| RE8 visual artifacts | Screenshot/video inspection | missing | no media files found | n/a | regression tests only | no actual image review possible |
| Live Telegram | Polling + bot identity | external_verification | `run`, `verify_bot_identity` | runtime state | not run against network | requires live token/network |

Summary count: full 20, partial 11, not_connected 0, missing 1, unreliable 0, external_verification 1.

## ה. End-to-End Flows

### Onboarding

```text
/start or menu
-> onboarding handlers/questions
-> user_facts writes
-> active_flow question state
-> confirmation/reconciliation
-> profile/planning readiness
```

Evidence: `noam_coach/bot/onboarding.py`, `questions.py`, `user_model.py`, tests `test_onboarding.py`, `test_questions.py`, `test_onboard_02_regression.py`.

### Goal

```text
user goal input / menu
-> proposal/version in goal_versions
-> explicit approval/provisional approval
-> supersede older active versions
-> goals mirror updated
-> daily status/next meal read active goal
```

Evidence: `planning.activate_goal`, `services/goals.fetch_goal`, migration `single_active_goal_version`, tests `test_database.py`, `test_planning_v2.py`.

### Next Meal

```text
Telegram menu/text or Mini API
-> build WorkoutNutritionContext
-> calculate consumed/remaining from meals + active goal
-> allocate MealBudget
-> generate deterministic options
-> validate restrictions/preferences
-> render compact choices
-> choose option
-> free-text correction if needed
-> explicit save
-> meals row + daily_flags idempotency
```

Evidence: `noam_coach/services/next_meal.py`, `callback_menu.py`, `meal_text.py`, `mini_api.py`, tests `acceptance/test_rec_next_meal_05.py`, `regression/test_recording_20260628_re8.py`.

Important behavior: with high remaining calories, the system does not suggest a tiny meal and explains that not all remaining calories must be eaten at once. RE8 test proves this.

### Meal Logging

```text
photo/text
-> analysis
-> approval payload
-> render meal with totals/items
-> correction/quantity edits
-> duplicate check
-> approve
-> meals + meal_items + optional fingerprint
-> daily status
```

Evidence: `noam_coach/bot/meals.py`, `callback_meals.py`, `meal_text.py`, evaluations and meal tests. Live Telegram photo upload was not run.

### Daily Menu / Nutrition Plan

```text
active goal + user facts
-> planning candidates
-> plan_versions
-> active_plans
-> daily menu/status/NutritionContext read planned meals separately from consumed meals
```

Finding: planned meals are separated from reported meals. The bug found and fixed was around date bounds when `now` is supplied in tests.

### Workout Plan

```text
profile + safety + availability
-> resolved availability
-> candidate workout plans
-> quality repair/validation
-> plan_versions
-> active_workout_plan fact
-> workout overview
```

Evidence: `planning.py`, `availability.py`, `callback_plans.py`, `test_exercise_plan_quality.py`, `test_availability_parser.py`.

### Active Workout

```text
start workout
-> session row active
-> current exercise card
-> set performed / adjusted / split
-> atomic save_set
-> rest timer
-> next set/exercise
-> finish full/partial/cancel
-> workout_summary + progression data for future
```

Evidence: `workout.py`, `callback_session.py`, `workout_runtime.py`, tests `test_set_undo.py`, `test_training_intelligence.py`, evaluations.

### Health Import

```text
Telegram document / local path / Mini upload / API
-> auth/size/type validation
-> parse Health export
-> upsert health rows
-> derive routine/facts
-> readiness/status/proactive can consume health
```

Evidence: `health_import.py`, `health_service.py`, `health_routes.py`, `health_jobs.py`, tests `test_health_import_security.py`, `test_device_routes.py`.

### Mini App

```text
/app
-> one-time mini_login_tokens
-> session cookie
-> /mini dashboard
-> /mini/api/profile/plans/next-meal/meals/upload
-> same DB/services as Telegram
```

Evidence: `mini_api.py`, `mini_auth.py`, `miniapp/*`, tests `test_mini_tokens.py`, `test_mini_profile_api.py`, `test_miniapp_render.py`.

## ו. RE8 Verification

No `.png`, `.jpg`, `.jpeg`, `.webp`, `.mp4`, or `.mov` artifacts were found in the repository, so no visual image inspection was possible.

| Artifact | Observed behavior | Current state | Fixed? | Evidence |
| --- | --- | --- | --- | --- |
| RE8 regression replay | next-meal compact keyboard, tortilla removal/dislike, quantity edit, high remaining calories, free-text next meal route, workout parameter edit | covered by tests | yes | `tests/regression/test_recording_20260628_re8.py` |
| Visual screenshots/videos | no files found | not verified | n/a | `rg --files -g *.png ...` returned no files |

## ז. Calculations

Examples verified by tests/evaluations:

| Calculation | Evidence | Result |
| --- | --- | --- |
| Goal missing sex/age | evaluations `goal-missing-sex-age` | provisional true, 2090 kcal, 160g protein |
| Goal complete | evaluations `goal-complete` | provisional false, 2370 kcal, 160g protein |
| Calorie floor | evaluations `goal-calorie-floor` | 1400 kcal, 90g protein |
| Consumed meal count | fixed `test_nutrition_context_counts_reported_not_planned_meals` | reported meal 700 kcal counted; planned meals not counted |
| Next meal high remaining | RE8 regression | 2600 remaining -> options >500 kcal and budget max >=650 |
| Ingredient totals | next-meal quantity regression | selected ingredient quantity recalculates option totals |
| Workout status | evaluations | single set/zero duration => partial, all sets => completed |
| Readiness | evaluations | fresh 100, poor sleep 62, severe pain 27 |
| Epley 1RM | evaluations | 100x5 => 116.7 |

## ח. UX Review

Strengths:

- Main Telegram entry points are registered and route through one callback/text router.
- Next meal was improved toward fewer buttons: choose option first, then free-text corrections.
- Dynamic numeric edits are mostly text-based where appropriate.
- Stale callback handling exists with version/flow id.
- Meal save requires approval and offers undo/edit.

Gaps:

- Several extracted bot modules still import a very large compatibility surface and use `runtime_bound`; this works but is harder to reason about than ordinary dependency injection.
- Mini App is useful but narrower than Telegram: no full parity for every Telegram workout/meal correction interaction.
- Some screens still use many inline buttons, especially active workout and safety substitution.
- Errors are mostly friendly, but some fallback paths return home/menu rather than resuming exact context.

## ט. Gaps By Severity

### Critical

None found after the small NutritionContext fix, within local verification scope.

### High

| Gap | Root cause | User impact | Files | Fix | Required test |
| --- | --- | --- | --- | --- | --- |
| Live Telegram polling not verified | external token/network not exercised | system may pass locally but fail on real bot identity/polling | `runtime.py` | run controlled live smoke with real token | startup/polling smoke |
| Single-user authorization model | `telegram_allowed_user_id` and Mini session guard | not ready for additional users | `config.py`, `mini_api.py`, `mini_auth.py` | introduce real user auth/tenant model | IDOR/multi-user tests |
| Heavy JSON state in `daily_flags` | fast feature delivery over typed tables | harder debugging, migration, analytics | `next_meal.py`, `health_service.py`, `nutrition_context.py` | promote stable fields to typed tables | migration + compatibility tests |

### Medium

| Gap | Root cause | User impact | Files | Fix | Required test |
| --- | --- | --- | --- | --- | --- |
| NutritionContext optional fields not captured | no production writer | AI context says `not_captured` for pantry/location/prep | `nutrition_context.py` | add capture flows or remove from AI contract | context writer tests |
| Photo/text meal real AI not exercised | external model not invoked | live meal recognition could differ | `services/profile.py`, `meal_text.py` | add mocked schema failure + live sandbox eval | AI integration tests |
| Mini App parity incomplete | narrower API/UI | user may need Telegram for complex edits/workouts | `mini_api.py`, `miniapp/*` | define parity scope | Mini E2E tests |
| Proactive delivery not live-smoked | no running JobQueue/Telegram send | reminders may not actually reach Telegram | `proactive.py`, `runtime.py` | local JobQueue integration smoke | delivery retry/suppression tests |

### Low

| Gap | Root cause | User impact | Files | Fix | Required test |
| --- | --- | --- | --- | --- | --- |
| `scripts/smoke_test.py` requires base_url | script is post-deploy only | accidental no-arg run fails | `scripts/smoke_test.py` | document or default localhost | script CLI test |
| Local DB currently sparse | local DB has health/facts but no meals/sessions/plans | manual smoke cannot prove history-rich UI | `noam_coach.db` | seed smoke fixture DB | seed script test |

## י. Run Results

Commands executed:

```text
DB backup:
Copy-Item noam_coach.db backups\noam_coach_verification_20260701_182632.db

compile:
python -m compileall -q .
exit 0

ruff:
python -m ruff check .
All checks passed!

pytest initial:
python -m pytest -q -o addopts="" --maxfail=0
1 failed, 639 passed, 3 warnings
failure: tests/test_nutrition_context.py::test_nutrition_context_counts_reported_not_planned_meals

fix:
noam_coach/services/nutrition_context.py now computes local-day UTC bounds from supplied now.

targeted retest:
python -m pytest -q tests\test_nutrition_context.py::test_nutrition_context_counts_reported_not_planned_meals
1 passed

pytest final:
python -m pytest -q -o addopts="" --maxfail=0
640 passed, 3 warnings in 92.03s

evaluations:
python scripts\run_evaluations.py
33 passed, 0 failed, pass_rate 1.0

preflight:
python scripts\preflight.py --skip-runtime-secrets
Preflight passed

FastAPI smoke via TestClient:
/healthz 200 {"status":"ok","version":"2.0.0-rc7"}
/readyz 503 {"status":"not_ready","version":"2.0.0-rc7","checks":{"database":true,"storage":true,"disk_free_mb":656361,"telegram":false},"errors":[]}

Telegram registration smoke:
handlers: 13
jobs: morning, evening, calorie_watch, motivation, workout_prompt, weekly_summary

scripts/smoke_test.py:
exit 1 without base_url; script requires explicit base_url.
```

## יא. Honest Conclusion

1. Does the system truly match everything defined?  
   No. It matches a large, useful subset, but not the full ideal of low-friction, fully contextual, multi-surface, multi-user product readiness.

2. What percent of listed capabilities are met?  
   By the catalog above: 20 full out of 33 total = 60.6% fully proven. If partial-but-usable is included, 31 out of 33 = 93.9% existing in some working form.

3. Ready flows: DB/migrations, goal versioning, daily status, next meal, meal approval/edit basics, active workout, set logging, availability parsing, preferences/allergies, Mini next meal, preflight/evaluations.

4. Broken or unproven flows: live Telegram polling, RE8 visual screenshot inspection, multi-user operation, fully live AI/photo verification, complete Mini App parity.

5. Can it be used day to day without getting stuck?  
   Likely yes for the configured personal user, assuming Telegram token/network are correct. The tests cover many restart/stale/idempotency cases. But there are still fallback-to-menu paths and live external dependencies not proven in this run.

6. Five most important remaining problems:
   - Run a real live Telegram startup/polling smoke with bot identity verification.
   - Decide whether this remains a personal single-user bot or becomes multi-user; the current design is personal.
   - Replace high-value `daily_flags` JSON state with typed tables where behavior is now stable.
   - Add browser-level Mini App E2E and parity matrix for meal/workout edits.
   - Add controlled live/sandbox AI/photo tests for malformed JSON, timeouts, hallucinated foods/allergens and schema drift.
