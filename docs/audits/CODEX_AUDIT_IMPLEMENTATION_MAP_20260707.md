# Codex Audit Implementation Map - 2026-07-07

This is the working map for the corrected 29-image audit. It records ownership,
storage, verified coverage, and remaining gaps before continuing deeper changes.

## Required Work Order

1. Telegram-safe infrastructure.
2. State and parsing by active flow step.
3. Data provenance and confidence.
4. Onboarding confirmation and dedupe.
5. Nutrition: daily status and next meal.
6. Training: full plans, live sessions, progression, substitutions.

## Ownership Map

### Telegram Callbacks

- `noam_coach/bot/callback_router.py`: central callback dispatcher, stale screen recovery, duplicate tap debounce.
- `noam_coach/bot/callback_menu.py`: menu, goals, Health, flags, home navigation.
- `noam_coach/bot/callback_meals.py`: meal callbacks, duplicate meal actions, edits, undo.
- `noam_coach/bot/callback_plans.py`: plan and workout setup callbacks.
- `noam_coach/bot/callback_session.py`: live workout session callbacks.
- `noam_coach/bot/checkins.py`: check-in callbacks.
- `noam_coach/bot/ui.py`: `safe_answer_callback`, `safe_edit`, keyboards and rendering helpers.
- `noam_coach/services/telegram_errors.py`: stale/transient Telegram error classification.

### State, Onboarding, Parsing

- `conversation.py`: `active_flow`, step, payload, flow_id, version, callback encoding.
- `questions.py`: question definitions and answer normalization.
- `user_model.py`: facts, history, profile audit, readiness.
- `noam_coach/bot/onboarding.py`: onboarding flow, profile confirmation, plan builder.
- `noam_coach/services/core.py`: shared flow helpers.
- `data_quality.py`: readiness and data gap checks.

### Apple Health And Confidence

- `health_import.py`: Apple Health XML/ZIP parsing.
- `health_service.py`: Health row upsert and profile sync.
- `routine.py`: workout, sleep, steps and routine inference.
- `noam_coach/services/health_jobs.py`: import flow, import summary, Health confirmation wizard.
- `noam_coach/services/health_quality.py`: data quality report and shared export freshness policy.
- `noam_coach/api/health_routes.py`: Health API ingestion routes.

### Nutrition

- `noam_coach/services/nutrition_context.py`: daily nutrition context and provenance.
- `noam_coach/services/next_meal.py`: "what to eat now", meal budget, restrictions and feedback.
- `noam_coach/services/dietary_restrictions.py`: allergies, sensitivities, avoidances and preferences.
- `noam_coach/services/meal_validation.py`: meal validation.
- `noam_coach/bot/meals.py`: meal analysis and persistence.
- `noam_coach/bot/meal_text.py`: text routing for meals and stale question escape.
- `noam_coach/bot/callback_meals.py`: next-meal and meal callback actions.

### Training

- `planning.py`: workout plan generation, repair, activation and quality gates.
- `exercise_plans.py`: templates and exercise data.
- `training_intelligence.py`: training adaptation and progression support.
- `noam_coach/services/training.py`: load recommendation and pain-aware progression.
- `noam_coach/bot/workout.py`: workout rendering and set persistence.
- `noam_coach/bot/workout_runtime.py`: live workout session state and UI.
- `noam_coach/bot/callback_session.py`: live session callback actions.
- `noam_coach/bot/callback_plans.py`: split and plan setup callbacks.

## Storage Map

- `user_facts`: profile facts, source, confidence, confirmation and validity.
- `user_fact_history`: profile fact change history.
- `active_flow`: single active flow, step, payload, version and flow id.
- `health`: Apple Health samples.
- `routine_profile`: computed routine profile.
- `goal_versions`: provisional/current/superseded goal versions.
- `meals`, `meal_items`, `meal_fingerprints`: meals, items and meal idempotency.
- `plan_versions`, `active_plans`: planned workout programs.
- `sessions`, `sets`: performed workouts and performed sets.
- `medical_constraints`: pain, safety and medical limits.
- `product_events`, `analytics_events`, `audit`: event trail and debugging evidence.

## Completed In Current Cleanup Slice

- Weekday display is centralized in `noam_coach/services/weekdays.py::weekday_labels_he`.
- Health export freshness is centralized in `noam_coach/services/health_quality.py::export_freshness_status`.
- Health import stale warning now uses the same freshness policy as the quality report.
- Onboarding and Health wizard use the same weekday label helper.
- Added regression coverage for the shared freshness boundary: 7 days is fresh, 8 days is stale.
- Health confirmation wizard now renders through `safe_edit`, so stale edit targets fall back to a new reply instead of failing the flow.
- Manual calorie target entry now rejects time ranges such as `00:20-06:50` as time-like input and keeps the user in the calorie step.
- Profile audit rows now expose canonical `source_kind` (`user`, `apple_health`, `inferred`, `default`, `unknown`) and `approved_status` in addition to the legacy source/approved fields.
- User fact freshness now has one implementation path: legacy `is_fact_fresh` delegates to `fact_is_fresh`.

## Verified Commands

- `python -m ruff check noam_coach/services/health_quality.py noam_coach/services/health_jobs.py noam_coach/services/weekdays.py noam_coach/bot/onboarding.py tests/regression/test_re13_health_quality.py`
- `python -m pytest -q`
- `python -m pytest tests/test_onboard_02_regression.py tests/test_questions.py tests/regression/test_re10_04_health_wizard.py tests/test_telegram_lifecycle.py tests/regression/test_re13_health_quality.py -q`

Both passed after the cleanup slice.

## Remaining Gaps By Priority

### Telegram-Safe

- Verify every callback path is covered by central `safe_answer_callback` at the start or by router-level ACK.
- Broaden `safe_edit_or_reply` behavior where screens still rely on edit-only behavior.
- Add or verify idempotency for sensitive actions beyond local mechanisms.
- Confirm polling/network transient errors never send user-facing failure messages.

### State And Parsing

- Ensure numeric and time-like answers are accepted only in the matching `active_flow.step`.
- Guard `00:20-06:50` from being saved as calories or other numeric fields.
- Keep allergies, sensitivities, avoidances and preferences separate.
- Ensure "no allergies" is stored and dedupes future allergy questions.

### Data Confidence

- Promote profile audit as the shared source for `field/source/confidence/approved/freshness/action_required`.
- Extend profile audit consumers to use `source_kind` and `approved_status` instead of interpreting internal source names.
- Confirm Health-derived values never override confirmed user values.
- Keep low-confidence sleep and weight signals out of strong plan decisions.

### Nutrition

- Daily status still needs explicit confidence/provenance for every displayed number.
- "What to eat now" needs continued verification for bedtime, remaining protein and restrictions.

### Training

- `TrainingProfile` and `ExerciseCatalog` are not full explicit data models yet.
- `sets` does not yet store every requested per-set pain/rest/note field.
- Split request parsing, especially ABC vs exercise edit, needs continued hardening.
- Substitution needs full movement-pattern and contraindication coverage.
