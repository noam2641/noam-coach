# Project Task Register — Noam Coach

This is the **canonical source of truth** for the product task history. It
consolidates the screenshot-derived backlog, the 22-task master specification,
and the development/audit reports into one engineering register. Raw source
documents are preserved under [`docs/archive/`](archive/) for historical
traceability.

Last consolidated: 2026-07-11. Verification baseline at consolidation: full
pytest suite `1093 passed, 0 failed`.

---

## Completed — Tasks 1–22

All 22 tasks are implemented, wired into the real runtime flow, and covered by
tests. Every task below was re-verified against the current repository during
the 2026-07-11 final audit (not accepted on commit message alone).

| # | Title | Intended behavior | Status | Commit(s) | Main test(s) |
|---|-------|-------------------|--------|-----------|--------------|
| 1 | Workout days from HealthKit history | Derive recommended training weekdays from real workout records (recency-weighted, window expansion, manual only when no pattern); never fill consecutive days. | COMPLETE | `080bf61` + health checkpoints | `test_re10_04_health_wizard.py`, `test_re12_wear_and_wizard.py` |
| 2 | Sleep from full HealthKit history | Analyze all sleep sources, normalize overlaps, recency-weight; watch-wear is not a hard filter; inferred schedule not shown as confirmed. | COMPLETE | health checkpoints | `test_re13_health_quality.py` |
| 3 | StepCount planning baseline | All StepCount sources, iPhone survives missing wear, local dates, exclude partial final day, 28-day window, reproducible diagnostics, numeric override; no hardcoded baseline. | COMPLETE | health checkpoints | `routine.py` step tests |
| 4 | Separate planning state | raw_imported / inferred / confirmed / user_overridden / pending / insufficient_data; summary uses confirmed/overridden; raw sleep records not called "nights"; stale warning once. | COMPLETE | health checkpoints | `test_post_import_menu_and_limitations.py` |
| 5 | Focused post-import completion menu | Reduced keyboard with "🎯 השלם את התוכנית שלי"; dynamic missing count; resume next pending field. | COMPLETE | `ca971d6` + `test`s | `test_plan_completion_flow.py`, `test_re14_single_menu.py` |
| 6 | Unify pain/injury/limitation | One canonical `training_limitations` fact ("כאב, פציעה או מגבלה"); legacy fields merged without duplication. | COMPLETE | `b8ba0bb` | `test_pain_aware_training.py`, `test_onboard_02_regression.py` |
| 7 | Goal first in plan completion | "Complete now" asks the goal first (nutrition + workout); confirmed goals skipped; separate Goal button removed. | COMPLETE | `ca971d6` | `test_re10_09_goal_wizard.py` |
| 8 | Unified limitations end-to-end | No planner/prompt/exercise/summary/profile flow reads a disconnected legacy limitation field; canonical-first with legacy fallback. | COMPLETE | `b8ba0bb` | `test_pain_aware_training.py`, `test_user_model_readiness.py` |
| 9 | Resume original gated action | Reusable pending-intent mechanism resumes the original action after prerequisites; not hardcoded into the allergy handler. | COMPLETE | `ef1802a` | `test_plan_completion_flow.py` |
| 10 | Food-photo analysis/correction pipeline | Canonical context + evidence precedence; cumulative deterministic-before-AI corrections (בלי שמן, חצי מהאורז, הכל כפול, x3); persist once; learned-food update; short status. | COMPLETE | `40546c7` | `test_meal_intelligence.py`, `test_meal_validation.py`, `test_nutrition_context.py` |
| 11 | Standalone pinnable daily menu | After strategy selection, generate + store an active daily menu; natural-language revisions edit the latest version; removed food not reintroduced. | COMPLETE | `7d7b2be` | `test_daily_menu_task12.py` |
| 12 | Multiple workout structures, one recommended | Show all compatible structures, exactly one ⭐; incompatible day-counts excluded; selection drives generation. | COMPLETE | `d9cfc78` | `test_planning_v2.py`, `test_re10_11_workout_wizard.py` |
| 13 | Day-specific workout times | Per-weekday time resolution overrides the global time; Friday not hardcoded; morning vs evening meal timing differs. | COMPLETE | `584721d` | `test_re10_12_unified_chronological.py` |
| 14 | "What should I eat now?" planner | Remaining cal/protein, valid-only sleep, explicit workout state (past-unrecorded → clarification), remaining-day timeline fitting the budget. | COMPLETE | `cc4462d` | `test_rec_next_meal_05.py`, `test_re9_next_meal_polish.py` |
| 15 | Morning Update ≠ daily menu | `menu:morning` is a short briefing (`build_morning_briefing_text`); morning/evening/rest differ; day-specific time; never resends the full menu. | COMPLETE | `50f953e` | `test_re16_morning_briefing.py` |
| 16 | One "My Week" action | `planv2:my_week` replaces Build/Show; validity via embedded plan IDs; meal logging never invalidates the week. | COMPLETE | `71ed907` | `test_re17_my_week.py` |
| 17 | Goal timeline feasibility | `targets.assess_goal_feasibility` (deficit, weekly rate, projected deadline weight, realistic timeline); estimate-framed warning + decision buttons; never forces an extreme deficit. | COMPLETE | `56e9664` | `test_goal_feasibility_task18.py`, `test_re10_09_goal_wizard.py` |
| 18 | Workout type wizard shows all strategies | Type list built from the canonical strategy registry, not candidate rows; one ⭐; step B regenerates if missing; telemetry uses the real count. | COMPLETE | `3570663` | `test_re10_11_workout_wizard.py` |
| 19 | Today's Menu around the actual day | `NutritionContext.day_type` (weekday/friday/saturday) → menu prompt; duplicate meal-time removed; adherence/optional-breakfast/weekend guidance; Morning-Update reuse. | COMPLETE | `dfa080c` | `test_todays_menu_task20.py` |
| 20 | Manual food logging (no photo) | `menu:food_text` → dedicated flow → `analyze_meal_text` (source="manual_text"); reuses the photo approval/persist/learning pipeline and cumulative correction. | COMPLETE | `f5877af` | `test_manual_food_logging_task21.py` |
| 21 | Simplify meal-analysis UX | Bulleted items (no `1.`); "correct by text" button removed; free-text correction works directly; full revised meal shown; no re-analysis after Save. | COMPLETE | `cf77804` | `test_meal_ux_timeline_task22.py` |
| 22 | Activity-aware "המשך היום" timeline | Post-save chronological timeline (cal/protein per slot, future workout at day-specific time, valid-only sleep), recomputed per meal; shared engine with next-meal planner. | COMPLETE | `cf77804` | `test_meal_ux_timeline_task22.py` |

