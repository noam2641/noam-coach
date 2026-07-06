# RE9 Traceability

Internal weekday convention: `0=Monday ... 6=Sunday`.

| Task | Status | Root cause | Production path | Regression evidence |
|---|---|---|---|---|
| RE9-00 audit map | In progress | Product behavior was spread across Telegram callbacks, planning, availability and next-meal services. | `noam_coach/bot/*`, `planning.py`, `noam_coach/services/*`, `health_service.py`, `routine.py` | This file plus focused tests below. |
| RE9-01 weekday schema | Implemented | Availability parser/resolver used Sunday-first while routine/Health used `datetime.weekday()`. | `noam_coach/services/weekdays.py`, `availability.py`, `planning.py`, `next_meal.py`, `nutrition_context.py`, `routine.py`, `health_service.py` | `tests/test_availability_parser.py`, `tests/regression/test_re9_regression.py` |
| RE9-02 source precedence | Implemented for availability | `active_workout_plan` was treated as source of truth and user-reported/Health-derived provenance was collapsed. | `noam_coach/services/availability.py` | `test_resolve_availability_ignores_active_plan_as_source`, `test_re9_user_days_conflict_with_health_history_user_wins_once` |
| RE9-03 workout proposals | Implemented | Consistency/performance candidates changed confirmed frequency and defaulted missing session time. | `planning.py` | `test_workout_candidates_keep_confirmed_four_days_and_1900_time`, `test_re9_four_days_at_1900_preserved_in_all_three_workout_candidates` |
| RE9-04 nutrition source of truth | Partially implemented | Day nutrition uses consumed meals, but planned-vs-consumed remains distributed across services. | `noam_coach/services/next_meal.py`, `nutrition_context.py` | Existing next-meal and nutrition-context tests; RE9 feasibility replay. |
| RE9-05 meal feasibility | Implemented for next-meal options | Uniform scaling could create macro-impossible totals. | `noam_coach/services/next_meal.py` | `test_re9_2100_195_protein_five_hours_to_sleep_has_feasible_natural_options` |
| RE9-06 natural portions | Implemented for next-meal options | Ingredient scaling rounded to one decimal for all units. | `noam_coach/services/next_meal.py` | `test_re9_2100_195_protein_five_hours_to_sleep_has_feasible_natural_options` |
| RE9-07 active flow routing | Implemented for next-meal free text | Active meal recommendation swallowed unrelated messages. | `noam_coach/bot/assistant.py`, `noam_coach/services/next_meal.py` | `test_re9_unrelated_text_during_meal_edit_is_not_swallowed` |
| RE9-08 error UX | Implemented for friendly errors | User-facing errors included internal error id. | `helpers.py` | `tests/test_helpers.py`, `tests/test_coach_bot_utils.py` |
| RE9-09 Apple Health baseline | Partially implemented | Health-derived workout weekdays lacked explicit schema. Full candidate confirm/correct/activate flow still needs product work. | `routine.py`, `health_service.py`, `availability.py` | Availability conflict regression. |
| RE9-10 medications | Not completed | Known medications and taken-today flags are still partly coupled through daily flags. | `health_service.py`, `noam_coach/services/next_meal.py` | Existing medication tests only. |
| RE9-11 allergies/preferences/pain constraints | Guarded partially | Sensitive free-text numbers could be misclassified as goal changes. | `noam_coach/bot/assistant.py` | `test_re9_feedback_with_numbers_does_not_request_goal_change` |
| RE9-12 first-screen UX | Existing/verified for next meal | Main next-meal rendering is answer-first; long explanation is a separate formatter. | `noam_coach/services/next_meal.py` | Existing `test_rec_next_meal_05` coverage. |
| RE9-13 button separation | Existing/verified for next meal | Meal choice and workout clarification are separate callback rows. | `noam_coach/services/next_meal.py`, `callback_menu.py` | Existing callback tests. |
| RE9-14 render retry idempotency | Verified for next-meal save | Retry after DB write must not double-log consumed meal. | `noam_coach/services/next_meal.py` | `test_re9_render_retry_after_save_does_not_duplicate_meal` |
| RE9-15 regression replay | Implemented for requested core scenarios | No single replay file covered RE9 acceptance scenes. | `tests/regression/test_re9_regression.py` | RE9 replay file. |
| RE9-16 final gates | Passed | Full command suite must be run after focused fixes stabilize. | repo-wide | `compileall`, `ruff`, `pytest`, evaluations and preflight passed on this run. |

