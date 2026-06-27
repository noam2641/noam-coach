# Recording Issues

Issues discovered during recorded onboarding-flow reviews. Each batch is named
`REC-<AREA>-<BATCH>` and individual issues are numbered within the batch.

---

## REC-ONBOARD-02 — Onboarding flow review (2026-06-25)

Fourteen issues found by replaying a full onboarding session recording and
noting every place the UI was confusing, data was lost, or internal IDs leaked
to the user.

| ID | Title | Severity | Files changed |
|----|-------|----------|---------------|
| 02-01 | Structured import result | Medium | `health_import.py`, `noam_coach/services/health_jobs.py` |
| 02-02 | Health export freshness warning | Medium | `health_service.py`, `noam_coach/bot/workout.py` |
| 02-03 | Remove internal IDs from buttons | High | `user_model.py`, `onboarding.py`, `noam_coach/bot/onboarding.py`, `planning.py`, `targets.py` |
| 02-04 | Standardize fact confirmation actions | Medium | `user_model.py`, `noam_coach/bot/onboarding.py` |
| 02-05 | Persist onboarding across restart | High | `noam_coach/bot/onboarding.py` |
| 02-06 | Better stale-callback recovery | High | `noam_coach/bot/callback_router.py`, `conversation.py` |
| 02-07 | question_by_fact_key lookup | Low | `questions.py` |
| 02-08 | Medication/appetite note in routine extraction | Medium | `models.py`, `noam_coach/services/profile.py` |
| 02-09 | Enhanced compute_readiness return | Medium | `user_model.py` |
| 02-10 | Safety profile label fix | Low | `user_model.py` |
| 02-11 | Centralized display_label in targets | Low | `targets.py` |
| 02-12 | Plan-generation eligibility with complete_missing | Medium | `noam_coach/bot/callback_plans.py` |
| 02-13 | Daily status freshness + provisional explanation | Medium | `noam_coach/bot/workout.py` |
| 02-14 | Home hint shows next onboarding action | Low | `noam_coach/bot/ui.py` |

### Details

**02-01 Structured import result** — `ImportSummary` now tracks `weight_records`,
`activity_records`, and `invalid_records` separately. `HealthImportOutcome` gained
`updated`, `invalid`, `source_file`, `total_stored`, `import_started`, and
`import_completed` fields. Success text renders structured Hebrew sections.

**02-02 Health export freshness warning** — Five-level freshness classification
(`current`/`recent`/`stale`/`very_stale`/`unavailable`) with `health_export_freshness()`,
`freshness_warning_text()`, and `is_today_activity_available()`. Daily status
includes a freshness section when data is stale.

**02-03 Remove internal IDs from buttons** — Centralized `FACT_DISPLAY_LABELS`
dict (30+ entries) and `display_label()`/`display_value()` functions in
`user_model.py`. All onboarding confirmation buttons, pattern items, plan
missing-info screens, and target explanations now show friendly Hebrew labels
instead of raw fact keys like `workout_pattern`.

**02-04 Standardize fact confirmation actions** — Seven `CONFIRM_*` constants
(`inferred`/`confirmed`/`corrected`/`not_applicable`/`deferred`/`stale`/`invalid`).
New functions: `defer_fact()`, `mark_not_applicable()`, `fact_confirmation_status()`.
Pattern confirmation buttons include a "later" option that defers without blocking.

**02-05 Persist onboarding across restart** — `resume_onboarding_after_restart()`
reads the persisted stage from the database and re-renders the current step.
Stale-callback and idle-flow handlers call this to recover mid-onboarding users.

**02-06 Better stale-callback recovery** — When a callback's version/flow_id
doesn't match the active flow, the handler now: answers immediately, logs the
stale event, removes the old keyboard, and routes to the current active flow
or resumes onboarding — instead of showing a generic home menu.

**02-07 question_by_fact_key lookup** — `question_by_fact_key(fact_key)` finds the
`Question` that populates a given fact key, enabling direct routing from
missing-info screens to the relevant question.

**02-08 Medication/appetite note** — `RoutineExtraction` gained a
`medication_appetite_note` field. The AI prompt no longer infers diagnosis or
dosage. The routine confirmation screen displays the note when present.

**02-09 Enhanced compute_readiness** — Returns `deferred`, `stale`,
`not_applicable`, `missing_labels` (friendly Hebrew), and `label` in addition
to the existing `score`/`ready`/`missing`/`present` fields.

**02-10 Safety profile label** — Changed from English to "שאלון בטיחות". Program
center shows "הושלם"/"חסר מידע" instead of a misleading percentage.

