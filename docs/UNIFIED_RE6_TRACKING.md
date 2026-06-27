# Noam Coach RE6 Unified Tracking

Updated: 2026-06-27

Status legend: Not started, In progress, Completed, Blocked.

## Claude Findings

| Priority | Item | Status | Evidence |
| --- | --- | --- | --- |
| P0 | `menu:smartplan` must clear stale `plan_completion` | Completed | `noam_coach/bot/callback_plans.py`; `tests/test_plan_completion_flow.py::test_plan_menu_clears_completion_flow_without_name_error`; `python -m pytest -q tests\test_plan_completion_flow.py` -> 6 passed |
| P0 | `planv2:complete_missing` must continue questions until done/back | Completed | `handle_plan_callback("planv2:complete_missing")` delegates to `ask_next_plan_completion_question`; regression `test_complete_missing_callback_starts_continuous_completion_flow` |
| P0 | `PlanningBlockedError` must be business UX, not generic ERROR | Completed | `callback_plans.py` catches before generic `Exception`; pending action stored in `conversation_state`; regression `test_planning_blocked_generation_saves_pending_action_without_error_log` |
| P0 | Resume plan generation after goal approval/restart | Completed | `resume_pending_plan_action`; `callback_menu.py` resumes after `GOAL_ACTIVATED`; regressions `test_resume_pending_plan_action_generates_and_clears_state`, `test_goal_approval_resumes_pending_plan_action` |
| P0 | Telegram transient errors and shutdown stability | Completed locally | `noam_coach/services/telegram_errors.py`; `callback_router.on_error`; `runtime.run`; `tests/test_telegram_lifecycle.py` -> 3 passed. External live Telegram verification still required. |
| P1 | One active goal including `active_provisional` | Completed | `planning.activate_goal` supersedes `active` and `active_provisional`; `tests/test_planning_v2.py::test_activate_goal_supersedes_active_provisional_goal` |
| P1 | DB constraint for one active goal | Completed | migration 9 `single_active_goal_version`; partial unique index `ux_goal_versions_single_current`; `tests/test_database.py::test_single_active_goal_migration_cleans_duplicates_and_adds_constraint` |
| P1 | Real preferences/dislikes/allergies flow | Completed locally | `noam_coach/services/food_preferences.py`; `record_dietary_preference`; `assistant.keyword_fallback`; `tests/test_food_preferences.py`; `tests/test_assistant.py`; targeted pytest -> 47 passed |
| P2 | Complete `NutritionContext` and "what to eat now" anti-repetition | Completed locally | `noam_coach/services/nutrition_context.py`, `noam_coach/services/next_meal.py`; disliked foods and explicit option feedback filter/regenerate options; `record_next_meal_served` stores daily recommendation history; `test_next_meal_history_prioritizes_fresh_options`. |

## Master Document Scope

