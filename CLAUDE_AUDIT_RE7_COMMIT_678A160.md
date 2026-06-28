# Final Verification Audit — Noam Coach — commit `678a160` (re7)

Read-only verification of commit
`678a160 Fix re7 next-meal/goal issues + audit follow-ups; commit untracked services & tests`
and of a **fresh git worktree checked out at that exact commit** (isolated from
the original working tree). **No files were changed during this audit.**

Audit date: 2026-06-28.

---

## 1. Release Decision

### GO WITH RESTRICTIONS

The headline re7 fixes are real and run through production paths, the commit is
self-contained, and tests were not weakened. But there are two genuine defects:

- **High — meal nutrition values are not real** (protein is the budget target
  pasted onto every option, not summed from ingredients; there is no
  per-ingredient breakdown at all). The calorie *cap* is correct, but the
  displayed macros are not credible.
- **High — the suite is date-dependent**: a committed test
  (`test_nutrition_context.py::test_nutrition_context_counts_reported_not_planned_meals`)
  passes only when "today" is Saturday. On the audit date (Sunday) the clean
  checkout reports **1 failed, 626 passed**. Codex's "627 passed" was true only
  on the day it ran.

Neither is a release blocker for the calorie-overshoot product bug (that is
fixed), but the nutrition numbers shown to users are misleading and the green
suite is not deterministic — hence restrictions, not a clean GO.

---

## 2. Exact pytest summary line

- **Original working tree (Saturday-equivalent / Codex run):** `627 passed, 3 warnings`
- **Clean worktree at `678a160`, audit run (2026-06-28, Sunday):**
  **`1 failed, 626 passed, 3 warnings in 46.57s`**
  - Failure: `tests/test_nutrition_context.py::test_nutrition_context_counts_reported_not_planned_meals`
    — `assert len(context.planned_meals) == 3` got `0` (date-dependent, see F-2).

---

## 3. Did the clean checkout pass?

**No — not unconditionally.** A real `git worktree` at `678a160` (no `.env`, no
zip, no working-tree files) gave:

| Check | Result |
|---|---|
| `python -c "import coach_bot"` | **OK** (self-contained; no untracked/local dependency) |
| `python -m compileall -q .` | **PASS** (exit 0) |
| `python -m ruff check .` | **PASS** ("All checks passed!") |
| `python -m pytest -o addopts="" -q -p no:cacheprovider` | **1 failed, 626 passed** (date-flaky F-2) |
| `python scripts/run_evaluations.py` | **PASS** (33/33) |
| `python scripts/preflight.py --skip-runtime-secrets` | **PASS** |

The import, compile, ruff, evaluations and preflight all pass from the commit
alone — confirming the previously-untracked services are now genuinely in Git.
Only the date-flaky test fails.

---

## 4. Is `678a160` self-contained and complete?

**Yes.** `git show --name-status 678a160` lists all four services as **added**:
`noam_coach/services/{food_preferences,next_meal,nutrition_context,telegram_errors}.py`,
plus all required tests as added, including the re7 file
`tests/regression/test_recording_20260627_re7.py`. `python -c "import coach_bot"`
succeeds from the clean worktree, so HEAD imports without the original tree.

- `git status --short` → empty (clean working tree).
- `git ls-files --others --exclude-standard` → empty.
- No `.env`, `*.db`, `*.zip`, `*.log`, `*.pyc` or `__pycache__` in the commit.
- No zip tracked anywhere in HEAD.

---

## 5. Were all six re7 findings actually closed? (production paths)