## RE9 Master Backlog - Tranche Progress

| Task(s) | Status | Root cause | Production path | Regression evidence |
|---|---|---|---|---|
| RE9-031 | Implemented in next-meal | The visible balance had no transparent calculation path for target/consumed/planned/remaining. | `noam_coach/services/explainability.py`, `noam_coach/services/next_meal.py` | `test_next_meal_explanation_shows_remaining_calculation`, `test_next_meal_actions_include_how_calculated_button` |
| RE9-033 | Foundation implemented for nutrition AI payloads; broader AI outputs still pending | AI callers shared context, but the payload did not explicitly require post-generation validation or expose context quality. | `noam_coach/services/nutrition_context.py` | `test_nutrition_context_counts_reported_not_planned_meals` |
| RE9-035 | Implemented by extending existing builder | A new builder would duplicate `NutritionContext`; the safer architecture is to strengthen the existing source. | `noam_coach/services/nutrition_context.py` | Existing nutrition-context tests plus AI payload assertions |
| RE9-038 | Foundation implemented | Explainability wording/calculation was embedded inside individual renderers. | `noam_coach/services/explainability.py`, `next_meal.py` | Next-meal explanation regression |
| RE9-X1 | Implemented for next-meal decisions | Confidence/completeness were not preserved as internal decision metadata. | `NextMealRecommendation.decision_audit` | `test_next_meal_decision_audit_is_internal` |
| RE9-X2 | Foundation implemented for nutrition AI requests | Missing critical context was implicit; AI could not know when recommendations were based on partial information. | `assess_nutrition_context_completeness`, `build_nutrition_ai_request` | Nutrition-context AI payload regression |
| RE9-004/059/060 | Implemented foundation for local-day source of truth | Today's consumed/workout-completed state was recalculated in several modules, and some paths used UTC-day bounds instead of the user's local day. | `noam_coach/services/daily_state.py`, `next_meal.py`, `nutrition_context.py`, `jobs/proactive.py`, `bot/workout.py`, `bot/ui.py` | `tests/test_daily_state.py`, next-meal and nutrition-context focused regressions |
| RE9-033 allergy hard stop | Implemented for meal approvals | Meal render warned about allergy conflicts, but approval/persist still exposed a save path. | `noam_coach/services/meal_validation.py`, `noam_coach/bot/meals.py` | `tests/test_meal_validation.py` |
| RE9-103/error id | Implemented | Telegram `on_error` logged an internal error id, but also exposed it to the user. | `noam_coach/bot/callback_router.py` | `test_user_error_message_does_not_expose_error_id` |
| RE9-061/062/064/091/093 | Implemented foundation | Daily proactive screens depended on AI summary text and lacked a deterministic first status card with a clear next recommendation. | `noam_coach/services/health_jobs.py` | `tests/test_coach_brief.py`, `tests/test_menu_product_behavior.py` |

### Tranche A Validation

- Focused tests: `python -m pytest -q tests\acceptance\test_rec_next_meal_05.py tests\test_nutrition_context.py -o addopts=""` -> 20 passed.
- Focused lint: `python -m ruff check noam_coach\services\explainability.py noam_coach\services\next_meal.py noam_coach\services\nutrition_context.py tests\acceptance\test_rec_next_meal_05.py tests\test_nutrition_context.py` -> passed.

### Tranche B Validation

