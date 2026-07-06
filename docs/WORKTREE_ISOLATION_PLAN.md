# Worktree Isolation Plan

This repository currently has mixed staged, unstaged, and untracked work. Do not
create a broad commit from this state. Use selective staging only.

## Current rule

- Do not run `git add .` or `git add -A`.
- Do not run `git reset`, `git stash`, or `git revert` unless the owner asks.
- Use `git add -p` or explicit file paths after reviewing each hunk.
- Keep unrelated legacy changes out of feature commits.

## Suggested commit slices

### 1. Menu callback regression

Purpose: keep `menu:today` as a backwards-compatible alias for `menu:morning`.

Likely files:

- `noam_coach/bot/assistant.py`
- `noam_coach/bot/callback_menu.py`
- `tests/regression/test_qa_session_20260705_fixes.py`

Verification:

```bash
python -m pytest tests/regression/test_qa_session_20260705_fixes.py -v
```

### 2. Pain-aware training

Purpose: make active pain affect exercise selection, alternatives, load
progression, and card warnings.

Likely files:

- `training_intelligence.py`
- `noam_coach/services/training.py`
- `noam_coach/bot/callback_session.py`
- `noam_coach/bot/workout.py`
- `noam_coach/bot/ui.py`
- `exercise_plans.py`
- `tests/regression/test_pain_aware_training.py`
- `tests/test_training_intelligence.py`

Verification:

```bash
python -m pytest tests/regression/test_pain_aware_training.py -v
python -m pytest tests/test_training_intelligence.py -v
```

### 3. Workout UX wording

Purpose: polish workout labels without changing training logic.

Likely files:

- `exercise_plans.py`
- `noam_coach/bot/ui.py`
- `noam_coach/bot/workout.py`
- `noam_coach/bot/workout_runtime.py`
- `tests/test_coach_bot_logic.py`

Verification:

```bash
python -m pytest tests/test_coach_bot_logic.py -v
```

### 4. Onboarding wording

Purpose: avoid overpromising medication learning and separate food preference
language from allergy/sensitivity language.

Likely files:

- `noam_coach/bot/onboarding.py`
- `questions.py`
- `tests/test_onboard_02_regression.py`
- `tests/test_questions.py`

Verification:

```bash
python -m pytest tests/test_onboard_02_regression.py -v
python -m pytest tests/test_questions.py -v
```

### 5. Nutrition / meal fixes

Purpose: keep meal corrections and preference memory separate from training
changes.

Likely files:

- `noam_coach/services/next_meal.py`
- `noam_coach/bot/meal_text.py`
- `noam_coach/bot/meals.py`
- nutrition-related tests under `tests/acceptance` and `tests/regression`

Verification:

```bash
python -m pytest tests/test_next_meal_callbacks.py -v
python -m pytest tests/regression/test_recording_20260628_re8.py -v
python -m pytest tests/acceptance/test_rec_next_meal_05.py -v
```

### 6. Product infrastructure

Purpose: CI, privacy, traceability, and regression reports.

Likely files:

- `.github/workflows/ci.yml`
- `docs/*`
- `scripts/*` (including `scripts/privacy_audit.py` and its coverage in
  `tests/test_privacy_tools.py`)
- `tests/test_decision_engine.py`
- `tests/test_prompt_builder.py`

Verification:

```bash
python -m compileall -q .
ruff check .
python -m pytest tests/test_decision_engine.py tests/test_prompt_builder.py -v
python -m pytest tests/test_privacy_tools.py -v
```

### 7. Mini App operational dashboard

Purpose: expose active pain, active session, and the current load decision in
the Mini App so support/debug does not require raw logs.

Likely files:

- `mini_api.py`
- `miniapp/static/app.js`
- `miniapp/static/styles.css`
- `miniapp/templates/index.html`
- `tests/test_mini_profile_api.py`
- `tests/test_miniapp_render.py`

Verification:

```bash
python -m pytest tests/test_mini_profile_api.py tests/test_miniapp_render.py -v
```

### 8. End-to-end life-scenario regression

Purpose: lock the full user journey (onboarding facts -> workout -> occupied
substitution -> pain report -> partial finish -> adapted next load -> meal
recommendation -> meal correction) as one wiring test.

Likely files:

- `tests/regression/test_e2e_life_scenario.py`
- `tests/regression/test_training_lifecycle_trace.py`

Verification:

```bash
python -m pytest tests/regression/test_e2e_life_scenario.py tests/regression/test_training_lifecycle_trace.py -v
```

## Final gate before any commit

Run:

```bash
git diff --cached --stat
git diff --cached --check
python -m pytest
```

Only commit when the cached diff contains exactly one slice above.