| # | re7 finding | Closed? | Evidence (production path) |
|---|---|---|---|
| 1 | 269 remaining → 450 meal | **Closed** | `allocate_next_meal_budget` caps `calories_max` to remaining; policy `low_remaining`. Verified live in worktree: remaining 269 → options 245/230/210, none ≥450, `calories_max ≤ 269`. `test_scenario1_...` |
| 2 | goal 2100 vs 2390 | **Closed** | `assistant.keyword_fallback`→`set_calorie_goal`→`route_free_text` shows confirm; `confirm:goal_cal:2100`→`handle_menu_callback` activates a single goal (supersedes provisional). `test_scenario2_...` drives the real handlers; verifies one current row + next-meal target=2100. |
| 3 | "לא מתאים לי" same meal / permanent dislike | **Closed** | `nextmeal:dislike:N`→`save_next_meal_option_feedback` stores a TTL rejection (fingerprint), excludes it, returns a different option; `disliked_foods` stays empty. `test_scenario3_...` via `handle_menu_callback`; rejection persisted in `daily_flags`. |
| 4 | "אבל נשאר לי 269" ignored | **Closed** | `handle_text_message` calls `handle_recommendation_correction` BEFORE generic routing; reloads snapshot, recomputes, stays in flow. `test_scenario4_...` drives real `handle_text_message`; reply contains options + 269, not the main menu. |
| 5 | answer-first + buttons + save-only-on-confirm | **Closed** | `format_next_meal_recommendation` is answer-first; `next_meal_action_rows` has the full set; choosing/viewing never logs a meal; `save_chosen_meal` is the only write and is double-tap idempotent (`first=True, second=False`). `test_scenario5_...` |
| 6 | provisional vs approved goal | **Closed** | `confirm:goal_cal` versions+activates; `planning.activate_goal` supersedes `active`+`active_provisional`; `fetch_goal` returns one source. `test_scenario6_...` + `test_provisional_goal_approval_resumes_pending_plan_action`. |

All six behaviours were re-verified by **running the production handlers in the
clean worktree**, not just by reading helpers.

---

## 6. Are the three options' nutrition values credible?

**No — this is the main new finding (H-1).** The displayed macros are not derived
from the ingredients.

`MealOption` has `ingredients: list[str]` (plain strings) and only meal-level
`calories`/`protein`. There is **no per-ingredient calories/protein**, so the
audit's "sum of ingredients vs meal total" cannot be checked — the breakdown does
not exist. The values come from constants and the budget target:

`noam_coach/services/next_meal.py:720-748` (`_low_remaining_templates`):
- protein = `min(max(budget.protein_min, 18/16), budget.protein_max + 5/4)` for
  **all three** options → with this scenario they all collapse to **40 g**.
- calories = `min(cap, 245/230/210)` (fixed constants clamped to the cap).

Reality check (typical values):
- "קוטג׳ 5% 150 גרם" ≈ **~17 g** protein — shown **40 g** (~2.3× overstated).
- "יוגורט חלבון 0% 200 גרם" ≈ ~18–20 g — shown 40 g.
- "3 חלבוני ביצה" ≈ **~11 g** — shown **40 g** (~3.6× overstated).

So the 40 g is a **target pasted onto every option**, exactly the case the audit
said to flag as High. The calorie cap is genuine and the product overshoot bug is
fixed, but a user reading "40 g protein" for 3 egg whites is being misled, and the
three options carry identical macros despite very different foods.

(No artificial fractional-unit fudging to hit 269 was found — the calories are
plausible constants under the cap; the defect is the macro values, not unit
gymnastics.)

---

## 7. Do the re7 tests exercise production?

**Yes.** `tests/regression/test_recording_20260627_re7.py` calls the real
handlers: `callback_menu_bot.handle_menu_callback` (`menu:nextmeal`,
`nextmeal:dislike`, `nextmeal:choose`, `nextmeal:save`, `confirm:goal_cal`),
`meal_text_bot.handle_text_message`, and `assistant_bot.route_free_text`. Only the
auth/identity boundary (`is_allowed`/`ensure_user`) is mocked — the routing,
budgeting, goal activation and persistence are production code. The shutdown test
invokes the real `runtime.run` with injected `serve()`/polling errors and asserts
no leftover tasks via `asyncio.all_tasks()`.

---

## 8. Is `*.zip` a safe ignore rule?

**Safe today, slightly too broad for the future (Low).** Every zip-using test
builds its archive at runtime in `tmp_path` with `zipfile.ZipFile`
(`test_health_import_security.py`, `test_local_health_path.py`,
`test_coach_bot_utils.py`, `test_build_release.py`). There are **no committed zip
fixtures** and no `tests/fixtures/` directory, so `*.zip` currently excludes
nothing needed. Risk is only prospective: a future committed fixture would be
silently ignored. Recommended (do **not** apply during audit):