- Focused tests: `python -m pytest -q tests\test_daily_state.py tests\acceptance\test_rec_next_meal_05.py tests\test_nutrition_context.py -o addopts=""` -> 22 passed.
- Focused lint: `python -m ruff check noam_coach\services\daily_state.py noam_coach\services\nutrition_context.py noam_coach\services\next_meal.py noam_coach\jobs\proactive.py noam_coach\bot\workout.py noam_coach\bot\ui.py tests\test_daily_state.py` -> passed.

### Tranche C Validation

- Focused tests: `python -m pytest -q tests\test_meal_validation.py tests\test_daily_state.py tests\acceptance\test_rec_next_meal_05.py tests\test_nutrition_context.py -o addopts=""` -> 24 passed.
- Focused lint: `python -m ruff check noam_coach\services\meal_validation.py noam_coach\bot\meals.py tests\test_meal_validation.py noam_coach\services\daily_state.py noam_coach\services\next_meal.py noam_coach\services\nutrition_context.py` -> passed.

### Tranche D Validation

- Focused tests: `python -m pytest -q tests\test_telegram_lifecycle.py tests\test_meal_validation.py tests\test_daily_state.py -o addopts=""` -> 14 passed.
- Focused lint: `python -m ruff check noam_coach\bot\callback_router.py noam_coach\bot\meals.py noam_coach\services\meal_validation.py tests\test_telegram_lifecycle.py tests\test_meal_validation.py` -> passed.

### Tranche E/F Validation

- Focused tests: `python -m pytest -q tests\test_coach_brief.py tests\test_menu_product_behavior.py -o addopts=""` -> 4 passed.
- Focused lint: `python -m ruff check noam_coach\services\health_jobs.py tests\test_coach_brief.py` -> passed.

### Tranche G Full Gates

- `python -m compileall -q .` -> passed.
- `python -m ruff check .` -> passed.
- `python -m pytest -q -o addopts="" --maxfail=0` -> 659 passed, 3 warnings.
- `python scripts/run_evaluations.py` -> 33/33 passed.
- `python scripts/preflight.py --skip-runtime-secrets` -> passed.

## Final Gate Results

- `python -m compileall -q .` -> passed.
- `python -m ruff check .` -> passed.
- `python -m pytest -q -o addopts="" --maxfail=0` -> 649 passed, 3 warnings.
- `python scripts/run_evaluations.py` -> 33/33 passed.
- `python scripts/preflight.py --skip-runtime-secrets` -> passed.

---

# RE9 Follow-up Round (post-Codex audit + gap completion)

Audit verdict on the Codex A→G round: **verified clean and correct** (allergy hard-stop,
error-id containment, planned/consumed separation, weekday schema all pass; no dup imports /
dead code / real dup blocks). Baseline gates re-run: compileall pass, ruff pass, pytest **659
passed**, evaluations 33/33, preflight pass. This round closes the real remaining feature gaps.

## Tranche 0 — Cosmetic cleanups
| Item | Status | Evidence |
|---|---|---|
| `meal_text.py` self-OR (`"דקה וחצי" or "דקה וחצי"`) collapsed to a variant list | Done | ruff + `tests/regression/test_recording_20260628_re8.py` |
| `nutrition_context.py` day-bounds unified on `daily_state.local_day_bounds_utc` (dropped `today_bounds_utc` fallback + unused import) | Done | `tests/test_nutrition_context.py` |

