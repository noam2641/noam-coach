# Test Matrix

Updated: 2026-06-27

## Mandatory Gates

| Gate | Command | Result |
|---|---|---|
| Compile | `C:\Users\user\anaconda3\python.exe -m compileall -q coach_bot.py noam_coach` | Passed |
| Lint | `C:\Users\user\anaconda3\python.exe -m ruff check .` | Passed |
| Unit/acceptance tests | `C:\Users\user\anaconda3\python.exe -m pytest --maxfail=0 -ra` | 573 passed, 0 skipped, 0 xfailed, 3 warnings |
| Evaluations | `C:\Users\user\anaconda3\python.exe scripts\run_evaluations.py` | 33/33 passed |
| Preflight | `C:\Users\user\anaconda3\python.exe scripts\preflight.py --skip-runtime-secrets` | Passed |
| Startup smoke | bounded `coach_bot.py` process | Passed startup milestones, no traceback |
| FastAPI route smoke | `TestClient(coach_bot.api)` for `/healthz` and `/readyz` | Passed with runtime readiness flags set; plain `/readyz` reports Telegram not ready without live runtime |
| Telegram initialization smoke | `coach_bot.build_telegram_app()` with fake local token | Passed, 13 handlers and 6 jobs registered |
| Mini App route smoke | `GET /mini` with signed session cookie | Passed |
| DB migration smoke | `scripts\migrate_db.py --db <temp.db>` | Passed on temporary SQLite DB |
| Release build | `C:\Users\user\anaconda3\python.exe scripts\build_release.py` | Passed, 178 files |
| Extracted release smoke | extracted release compile/import/preflight | Passed; SHA-256 `088b1f573415eb2bece1607250a4f65478e7b0e2623635aa3e15daf81e684b8b` |

## REC-PROGRAM-04 Coverage

| Area | Tests |
|---|---|
| Availability precedence and production planning integration | `tests/test_recording_04_regression.py`, `tests/acceptance/test_recording_04_program_meal_flow.py`, `tests/test_mini_profile_api.py` |
| Dietary alias boundaries and negation | `tests/test_recording_04_regression.py` |
| Nutrition plan restriction filtering | `tests/acceptance/test_recording_04_program_meal_flow.py` |
| Body-fat source-aware normalization | `tests/test_recording_04_regression.py`, `tests/acceptance/test_recording_04_program_meal_flow.py` |
| Delta meal correction | `tests/test_recording_04_regression.py`, `tests/acceptance/test_recording_04_program_meal_flow.py` |
| Mini App CSP/XSS/static rendering | `tests/test_miniapp_render.py` |
| Mini upload body limits | `tests/test_api_guard.py` |

No known-bug `xfail` cases remain in the test tree.

## REC-NEXT-MEAL-05 Coverage

| Area | Tests |
|---|---|
| Shared workout-aware next-meal service | `tests/acceptance/test_rec_next_meal_05.py` |
| Rest day and signed remaining/overage balances | `tests/acceptance/test_rec_next_meal_05.py` |
| Pre-workout and post-workout nutrition phases | `tests/acceptance/test_rec_next_meal_05.py` |
| Planned workout time passed without assuming completion | `tests/acceptance/test_rec_next_meal_05.py` |
| Persisted workout-status clarification | `tests/acceptance/test_rec_next_meal_05.py` |
| Dietary restriction filtering for generated options | `tests/acceptance/test_rec_next_meal_05.py` |
| Mini App API parity with central recommendation | `tests/acceptance/test_rec_next_meal_05.py` |
| Mini App workout clarification persistence | `tests/acceptance/test_rec_next_meal_05.py` |
| Fasting and "not fasting" daily flags affect the recommendation | `tests/acceptance/test_rec_next_meal_05.py` |

## Nutrition Context Coverage

| Area | Tests |
|---|---|
| Structured nutrition context separates reported meals from planned meals | `tests/test_nutrition_context.py` |
| Morning menu AI request receives the structured nutrition payload | `tests/test_nutrition_context.py` |
| Meal reanalysis AI fallback receives the structured nutrition payload | `tests/test_nutrition_context.py` |

## Mini App Continuity Coverage

| Area | Tests |
|---|---|
| Today's meals API returns DB-backed meals and quality data | `tests/test_mini_profile_api.py` |
| Mini App shell renders today's meals card and JS hooks | `tests/test_miniapp_render.py` |

## Plan Completion Coverage

| Area | Tests |
|---|---|
| Missing plan details continue one question at a time until smart plan hub | `tests/test_plan_completion_flow.py` |
| Returning to the plan hub clears plan-completion state without `NameError` | `tests/test_plan_completion_flow.py` |