```gitignore
*.zip
!tests/fixtures/*.zip
```

---

## 9. New Findings by Severity

### High

**H-1 — Meal nutrition values are not computed from ingredients (misleading macros).**
- `next_meal.py:720-748` (and the other template builders): option protein/calories
  come from the budget target / constants, not from the ingredient list. All three
  low-remaining options show identical 40 g protein for very different foods; real
  values are ~11–20 g. No per-ingredient breakdown exists, so totals cannot be
  validated against components.
- Impact: users are shown inaccurate protein/calorie numbers; the "protein-dense"
  rationale is unsubstantiated; options look distinct but carry identical macros.
- Fix direction: give ingredients quantities + per-item kcal/protein, compute meal
  totals by summation, and validate totals == sum(items) within a rounding
  tolerance (the audit's own §4 requirement, currently unmet).

**H-2 — Suite is date-dependent; clean checkout is not deterministically green.**
- `tests/test_nutrition_context.py::test_nutrition_context_counts_reported_not_planned_meals`
  hardcodes the plan day as `"weekday": 6` while production matches
  `(today.weekday()+1)%7`. It passes only when today is Saturday; on the audit
  date (Sunday) the clean checkout is **1 failed, 626 passed**.
- Impact: "627 passed" is not reproducible day-to-day; CI will fail ~6/7 days.
  This is a committed test added in `678a160`, so it is in scope.
- Fix direction: freeze "now" (inject a fixed datetime) or set the plan weekday to
  the computed `(now.weekday()+1)%7` of the test's clock.

### Medium

**M-1 — Availability parser supports per-day time but not per-day duration.**
- Audit input `ראשון 19:00, שני 19, רביעי 18:30 45 דקות, שישי 10:00 שעה` yields
  sun 19:00 / mon 19:00 / wed 18:30 / fri 10:00 — times correct — but **every day
  gets the global `session_minutes` (45)**; שישי should be 60 ("שעה"). Duration is
  parsed once globally, not per segment.
- Impact: per-day workout lengths are wrong when the user states different
  durations per day. (The original "Monday disappears" / global-time bug from the
  earlier audit IS fixed.)

### Low

**L-1 — `*.zip` ignore is broader than needed (future fixtures).** See §8. No
current impact; add `!tests/fixtures/*.zip` if zip fixtures are ever introduced.

**L-2 — `nextmeal:editqty` is a placeholder.** The "edit quantities" button shows a
text hint rather than opening a real quantity editor; acceptable as a stub but not
the full P1-11 capability.

---

## 10. Five precise tasks for Codex (remaining gaps)

1. **H-1 (nutrition realism).** Model meal options with per-ingredient quantity +
   kcal + protein; compute option totals by summation; assert
   `abs(total - sum(items)) <= tolerance` and stop reusing the budget target as the
   per-option protein. Acceptance: 3 egg whites ≈ 11 g (not 40 g); options have
   distinct, ingredient-derived macros. Add a test asserting totals == sum(items).

2. **H-2 (date-flaky test).** Make
   `test_nutrition_context_counts_reported_not_planned_meals` deterministic — pass a
   fixed `now`/weekday so `planned_meals == 3` every day. Acceptance: clean checkout
   green on any weekday (run with a forced Sunday clock).

3. **M-1 (per-day duration).** Extend `parse_hebrew_availability_answer` /
   `_segment_availability_by_day` to capture a per-segment duration (so "שישי 10:00
   שעה" → 60 min for Friday only). Acceptance: the §5 input yields fri minutes=60,
   wed minutes=45.

4. **H-1 validator gap.** Extend `validate_meal_option` to also check
   macro-plausibility (protein not exceeding what the ingredients can provide), not
   only the calorie cap. Acceptance: an option claiming 40 g protein from low-protein
   ingredients fails validation and is repaired/regenerated.

5. **L-1 / L-2 cleanup.** Narrow the gitignore to `*.zip` + `!tests/fixtures/*.zip`,
   and either implement a real `nextmeal:editqty` quantity editor or relabel the
   button so it does not imply an editor that doesn't exist.

---

*End of report. No code was modified during this audit.*