**02-11 Centralized display_label in targets** — `targets.py` delegates to
`user_model.display_label()` for provisional-target missing-input labels.

**02-12 Plan-generation eligibility** — Missing-info screen groups fields by
category with a "complete now" button that routes to the first missing required
field via `planv2:complete_missing` callback.

**02-13 Daily status freshness** — `build_daily_status()` includes a health
freshness section and explains provisional targets when present.

**02-14 Home hint** — `_home_hint()` checks onboarding status and profile
completeness to suggest the most useful next action.

### Regression tests (REC-ONBOARD-02)

41 tests in `tests/test_onboard_02_regression.py` covering all 14 issues:
ImportSummary fields, HealthImportOutcome fields, freshness constants/warnings,
display labels/values, patterns_text display_label, CONFIRM_* constants,
fact_confirmation_status, defer_fact, mark_not_applicable, onboarding
persistence, stale-callback extraction, question_by_fact_key, enhanced
compute_readiness, safety profile label, targets display_label, and
no-internal-IDs-in-buttons cross-checks.

---

## REC-PLAN-MEAL-03 — Plan, meal and conversation review (2026-06-27)

Eighteen issues found by replaying a full plan-building, meal-logging and
daily-status session. Covers P0 crashes, data precedence, profile display,
workout proposals, meal analysis, daily status and conversational issues.

| ID | Title | Severity | Files changed |
|----|-------|----------|---------------|
| 03-01 | Dietary restriction answer crashes | P0 | `noam_coach/bot/onboarding.py` |
| 03-02 | Error recovery loses active question | P0 | `noam_coach/bot/onboarding.py` |
| 03-03 | Plan selection captures unrelated messages | P0 | `noam_coach/bot/meal_text.py` |
| 03-04 | Meal-status question not routed | P0 | `assistant.py`, `noam_coach/bot/assistant.py` |
| 03-05 | Don't re-ask known information | P0 | `noam_coach/bot/onboarding.py` |
| 03-06 | Training days conflict resolution | P0 | `coach_intelligence.py` |
| 03-07 | Raw internal values in profile display | P1 | `noam_coach/bot/onboarding.py` |
| 03-08 | Readiness percentages not explainable | P1 | `noam_coach/bot/onboarding.py` |
| 03-09 | Proposals use unconfirmed data | P1 | `planning.py` |
| 03-10 | User text not authoritative over image | P1 | `noam_coach/services/profile.py` |
| 03-11 | Meal totals != item totals | P1 | Already correct (verified) |
| 03-12 | Oil removal not revision-safe | P1 | `meal_intelligence.py`, `noam_coach/services/profile.py`, `noam_coach/bot/meal_text.py` |
| 03-13 | Restriction contradiction in meals | P1 | `noam_coach/bot/meals.py` |
| 03-14 | Meal save missing confirmation/undo | P1 | `noam_coach/bot/callback_meals.py` |
| 03-15 | Daily totals include pending meals | P1 | Already correct (verified) |
| 03-16 | Next action has no matching button | P1 | `noam_coach/bot/ui.py`, `noam_coach/bot/callback_menu.py` |
| 03-17 | Redundant question frustration unhandled | P1 | `assistant.py`, `noam_coach/bot/assistant.py` |
| 03-18 | Unstructured error recovery | P1 | `noam_coach/bot/callback_router.py`, `noam_coach/bot/meals.py`, `noam_coach/bot/meal_text.py` |

### Details

**03-01 Dietary restriction parsing** — Free-text answers to diet restriction
questions are parsed into structured food items via `_parse_dietary_answer()`.
A follow-up inline keyboard lets the user classify each item as preference,
intolerance, sensitivity, or allergy. The `qa:diet_type:*` callback handler
moves items between `diet_restrictions` and `allergies` facts as needed.

**03-02 Error recovery preserving active question** — `handle_onboarding_text`
wraps `record_answer()` in a try/except. On failure, the pending question is
NOT cleared; the user sees retry/skip/menu buttons. Event type:
`question_answer_failed`.

**03-03 Plan selection passthrough** — The `selection_flow` handler in
`handle_text_message` now only consumes exact "1"/"2"/"3" inputs. Other text
falls through to the intent router with `unrelated_message_released_to_intent_router`
event logging.

**03-04 Meal status intent** — Added `meal_status` to the Action literal type,
SYSTEM_PROMPT, and keyword fallback. Handler `_handle_meal_status_action` queries
today's saved meals and shows a summary with add/status/menu buttons.

**03-05 Check existing data before asking** — `ask_next_question` checks for an
existing non-gap fact before displaying the question. If found, shows the
existing value with source label and confirm/update buttons.