> Note on strategy labels: the codebase uses strategy keys
> `consistency` / `balanced` / `performance` → Hebrew labels
> "מקסימום עקביות" / "מאוזנת" / "ביצועים". Some source specs use an
> illustrative third label ("מקסימום ירידה"/"מקסימום מגוון"); the
> architectural requirement (all three always selectable from the canonical
> registry) is met — only the sample label text differed.

---

## Open Backlog

| # | Title | Context | Verified gap | Required behavior | Source | Suggested acceptance criteria |
|---|-------|---------|--------------|-------------------|--------|-------------------------------|
| 58 | Meal image identity correction and portion estimation | Production incident where a user rejected "טחינה" and confirmed "חציל במיונז", but the corrected draft still restored raw tahini and broadly re-estimated unrelated items. | Food identity corrections are not enforced as hard item-scoped constraints; ambiguous Israeli food overrides can canonicalize generic tahini to raw tahini; portion estimates may lack sufficient visual evidence. | Implement structured identity correction, evidence precedence, safer canonicalization, improved portion evidence, bone-in handling, and high-impact uncertainty gates while preserving FIX 1-57 behavior. | [`tasks/TASK_58_MEAL_IMAGE_IDENTITY_CORRECTION_AND_PORTION_ESTIMATION.md`](../tasks/TASK_58_MEAL_IMAGE_IDENTITY_CORRECTION_AND_PORTION_ESTIMATION.md) | Rejected identities cannot return; unrelated items remain unchanged; corrections survive lifecycle; generic tahini does not canonicalize to raw tahini; explicit raw tahini still matches; high-impact ambiguous items trigger targeted clarification. |
| 59 | Remaining-day chronological timeline | Post-meal continuation currently renders a generic remaining-day block that can repeat the full remaining budget for each meal and include redundant prose. | Remaining-day surfaces do not consistently render one chronological action timeline with allocated per-meal calories/protein. | Reuse canonical planner semantics to render all remaining-day events in chronological order, allocate remaining nutrition budget across planned meals, and recalculate after state-changing events. | [`tasks/TASK_59_REMAINING_DAY_CHRONOLOGICAL_TIMELINE.md`](../tasks/TASK_59_REMAINING_DAY_CHRONOLOGICAL_TIMELINE.md) | Timeline covers workout/non-workout days, meal approval/correction, workout completed/postponed, chronological ordering, and per-meal protein allocation rather than repeated full remaining target. |
| 60 | Workout time averages and outlier-day approval | Health confirmation currently proposes a single typical workout hour even when historical workouts show specific weekdays with materially different usual times. | Per-weekday workout-time evidence is not surfaced during approval, and the default hour can collapse distinct day-specific routines. | Calculate average workout time per proposed training weekday, suggest a default from the average of per-day averages, and separately ask approval for weekdays with materially different times. | [`tasks/TASK_60_WORKOUT_TIME_AVERAGES_AND_OUTLIER_DAY_APPROVAL.md`](../tasks/TASK_60_WORKOUT_TIME_AVERAGES_AND_OUTLIER_DAY_APPROVAL.md) | Per-day averages are calculated from local workout history; default time comes from weekday averages; outlier weekdays receive day-specific approvals; approved values persist through existing workout_window and weekly_availability facts. |
| 61 | Pain-aware exercise substitution and load adjustment | A tennis-elbow limitation appears to make multiple workout days include squat/lower-body exercises, even where the day intent is chest, arms, or back. | Pain-aware filtering can over-remove elbow-sensitive work and replace it with unrelated exercises that are safe but do not preserve movement pattern or workout intent. | Add structured adaptation decisions that prefer same-pattern substitutions, support appropriate load/volume/variation reductions, and render concise user-facing notes for limitation-driven changes. | [`tasks/TASK_61_PAIN_AWARE_EXERCISE_SUBSTITUTION_AND_LOAD_ADJUSTMENT.md`](../tasks/TASK_61_PAIN_AWARE_EXERCISE_SUBSTITUTION_AND_LOAD_ADJUSTMENT.md) | Tennis elbow does not cause unrelated squat substitutions; same-pattern alternatives are preferred; load reduction is distinct from replacement; unsafe movements are still replaced/omitted; approval screen explains limitation-driven changes. |