| Task | Status | Evidence / next proof required |
| --- | --- | --- |
| 1. Single source of truth for goals and approval | Completed locally | Goal activation/resume fixed, one-current-goal constraint/migration added, active_provisional is superseded by active goals, and `fetch_goal` prefers `goal_versions` over legacy `goals`; `python -m pytest -q tests\test_planning_v2.py tests\test_database.py` -> 15 passed. |
| 2. `PlanningBlockedError` friendly business block | Completed | See Claude P0 evidence above. |
| 3. Telegram polling, pool and graceful shutdown | Completed locally | See Claude P0 evidence above; live Telegram smoke remains external. |
| 4. Full regression tests for recorded scenarios | Completed locally | Added/verified regressions for plan completion, planning block/resume, Telegram lifecycle, preferences, NutritionContext, next-meal, availability, workout candidate quality, profile rendering and recording flows; `python -m pytest -q tests\acceptance tests\test_recording_04_regression.py tests\test_rec_plan_meal_03.py tests\test_bugfix_regression.py` -> 166 passed; targeted `ruff` passed. |
| 5. Contextual food recommendation feedback loop | Completed locally | Free-text "I don't like/prefer" is stored canonically; Telegram next-meal screen exposes "לא מתאים לי" option feedback; `save_next_meal_option_feedback` persists dislike and regenerates; `tests/test_next_meal_callbacks.py`, `test_next_meal_feedback_saves_dislike_and_regenerates`. |
| 6. Canonical preferences, allergies, intolerances and restrictions | Completed locally | Shared `food_preferences` service, opposite preference cleanup, allergy/restriction targets, next-meal preference filtering; `python -m pytest -q tests\test_food_preferences.py tests\test_assistant.py tests\acceptance\test_rec_next_meal_05.py` -> 47 passed |
| 7. Clean and safe profile rendering | Completed locally | Structured profile values are formatted without raw dict/list output in `noam_coach/bot/onboarding.py`; `tests/test_daily_status.py::test_profile_formats_structured_values_without_raw_dicts`; existing gap dict invariant remains green. |
| 8. Accurate Hebrew availability parsing | Completed locally | `parse_hebrew_availability_answer` converts Hebrew day/time/duration text into structured `weekly_availability`, `workout_window`, and `session_minutes`; onboarding stores parsed availability; `tests/test_availability_parser.py`; availability regressions green. |
| 9. Workout candidate quality | Completed locally | Added `planning.workout_quality_issues` for duplicate exercises, missing names/times, invalid prescriptions and excessive volume; activation blocks bad workout payloads; `tests/test_planning_v2.py` quality and duplicate-block regressions. |
| 10. Central nutrition service for AI routes | Completed locally | `NutritionContext` feeds morning menu, evening summary and meal reanalysis; next-meal uses the central deterministic nutrition/workout service; `tests/test_nutrition_context.py`, `tests/test_recommendations.py` -> 19 passed. |
| 11. "Today menu" and "what to eat now" product behavior | Completed locally | Next meal covers signed balances, workout clarification, fasting, restrictions/dislikes, feedback and anti-repetition; no-active-goal menu/next-meal behavior has explicit default disclaimers; `tests/test_menu_product_behavior.py`. |
| 12. Consistent Active Flow/FSM and minimum typing | Completed locally | `active_flow` is authoritative; plan-completion flow continues until done/back; conversation router regressions and plan-completion regressions green. |
| 13. Mini App parity | Blocked by user direction | User explicitly requested to skip Mini App for now. |
| 14. Useful proactive messages | Completed locally | Proactive delivery now uses explicit quality gates: morning check-in is not blocked by empty nutrition logs, morning menu requires goal quality but not same-day meals, intraday nudges require usable nutrition data, and all non-urgent messages defer during active flow; `python -m pytest -q tests\test_jobs.py tests\test_data_quality.py` -> 7 passed; `ruff check data_quality.py noam_coach\jobs\proactive.py tests\test_jobs.py` -> passed. |
| 15. Onboarding and Apple Health | Completed locally | Health import keeps source/confidence/history, protected ZIP/XML/local-path import, freshness checks, confirmation UI for existing facts, and now appends a structured follow-up summary for "requires approval" and "still missing" without raw internal keys; `python -m pytest -q tests\test_onboard_02_regression.py tests\test_questions.py tests\test_onboarding.py tests\test_health_import_security.py` -> 66 passed; targeted `ruff` passed. |
| 16. Observability, admin alerts and privacy | Completed locally | Telegram transient/shutdown errors are classified, admin alerts are deduped, unexpected error alerts/log/event payloads redact tokens and local user paths, proactive/job/onboarding/health flows record events, and existing retention/privacy/API guards remain green. No new Mini App work was added per user direction; existing Mini auth/static guards were only verified. `tests/test_telegram_lifecycle.py` -> 5 passed; privacy/retention/API guard subset -> 15 passed; targeted `ruff` passed. |
| 17. Architecture cleanup and dead code | Completed locally | Removed obsolete plan-completion block earlier; callback routing remains centralized in `noam_coach/app/runtime.py`; lifecycle helpers manage background tasks; broad compile/lint audit found and fixed an unused loop variable; no duplicate handler registration found in runtime composition. `python -m compileall -q coach_bot.py noam_coach assistant.py questions.py onboarding.py planning.py health_service.py` -> passed; `ruff check coach_bot.py assistant.py questions.py onboarding.py planning.py health_service.py noam_coach --select F401,F811,F821,F841,E722,B` -> passed; architecture/import/conversation tests -> 16 passed. |

