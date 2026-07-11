# CHATGPT PATCH-09 — Backlog verification and stabilization

## Purpose

This patch is a cumulative stabilization patch for the 14 screenshot-derived backlog items. It includes all previously supplied ChatGPT patches/hotfixes plus additional verification contracts and focused fixes found during the audit.

The audit rule used here: do not mark an item as implemented because a report says so; require an implementation hook in production code plus a regression/contract check.

## Verification performed in this environment

Passed:

```text
python -m compileall -q noam_coach/bot/onboarding.py noam_coach/services/health_jobs.py noam_coach/bot/ui.py noam_coach/services/nutrition_context.py noam_coach/services/next_meal.py noam_coach/bot/meal_text.py noam_coach/bot/callback_menu.py noam_coach/bot/callback_plans.py noam_coach/bot/callback_meals.py noam_coach/bot/workout.py noam_coach/services/availability.py noam_coach/services/goal_validation.py noam_coach/services/learned_foods.py planning.py exercise_plans.py training_intelligence.py user_model.py recommendations.py tests/regression/test_backlog_14_contracts.py
```

Passed:

```text
python -m pytest -p no:cacheprovider -q tests/regression/test_backlog_14_contracts.py
# 9 passed
```

Not run here: full pytest, because this container lacks the project runtime dependencies available on Noam's Windows environment. Run the full suite locally after extracting.

## Changes added in PATCH-09

### TASK-07 — four-day workout plans must not drop Sunday / requested days

`planning.py` now keeps `consistency_freq = desired` instead of reducing to `desired - 1`. This specifically prevents the screenshot bug where a 4-day availability could produce a 3-day plan.

For 4-day availability, the three candidate structures are now named and shaped professionally:

- Full Body מותאם
- Upper / Lower מאוזן
- ABC + Full Body מותאם

`exercise_plans.py` adds explicit 4-day templates:

- `FB1`, `FB2`, `FB3`, `FB4`
- `U1`, `L1`, `U2`, `L2`

### TASK-10 — profile must not look like a debug log

`render_profile_snapshot()` was rebuilt to show categorized profile sections instead of a raw list with a source label on every line.

It now includes:

- goal/body block, including `goal_weight_kg` and `goal_timeframe_weeks`
- nutrition block
- training block
- routine block
- one canonical active availability block

It intentionally does not render raw `weekly_availability` next to resolved preferred days, because that is what created contradictory profile screens.

### TASK-11 — HealthKit must not override manual training availability

`health_jobs.py` now treats only confirmed user-authored facts as manual overrides. HealthKit-derived confirmed facts are not enough to suppress or replace user-declared days.

The HealthKit import wizard no longer shows arbitrary `2 / 3 / 4` buttons when the data is insufficient. It asks the user to type the number and leaves only a skip/escape button.

### Backlog contract test

Added:

```text
tests/regression/test_backlog_14_contracts.py
```

This test scans production source and verifies that the 14 backlog contracts have implementation hooks, including:

- nutrition completion is domain-scoped
- next-meal recommendation is one focused option
- post-meal status renderer is connected
- daily menu canonical route exists
- requested workout days are not dropped
- ExerciseCatalog/substitution hooks exist
- global workout parameter scope exists
- profile and HealthKit precedence are explicit
- Telegram stale callback handling exists
- goal validation is connected

## Backlog status after PATCH-09

| Task | Status | Evidence level |
|---|---|---|
| TASK-01 Nutrition flow not jumping to workout | Implemented | code + contract |
| TASK-02 don't repeat confirmed questions | Mostly implemented | state/readiness hooks + contract; still needs live E2E |
| TASK-03 one immediate next-meal recommendation | Implemented | code + contract |
| TASK-04 recommended/planned/consumed separation | Behavior implemented | reported/planned separation; not a full DB status model |
| TASK-05 standalone daily menu | Partially implemented | separate renderer/route; full pin/edit message-id lifecycle still not complete |
| TASK-06 unified menu hierarchy | Partially implemented | canonical routes/aliases; full UX consolidation still needs live review |
| TASK-07 professional workout plan structures | Improved materially | 4-day split preservation + Full Body/Upper-Lower/ABC structures |
| TASK-08 ExerciseCatalog + pain/substitution | Partial but real | catalog/substitution/adaptation hooks; not exhaustive for every exercise |
| TASK-09 workout parameter editing/global scope | Implemented at parser/handler level | contract checks scope/confirmation hooks |
| TASK-10 clean profile | Improved materially | categorized renderer + active availability precedence |
| TASK-11 HealthKit manual precedence | Implemented | manual user-source precedence + Health wizard cleanup |
| TASK-12 Telegram callback/network handling | Implemented | safe callback/error classification hooks |
| TASK-13 goal weight validation | Implemented | service + onboarding connection |
| TASK-14 short post-meal day status | Implemented | callback connection + renderer |

## Known remaining gaps

These are not hidden:

1. Full DB-level `meal.status` model is still not implemented. The current behavior separates planned vs reported/consumed through services and flags.
2. Daily menu can be rendered separately, but full Telegram message-id tracking for pin/edit lifecycle is not finished.
3. ExerciseCatalog is much better than before, but not exhaustive across all future exercises.
4. Full live Telegram UX still needs manual scenario testing after local full pytest.

## Required local verification after extract

```powershell
python -m pytest -p no:cacheprovider -q tests/regression/test_backlog_14_contracts.py
python -m pytest -p no:cacheprovider -q tests/regression/test_re13_health_quality.py tests/regression/test_learned_foods_personalization.py tests/regression/test_goal_weight_validation.py tests/test_availability_parser.py tests/test_next_meal_callbacks.py
python -m pytest -p no:cacheprovider -q
```