**03-06 Training days conflict resolution** — `profile_conflicts()` now also
compares plan frequency with declared and observed training frequency. New
conflict key `plan_vs_actual_frequency` surfaces when the plan and reality
diverge significantly.

**03-07 Profile display formatting** — Centralized `_ENUM_DISPLAY_MAP` maps
all internal enum values to Hebrew labels. `_format_fact_value()` handles
dicts (work_schedule as time range), lists, numeric facts with units, and
falls back to stripping underscores from unknown values.

**03-08 Explainable readiness** — `render_smart_plan_hub` now shows "מוכן"
when ready or "חסר: X, Y" with up to 3 missing field labels when not ready,
mirroring the pattern in `build_profile_text`.

**03-09 Proposals use confirmed data only** — `generate_candidates()` checks
critical facts for confirmation status. Unconfirmed facts add an assumption
note to each candidate: "מבוסס על הערכה לא מאושרת: X, Y".

**03-10 User text authoritative** — The system prompt for
`reanalyze_meal_with_text_and_image` now explicitly states: "THE USER'S TEXT
IS AUTHORITATIVE. If the text contradicts the image analysis, always trust
the text."

**03-11 Meal totals consistency** — Verified already correct: `MealAnalysis.totals()`
sums items directly, and both `persist_meal()` and `render_meal()` use it as
the single source of truth. No changes needed.

**03-12 Oil revision-safe** — Added `_REMOVAL_PATTERNS` and
`apply_item_removal_correction()` to `meal_intelligence.py`. "בלי שמן" / "ללא שמן"
is now parsed as an item removal. `reanalyze_meal_with_text_and_image` accepts
`locked_corrections` parameter, and the correction flow passes previous
corrections to subsequent re-analyses so removed items stay removed.

**03-13 Restriction contradiction detection** — `render_meal()` checks each
meal item against the user's `diet_restrictions` and `allergies` facts.
Matching items show a warning: "⚠️ <item> — רשום אצלך כהימנעות: <restriction>".

**03-14 Meal save confirmation with undo** — Both `approve_meal:` and
`force_approve_meal:` callback handlers now show a meal-specific confirmation
("נשמר ✅ <name> — X קל׳, Y ג׳ חלבון") with undo and edit buttons.

**03-15 Daily totals from saved meals** — Verified already correct: all daily
calorie/protein queries use the `meals` table exclusively; the `approvals`
table is never included in daily totals.

**03-16 Next action button** — Added `home_keyboard_for_user(user_id)` async
function that prepends the `next_best_action` callback as a button. The
`menu:home` handler uses it instead of the static `home_keyboard()`.

**03-17 Redundant question challenge** — Added `redundant_question_challenge`
action to the classifier. Handler `_handle_redundant_question_challenge`
checks the active question flow, shows the existing value with source, and
offers confirm/update/skip buttons.

**03-18 Structured error recovery** — Added `event_log.append_event` calls to
three error handlers: `callback_error` in callback_router, `photo_analysis_error`
in meals, and `meal_correction_error` in meal_text. Each logs error type,
context, and callback/correction data for diagnostics.

### Regression tests (REC-PLAN-MEAL-03)

55 tests in `tests/test_rec_plan_meal_03.py` covering all 18 issues:
dietary parsing (6), error recovery (1), selection passthrough (2),
meal status intent (4), existing data check (2), training conflicts (2),
profile display (10), explainable readiness (1), confirmed data proposals (1),
user text authoritative (1), meal totals consistency (2), oil removal (4),
restriction contradiction (1), meal save confirmation (2), daily totals (1),
next action button (2), redundant question (4), structured error recovery (3),
system prompt completeness (3), keyword fallback ordering (4).
## REC-PROGRAM-04 — Program and meal recording review (2026-06-27)

Independent audit and completion evidence is in `docs/CODEX_AUDIT_REC_PROGRAM_04.md`.

Key production fixes:

- Canonical training availability is used by workout candidate generation, Telegram profile/program views, and Mini App profile/dashboard payloads.
- Nutrition plan protein options now receive canonical dietary restriction IDs.
- Dietary matching handles `nut`, `nutmeg`, `coconut milk`, and explicit negation phrases such as `contains no nuts`, `nut-free`, `ללא אגוזים`, and `בלי חלב`.
- Mini App inline event handlers were removed to satisfy CSP, server text is escaped before HTML rendering, and weekday IDs now match backend `0=Sunday`.
- `/mini/upload` uses the Health upload body limit rather than the generic small API body limit.

Verification: compileall passed, Ruff passed, pytest `557 passed, 3 warnings`, evaluations `33/33`, preflight passed, startup smoke passed, release ZIP has 169 files and 0 forbidden entries.