## Current Local Verification

- `python -m pytest -q tests\test_plan_completion_flow.py` -> 6 passed.
- `python -m pytest -q tests\test_telegram_lifecycle.py tests\test_plan_completion_flow.py` -> 9 passed.
- `python -m pytest -q tests\test_planning_v2.py tests\test_database.py` -> 15 passed.
- `python -m pytest -q tests\test_food_preferences.py tests\test_assistant.py tests\acceptance\test_rec_next_meal_05.py` -> 47 passed.
- `python -m pytest -q tests\test_food_preferences.py tests\test_next_meal_callbacks.py tests\acceptance\test_rec_next_meal_05.py` -> 19 passed.
- `python -m pytest -q tests\test_next_meal_callbacks.py tests\acceptance\test_rec_next_meal_05.py` -> 15 passed.
- `python -m pytest -q tests\test_daily_status.py tests\acceptance\test_recording_04_program_meal_flow.py -k "profile_no_raw_dict or structured_values"` -> 2 passed.
- `python -m pytest -q tests\test_availability_parser.py tests\test_recording_04_regression.py -k "availability" tests\acceptance\test_recording_04_program_meal_flow.py -k "availability"` -> 13 passed.
- `python -m pytest -q tests\test_planning_v2.py tests\test_recording_04_regression.py::TestAvailabilityWiring::test_workout_candidates_use_resolved_availability` -> 8 passed.
- `python -m pytest -q tests\test_nutrition_context.py tests\test_recommendations.py` -> 19 passed.
- `python -m pytest -q tests\test_menu_product_behavior.py tests\acceptance\test_rec_next_meal_05.py` -> 15 passed.
- `python -m pytest -q tests\test_conversation_v2.py tests\test_plan_completion_flow.py` -> 11 passed.
- `python -m pytest -q tests\test_jobs.py tests\test_data_quality.py` -> 7 passed.
- `python -m pytest -q tests\test_onboard_02_regression.py tests\test_questions.py tests\test_onboarding.py tests\test_health_import_security.py` -> 66 passed.
- `python -m pytest -q tests\test_telegram_lifecycle.py` -> 5 passed.
- `python -m pytest -q tests\test_privacy_tools.py tests\test_retention.py tests\test_photo_retention.py tests\test_operational_retention.py tests\test_api_guard.py tests\test_event_log.py tests\test_mini_tokens.py tests\test_miniapp_render.py tests\test_coach_bot_utils.py -k "privacy or retention or auth or token or mini_upload or mini_page or event or audit"` -> 15 passed.
- `python -m compileall -q coach_bot.py noam_coach assistant.py questions.py onboarding.py planning.py health_service.py` -> passed.
- `ruff check coach_bot.py assistant.py questions.py onboarding.py planning.py health_service.py noam_coach --select F401,F811,F821,F841,E722,B` -> passed.
- `python -m pytest -q tests\test_architecture.py tests\test_imports.py tests\test_conversation_v2.py tests\test_plan_completion_flow.py` -> 16 passed.
- `python -m pytest -q tests\acceptance tests\test_recording_04_regression.py tests\test_rec_plan_meal_03.py tests\test_bugfix_regression.py` -> 166 passed.
- `git diff --check` -> passed with CRLF normalization warnings only.
- `python -m compileall -q .` -> passed.
- `python -m ruff check .` -> passed.
- `python -m pytest -q` -> full suite passed.
- `python scripts\run_evaluations.py` -> 33/33 passed.
- `python scripts\preflight.py --skip-runtime-secrets` -> passed.
- Targeted `ruff check` for changed files passed after each group.
- `python -m compileall -q config.py noam_coach\app\runtime.py noam_coach\bot\callback_router.py noam_coach\services\telegram_errors.py` -> passed.

## Remaining Quality Gates

- startup/shutdown smoke without live secrets where locally possible
- final release ZIP secret/runtime-file scan