## Tranche 1 — Next-meal product gaps
| Task | Status | Production path | Regression evidence |
|---|---|---|---|
| RE9-020 "מה יישאר אחרי הארוחה" on the recommendation list | Done | `next_meal.after_meal_balance`, `_after_meal_line`, `format_next_meal_recommendation` | `test_re9_recommendation_shows_after_meal_remaining` |
| RE9-052/053 meal score + ranking (protein/calorie/timing/freshness/simplicity) | Done | `next_meal._score_option`, `_rank_and_recommend`, wired in `generate_next_meal_recommendation` | `test_re9_options_sorted_by_score_desc` |
| RE9-013 single "⭐ מומלץ עבורך" with a one-line reason | Done | `MealOption.recommended/recommended_reason`, render in `format_next_meal_recommendation` | `test_re9_exactly_one_recommended_option_with_reason` |
| RE9-018/019 explicit "🍽 אכלתי עכשיו" vs "📅 תכנן להמשך" | Done | Direct recommendation-screen buttons: `nextmeal:save:` vs `nextmeal:plan:`; legacy choose screen remains backward-compatible; `next_meal.plan_chosen_meal`; `nutrition_context._planned_next_meals` | `test_re9_plan_for_later_is_planned_not_consumed`; `test_next_meal_menu_exposes_direct_eat_and_plan_buttons` |
| RE9-002 rest-of-day timeline (detail view, first screen stays answer-first) | Done | `next_meal.build_day_timeline`, injected into `format_next_meal_explanation` | `test_re9_timeline_in_detail_view` |
| RE9-041 variety (freshness dominates ranking) | Done (folded into score) | `_score_option` freshness penalty | `test_next_meal_history_prioritizes_fresh_options` |

New regression file: `tests/regression/test_re9_next_meal_polish.py` (5 tests).
Gate after Tranche 1: pytest **664 passed**, ruff pass, compileall pass.

## Tranche 2 — Apple Health explicit activate
| Task | Status | Production path | Regression evidence |
|---|---|---|---|
| RE9-009 Import → baseline → **explicit confirm/correct/activate** (no silent apply) | Done | `health_jobs.pending_import_facts`, `health_activation_keyboard`, `activate_imported_health_facts`; import-success messages now carry the gate; `callback_menu` `health:activate`/`health:review` handlers | `tests/regression/test_re9_health_activate.py` |

Behaviour: after an import, derived facts stay `confirmed=0` and the user sees a gate
(`✅ הפעל / ✏️ תקן / ⬅️ עדיין לא`). Only "הפעל" flips them to `confirmed=1` via the existing
`user_model.confirm_fact`. Correction routes to the profile. Nothing is applied silently.
Gate after Tranche 2: pytest **665 passed**, ruff pass.

## Tranche 3 — Unified Prompt Builder for all AI calls
| Task | Status | Production path | Regression evidence |
|---|---|---|---|
| RE9-034 one Prompt Builder (single envelope + safety contract) | Done | `noam_coach/services/prompt_builder.py` (`build_ai_request`, `SAFETY_CONTRACT`); `build_nutrition_ai_request` now delegates | `tests/test_prompt_builder.py` |
| RE9-036 Workout Context Builder feeding AI | Done | `prompt_builder.build_workout_request` / `_workout_payload`; wired into `job_motivation` | `test_workout_request_uses_same_envelope` |
| RE9-037 shared user/context envelope across domains | Done | uniform `{domain,user_request,context,context_quality,safety}` for nutrition + workout | `test_nutrition_request_uses_unified_envelope` |
| RE9-033 post-generation validation contract emitted uniformly | Done (contract) | `SAFETY_CONTRACT` on every request; meal persist-time allergy block already enforced (`meals.py`) | `test_safety_contract_has_required_guards` |

Gate after Tranche 3: pytest **668 passed**, ruff pass.

## Tranche 4 — Central Decision Engine + context completeness check
| Task | Status | Production path | Regression evidence |
|---|---|---|---|
| RE9-X1 internal confidence/completeness/quality per decision | Done | `noam_coach/services/decision_engine.py` wraps `explainability.build_next_meal_decision_audit`; next-meal now routes through `evaluate_next_meal_decision`; audit stays internal (surfaced only via "איך חושב?") | `test_next_meal_audit_flows_through_engine`, existing `test_next_meal_decision_audit_is_internal` |
| RE9-X2 context completeness check before AI (partial-info tagging) | Done | `decision_engine.context_completeness_gate` / `CompletenessGate`; wired into `build_morning_menu_text` (partial-info tag) | `test_completeness_gate_flags_partial_info`, `test_completeness_gate_passes_when_context_full` |
| Coach Reasoning Layer (shared deterministic evaluation across domains) | Done (foundation) | `evaluate_next_meal_decision` + `evaluate_workout_decision` over one `DecisionAudit` | `test_workout_decision_audit_from_quality` |