New backlog items should be appended here with: unique ID, title, context,
verified gap, required behavior, source, and suggested acceptance criteria.

---

## Deferred / Known Technical Follow-ups

These are non-product follow-ups supported by repository evidence
(primarily [`docs/WHAT_REMAINS.md`](WHAT_REMAINS.md) and
[`docs/PRODUCT_ROADMAP.md`](PRODUCT_ROADMAP.md)). They require an external
resource or environment, not a code change.

- **External credentials/services** — production Telegram token + bot identity;
  active OpenAI key with real cost/rate-limit testing; Google Calendar OAuth;
  Sentry/monitoring; barcode/product-price/restaurant-menu providers.
- **Production infrastructure** — public HTTPS for the Mini App; production
  server, backups, secrets manager, monitoring; real load / multi-user testing;
  PostgreSQL + queue + encrypted object storage before significant scale.
- **Live Telegram integration testing** — end-to-end runs against a real bot
  (the suite mocks Telegram).
- **Flaky/order-dependent test investigation** — two next-meal option tests
  (`test_claude_audit_re8.py::test_quantity_edit_recalculates_option_totals_through_callback`,
  `test_recording_20260628_re8.py::test_selected_option_quantity_text_recalculates_without_saving`)
  can fail in certain isolated orderings when generation yields a single option;
  they pass in-file and in the full suite. Low priority.
- **Long-horizon routine learning** (Task 19 extension) — automatic learning of
  usual meal/photo/confirmation timing weighted by recency, feeding the daily
  planner. The context pipeline already supplies the inputs; the aggregator is
  not yet built.

---

## Archived / Superseded Requirements

For historical traceability only — these earlier requirements were replaced or
merged and should not be reopened.

- **Separate pain / injury / doctor-avoidance questions** → superseded by the
  unified `training_limitations` concept (Tasks 6 & 8).
- **"Build Unified Week" + "Show the Week"** two buttons → superseded by the
  single "🗓️ השבוע שלי" action (Task 16).
- **Dedicated "תקן במלל" meal-correction button** → removed in favor of direct
  free-text correction while a meal awaits approval (Task 21).
- **`menu:morning` rendering the full daily menu** → split: `menu:morning` is
  now the short briefing; the full menu lives on `menu:daily_menu`/`menu:today`
  (Task 15).
- **Numbered (`1.`/`2.`) meal-analysis items** → replaced with plain bullets
  (Task 21).
- **Generic "המשך היום: ארוחה" line** → replaced by the real activity-aware
  timeline (Task 22).

---

## Source documents

Raw specs and development history are preserved under:

- [`docs/archive/`](archive/) — the raw 22-task spec, the Hebrew screenshot
  backlog, and superseded work plans/handoffs.
- [`docs/audits/`](audits/) — audit reports (Claude/Codex independent audits).
- [`docs/reports/`](reports/) — patch reports, verification reports, status
  snapshots.
