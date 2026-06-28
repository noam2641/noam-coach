# Codex Audit After Claude 678a160

Audit date: 2026-06-28
Baseline commit: `678a1604c3a7dea293bb689a6d2e93fffa9f26b5`
Branch: `codex/complete-rec-program-04`
Environment: Windows 11 Pro 10.0.26200, Python 3.12.8, timezone Asia/Jerusalem

## Baseline Verification

Isolated worktree: `C:\coach_bot\noam_coach_audit_678a160`.

| Claude claim | Verified | Evidence |
|---|---:|---|
| Commit is self-contained | Yes | clean worktree at `678a160`; import/compile/ruff/evaluations/preflight ran from isolated checkout |
| Compile passes | Yes | `python -m compileall -q .` exit 0 |
| Ruff passes | Yes | `python -m ruff check .` -> `All checks passed!` |
| Evaluations pass | Yes | `33/33`, pass rate 1.0 |
| Preflight passes | Yes | `Preflight passed` with `--skip-runtime-secrets` |
| Pytest fully green | No | `1 failed, 626 passed, 3 warnings in 49.11s` on 2026-06-28 |
| re7 calorie cap 269 -> no 450 meal | Yes | re7 regression plus current focused tests |
| Goal 2100 is single active source | Yes | re7 regression through `confirm:goal_cal` |
| Temporary rejection is not permanent dislike | Yes | re7 regression through `nextmeal:dislike:*` |
| Meal saved only after approval and idempotent | Yes | re7 regression through choose/save path |
| Shutdown cleanup test exists | Yes | re7 regression suite covers injected runtime failure |
| No skip/xfail added | Yes | scan found no new skip/xfail |

## Findings Fixed

| Finding | Root cause | Production implementation | Test | Result |
|---|---|---|---|---|
| H-1 meal nutrition values were not actual ingredient totals | `MealOption` carried plain ingredient strings and template-level calories/protein, sometimes shaped by the budget target | Added `MealIngredient`, canonical nutrition facts, ingredient-derived totals, total/plausibility validation, cap repair by scaling ingredients | `tests/regression/test_claude_audit_re8.py::test_low_remaining_protein_is_calculated_from_ingredients` | Fixed |
| H-2 date-dependent nutrition context test | Test hardcoded weekday 6 and used real current date | Test now injects fixed `2026-06-28` clock and matching Sunday index | `tests/test_nutrition_context.py::test_nutrition_context_counts_reported_not_planned_meals` | Fixed |
| M-1 per-day availability duration lost | Parser stored one global `session_minutes` for all segments | `_segment_availability_by_day` now carries per-segment duration; slots store segment minutes | `tests/regression/test_claude_audit_re8.py::test_availability_friday_one_hour_keeps_per_day_duration` | Fixed |
| L-1 `*.zip` too broad for future fixtures | Global ignore hid all zip fixtures | `.gitignore` keeps zips ignored but allows `tests/fixtures/*.zip` | diff + release scan | Fixed |
| L-2 `nextmeal:editqty` placeholder | Callback only showed a text hint | Implemented quantity edit screen and `nextmeal:qty:*` scaling, persisted in daily flags and active recommendation snapshot | `tests/regression/test_claude_audit_re8.py::test_quantity_edit_recalculates_option_totals_through_callback` | Fixed |
| Additional callback ordering risk | Rendering a recommendation records titles as recent, so fresh regeneration can reorder before choose/save | Active recommendation now stores full option payloads; choose/save/edit resolve from active snapshot first | same quantity callback regression | Fixed |

## Nutrition Examples

Scenario: target 2100 kcal, consumed 1831 kcal, remaining/cap 269 kcal.

| Meal | Ingredients | Computed calories | Computed protein | Cap |
|---|---|---:|---:|---:|
| Cottage 5% with vegetables | cottage 150 g, cucumber 100 g, tomato 120 g | 184 | 18 g | 269 |
| Protein yogurt 0% with small fruit | protein yogurt 200 g, small apple 120 g | 206 | 20 g | 269 |
| Egg-white omelet with vegetables | 3 egg whites, vegetables 160 g, olive oil 1 tsp | 131 | 13 g | 269 |

All displayed totals are computed from `ingredient_details`; budget targets are used only for budget allocation and validation, not as actual protein values.

## Final Gates

```text
compile: python -m compileall -q . -> pass
ruff: python -m ruff check . -> All checks passed!
pytest run 1: pass (quiet project output)
pytest run 2: 630 passed, 3 warnings in 49.22s
evaluations: 33 passed / 0 failed
preflight: Preflight passed
```

Warnings: three existing Starlette `TestClient` cookie deprecation warnings in `tests/test_coach_bot_utils.py`.

No `mypy` config and no `package.json` are present, so no mypy/npm gates apply.

## Traceability

| Requirement | Production implementation | Test | Result |
|---|---|---|---|
| Meal total equals sum of ingredients | `MealOption.__post_init__`, `_ingredient_totals`, `validate_meal_option` | `test_low_remaining_protein_is_calculated_from_ingredients` | Pass |
| Protein target is not displayed as actual protein | templates use `_meal_option` with `MealIngredient`; no `budget.protein_*` copied to option protein | same | Pass |
| Calorie cap remains enforced | `_repair_option_to_budget` scales ingredients | re7 scenario 1, re8 low remaining | Pass |
| Quantity edit recalculates totals | `adjust_next_meal_quantity`, `nextmeal:qty:*`, active option payload | re8 quantity callback | Pass |
| Meal saved only after approval | existing `save_chosen_meal`; choose uses active option snapshot | re7 scenario 5 | Pass |
| Per-day duration preserved | availability segment tuple includes minutes | re8 availability | Pass |
| Date-independent nutrition context test | injected clock and computed weekday | nutrition context test | Pass |
| Legal zip fixtures can be tracked | `.gitignore` exception for `tests/fixtures/*.zip` | diff/release scan | Pass |

## External Verification Limits

Not locally verifiable without external resources: real Telegram bot token/delivery, real OpenAI API calls, real Apple Health export from a device, public HTTPS production server, production monitoring/secrets manager.

## Release

Final commit: `6d68c4fb8c708d56266f262662d19cb8a0fc09ee`

Release ZIP path: `dist/noam_coach_2.0.0-rc7.zip`

ZIP verification: built by `scripts/build_release.py`, independently scanned for forbidden entries, extracted to a temporary directory, compiled, and passed `python scripts/preflight.py --skip-runtime-secrets` from the extracted package.

Note: the authoritative ZIP SHA-256 is reported after package build in the final Codex response. Embedding that hash inside this audit file would change the ZIP contents and therefore change the hash.