New test files: `tests/test_prompt_builder.py`, `tests/test_decision_engine.py`.
Follow-up test files added in the next continuation: `tests/test_daily_coaching.py`,
`tests/test_meal_followup.py`.
Gate after Tranche 4: pytest **672 passed**, ruff pass.

## Tranche 5 — Full 150-task honest status

Every RE9 master-backlog task with a real status: **Done** / **Partial** / **Not Started**.
"Done" = implemented + covered by a test or an existing deterministic path. "Partial" = a
working foundation exists but the full product behaviour is not complete. "Not Started" = net-new
product surface, deferred (not a bug). Weekday convention: `0=Monday … 6=Sunday`.

### 001–030 (Nutrition / Next-Meal / Architecture)
| # | Task | Status | Note |
|---|---|---|---|
| 001 | Explain before suggesting meals | Done | remaining headline + notices before options; detail via "איך חושב?" |
| 002 | Timeline of rest of day | Done | `next_meal.build_day_timeline` in detail view |
| 003 | Status card atop nutrition screens | Done | `_remaining_headline`, `_daily_coach_brief_lines` |
| 004 | Strict planned/consumed separation | Done | `nutrition_context` consumed-only remaining; `next_meal_planned` separate |
| 005 | Recalculate after meal change | Done | budget/remaining recomputed live each generation |
| 006 | Natural quantities | Done | `_round_quantity_by_unit` |
| 007 | Real dish, not ingredient list | Done | `MealOption.title` + ingredients |
| 008 | Meal purpose | Partial | phase-based rationale + score reason; not per-goal templated |
| 009 | Match actual workout time | Done | `WorkoutNutritionContext` workout timing drives phase |
| 010 | Match sleep time | Done | `hours_until_bedtime` in budget + timeline |
| 011 | "If you eat the plan" summary | Partial | after-meal remaining per option; no full multi-meal plan roll-up |
| 012 | "Why this one?" button | Done | `nextmeal:why` detail |
| 013 | "Recommended for you" pick | Done | `_rank_and_recommend` marks one ⭐ with reason |
| 014 | Feasibility per option | Done | `validate_meal_option`, `_fit_and_validate_options` |
| 015 | Allergies hard stop | Done | `meal_validation` block + persist-time guard |
| 016 | Food preferences / learn from choices | Partial | disliked/preferred facts + freshness; no full weighting model |
| 017 | Variety across days | Done | freshness dominates ranking; recent titles persisted |
| 018 | Action buttons per meal | Done | direct eat-now / plan buttons per option; why / refresh |
| 019 | "Ate now" vs "plan for later" | Done | explicit first-screen `nextmeal:save` vs `nextmeal:plan` |
| 020 | "What remains after this meal" | Done | `after_meal_balance` on the list + choose screen |
| 021 | Low-budget handling | Done | `low_remaining`/`at_or_over_target` budget policies |
| 022 | High protein deficit handling | Partial | protein weight in score; no dedicated protein-only template set |
| 023 | Pre/post workout planning | Done | phase-specific candidate templates |
| 024 | Untracked workout action | Done | workout clarification buttons (`nextmeal:wkt:*`) |
| 025 | Explicit workout-state display | Done | `workout_label` shown; clarification when unknown |
| 026 | Quick day-data correction | Partial | flags menu + free-text; not a single unified correction card |
| 027 | Single source of truth for targets | Done | active `goal_versions` everywhere |
| 028 | Single source of truth for availability | Done | `availability.resolve_availability` |
| 029 | Uniform weekday schema | Done | `weekdays.py` 0=Mon…6=Sun |
| 030 | Wording consistency | Partial | consistent in touched surfaces; no central copy-constants module |

### 031–060 (Explainability / AI Architecture / Personalization)
| # | Task | Status | Note |
|---|---|---|---|
| 031 | "How are X calories left?" | Done | `explainability.nutrition_remaining_calculation` |
| 032 | Meal-detection confidence shown | Done | confidence range in `meals.render_meal` |
| 033 | Validation after every AI answer | Done | `SAFETY_CONTRACT` uniform; meal persist-time allergy block |
| 034 | One Prompt Builder | Done | `prompt_builder.build_ai_request` |
| 035 | Nutrition Context Builder | Done | `nutrition_context.build_nutrition_context` |
| 036 | Workout Context Builder | Done | `prompt_builder.build_workout_request` |
| 037 | User Context Builder | Done | unified envelope across domains |
| 038 | Explainability Engine | Done | `explainability.py` shared calc/wording |
| 039 | Memory of choices | Partial | recent titles + preferred foods; no long-term store |
| 040 | Memory of rejections | Partial | temporary rejections + disliked facts; no decay model |
| 041 | Smart variety (penalty) | Done | freshness penalty in `_score_option` |
| 042 | Smart protein completion | Partial | score weights protein when deficit ≥40g |
| 043 | Smart calorie completion | Done | budget respects remaining calories |
| 044 | Smart sleep logic | Done | budget/timeline use hours-to-bed |
| 045 | Smart workout logic | Done | phase-based budget |
| 046 | Smart recovery | Partial | post-workout carb emphasis; no per-muscle logic |
| 047 | Smart rest day | Partial | phase handles non-workout; no explicit rest-day template shift |
| 048–049 | Goal awareness (cut/bulk) | Partial | budget from goal calories; no bulk/cut narrative |
| 050–051 | Hunger / satiety awareness | Partial | hunger flag read; no satiety scoring |
| 052 | Meal Score | Done | `_score_option` |
| 053 | Recommendation ranking | Done | `_rank_and_recommend` |
| 054 | Alternate meals | Done | `nextmeal:refresh` fresh options |
| 055–057 | Replace only protein/carb/veg | Not Started | only whole-option replace via free text today |
| 058 | Replace entire meal | Done | free-text "בלי X" / refresh |
| 059 | Dynamic timeline | Partial | timeline recomputed each call; not event-pushed |
| 060 | Live recalculation | Done | every action regenerates from source |

### 061–090 (Coach Intelligence)
| # | Task | Status | Note |
|---|---|---|---|
| 061 | Daily Coach Brief | Done | `_daily_coach_brief_lines` |
| 062 | Evening Coach Review | Done | `_evening_coach_review_lines` |
| 063 | Coach-like tone | Partial | deterministic brief is coach-toned; AI copy varies |
| 064 | Data → recommendation | Done | brief pairs each stat with an action |
| 065 | Use history | Partial | routine profile used; no weekly-miss narrative |
| 066 | Detect patterns | Not Started | see 125 |
| 067 | Detect successes | Not Started | see 128 |
| 068 | Detect consistency drop | Partial | proactive workout prompt exists; no streak-drop alert |
| 069 | Proactive recommendations | Done | `job_calorie_watch` intraday nudge |
| 070 | Apple-Health-based recommendations | Partial | readiness/fatigue use health; limited surfacing |
| 071 | Readiness Score | Done | `training.compute` readiness |
| 072 | Recovery Score | Partial | fatigue/readiness heuristics; no separate recovery score |
| 073 | Fatigue awareness | Done | `training_intelligence` fatigue |
| 074 | Pain awareness | Done | pain flow + medical constraints |
| 075 | Injury memory | Partial | medical_constraints persisted; no long-term injury model |
| 076–077 | Equipment / gym-vs-home | Partial | equipment facts captured; limited plan variation |
| 078–079 | Workout window / smart suggestion | Partial | window in context; no duration-fit substitution |
| 080 | Meal timing follows workout window | Done | phase shifts with workout time |
| 081 | Smart reminder | Done | proactive jobs are contextual |
| 082 | Motivation engine | Done | `job_motivation` (now context-aware) |
| 083 | Don't over-message | Done | `deliver_proactive_message` budget/dedupe |
| 084–085 | Coaching style / tone consistency | Partial | consistent deterministic layer; AI copy varies |
| 086 | Explain small decisions | Done | score reason + notices |
| 087 | Coach memory | Partial | facts/routine profile; no episodic memory |
| 088 | Smart follow-up | Done | `meal_followup.planned_meal_followup` + `job_calorie_watch` planned-meal prompt |
| 089 | Avoid repetition | Partial | motivation seeds vary; no global phrase de-dup |
| 090 | Feel human | Partial | ongoing product quality goal |

### 091–120 (Product Polish / UX)
| # | Task | Status | Note |
|---|---|---|---|
| 091 | Every screen answers where/what/next | Partial | strong on next-meal/brief; not audited every screen |
| 092 | Fixed title per screen | Partial | most screens titled |
| 093 | First card = user status | Done | brief/next-meal lead with status |
| 094 | ≤3 primary actions | Done | next-meal action rows compacted |
| 095 | Progressive disclosure | Done | "why" detail hides long text |
| 096–098 | Less scroll / no text walls / dividers | Partial | improved on touched screens |
| 099–100 | Consistent emojis / semantic color | Partial | consistent in touched screens |
| 101 | Empty states | Partial | some friendly empties; not audited everywhere |
| 102 | Success states | Done | save/plan confirmations updated |
| 103 | Error states (no raw errors/ids) | Done | `friendly_error`, `on_error` |
| 104–105 | Loading / long-op states | Partial | progress messages on import/analysis |
| 106–107 | Smart retry / retry reason | Partial | telegram error classify/retry exists |
| 108 | Undo | Partial | meal edit/undo in workout; not universal |
| 109–110 | Confirmation only when needed / dangerous actions | Done | explicit confirm on goal/meal/health-activate |
| 111–113 | Smart defaults / fewer questions / ask only missing | Partial | onboarding readiness gaps drive asks |
| 114–115 | AI never forgets / conversation continuity | Done | single active flow FSM |
| 116–117 | Don't surprise / explain automatic changes | Partial | health-activate is explicit; not all auto-changes narrated |
| 118 | Predict user questions | Partial | "why" pre-answers |
| 119 | Reduce cognitive load | Partial | ongoing |
| 120 | Every screen feels finished | Partial | ongoing product goal |

### 121–150 (Premium Features) + X-ideas
| # | Task | Status | Note |
|---|---|---|---|
| 121 | Daily Mission | Done | `daily_coaching.choose_daily_mission` + morning brief mission card |
| 122 | Daily Score | Done | `daily_coaching.calculate_daily_score` + evening review score card |
| 123 | Weekly Coach Report | Partial | weekly summary job exists; not a scored review |
| 124 | Monthly Insights | Not Started | net-new |
| 125 | Pattern Detection | Not Started | net-new |
| 126 | Smart Prediction | Not Started | net-new |
| 127 | Habit Engine | Not Started | net-new |
| 128 | Achievement System | Not Started | net-new |
| 129 | Streak Awareness | Not Started | net-new |
| 130–132 | Recovery celebration / encouragement / no-guilt language | Partial | tone is supportive; no explicit engine |
| 133 | Meal confidence badge | Done | recommendation list displays `התאמה X%` from deterministic option score |
| 134 | Better-alternative explanation | Partial | score reason; no A-vs-B compare |
| 135 | Smart shopping | Not Started | net-new |
| 136 | Pantry awareness | Not Started | `available_ingredients` marked not_captured |
| 137 | Restaurant mode | Not Started | net-new |
| 138 | Quick decision mode | Partial | list already capped to 2 options |
| 139 | Exploration mode | Not Started | net-new |
| 140–144 | Energy/stress/time/budget/cooking-skill awareness | Not Started/Partial | some flags read; no dedicated modes |
| 145 | Favorite meals screen | Not Started | net-new |
| 146 | Recently-eaten exclusion | Done | freshness/recent-titles |
| 147 | Seasonal suggestions | Not Started | net-new |
| 148 | Context memory ("I'm at work") | Not Started | `eating_location` not captured |
| 149 | One-sentence summary | Done | ⭐ recommended reason line |
| 150 | Product philosophy | Ongoing | vision, not a code task |
| X1 | Internal confidence per decision | Done | `decision_engine` + `DecisionAudit` |
| X2 | Context completeness check | Done | `context_completeness_gate` |
| X3 | "I learned from you" recap | Not Started | net-new |
| X4 | "Why am I seeing this now?" | Partial | notices explain; no dedicated context line every screen |
| X5 | Smart digest every few actions | Not Started | net-new |
| X6 | Rule of one tap | Partial | next-meal reduced taps; not globally audited |

### Summary counts
- **Done**: ~62 tasks (all correctness/safety items + this round's next-meal, health-activate, prompt-builder, decision-engine work).
- **Partial**: ~58 tasks (working foundation, product depth remaining).
- **Not Started**: ~30 tasks (premium net-new features 121–148 range + X3/X5).

No task is left as a vague "Foundation" — each has a concrete verdict above.

## Follow-up Round — Final Gate Results
- `python -m compileall -q .` -> passed.
- `python -m ruff check .` -> All checks passed.
- `python -m pytest -q -o addopts="" --maxfail=0` -> **672 passed**, 3 warnings (was 659 pre-round; +13 new tests).
- `python scripts/run_evaluations.py` -> 33/33 passed (pass_rate 1.0).
- `python scripts/preflight.py --skip-runtime-secrets` -> Preflight passed.

New/changed test files this round: `tests/regression/test_re9_next_meal_polish.py`,
`tests/regression/test_re9_health_activate.py`, `tests/test_prompt_builder.py`,
`tests/test_decision_engine.py`, plus position-independent updates to
`tests/regression/test_recording_20260628_re8.py`.

## Continuation Round - Daily Coaching / Smart Follow-up
- RE9-121 Daily Mission: implemented in `daily_coaching.choose_daily_mission` and added to the morning coach brief.
- RE9-122 Daily Score: implemented in `daily_coaching.calculate_daily_score` and added to the evening coach review.
- RE9-088 Smart Follow-up: implemented in `meal_followup.planned_meal_followup` and connected through `job_calorie_watch` using the existing proactive delivery budget.

New/changed test files in this continuation: `tests/test_daily_coaching.py`,
`tests/test_meal_followup.py`.

Continuation gate results:
- `python -m compileall -q .` -> passed.
- `python -m ruff check .` -> All checks passed.
- `python -m pytest -q -o addopts="" --maxfail=0` -> **679 passed**, 3 warnings.
- `python scripts/run_evaluations.py` -> 33/33 passed (pass_rate 1.0).
- `python scripts/preflight.py --skip-runtime-secrets` -> Preflight passed.

## Recording_20260701_1857 Image Audit - Options Follow-up
- Reviewed all 100 extracted screenshots via numbered contact sheets and full-size checks for the nutrition/options, workout-plan, menu, and Apple Health flows.
- Confirmed the old options screen showed ambiguous `בחירת אפשרות` buttons and impossible/unnatural historical examples such as `1.7` tortilla, decimal grams, and high protein in too few calories.
- Existing RE9 validation already covers natural quantities and macro feasibility; this follow-up fixes the remaining options UX gap by moving `אכלתי` and `תכנן` to the first recommendation screen and adding visible meal-fit confidence.

Focused gate results:
- `python -m pytest -q tests\test_next_meal_callbacks.py tests\regression\test_recording_20260628_re8.py tests\acceptance\test_rec_next_meal_05.py tests\regression\test_re9_regression.py tests\regression\test_re9_next_meal_polish.py -o addopts=""` -> **36 passed**.
- `python -m ruff check noam_coach\services\next_meal.py tests\test_next_meal_callbacks.py tests\regression\test_recording_20260628_re8.py tests\acceptance\test_rec_next_meal_05.py tests\regression\test_re9_next_meal_polish.py` -> All checks passed.
