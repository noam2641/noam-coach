# Addendum to the Master Correction Backlog

Audit anchor: branch `codex/complete-rec-program-04`, commit `bfb56d7f629ac96ac6909f63f140cbfa67af7ca1`.

This addendum preserves FIX 1–37 and extends the same audit. No implementation was performed.

> Scope note: this file contains the authoritative addendum and new FIX 38–57. The original full text of FIX 1–37 is not reproduced in this repository file. References to FIX 1–37 below are expansions and dependency links, not replacements for their original definitions.

## Executive conclusion

The product does not yet behave as one continuous coach with one coherent understanding of the user and today.

There are:

* 10 separately assembled production representations of “TODAY”.
* Two competing conversation-state systems.
* Several unversioned, independently persisted day projections.
* Different confirmation and freshness policies depending on the reader.
* No system-wide invalidation/version mechanism after a state-changing event.
* No cross-channel freshness protocol between Telegram and the Mini App.

The most serious newly confirmed risks are:

1. New allergies do not invalidate or revalidate old food actions.
2. `user_model.get_value()` bypasses the repository’s own freshness and confirmation policy.
3. Explicit workout completion/cancellation affects next-meal advice but not other workout consumers.
4. Meal mutation does not propagate to menus, recommendations, plans, follow-ups, or open clients.
5. Old Telegram callbacks can mutate current state without proving which rendered state they belong to.
6. Concurrent writers can overwrite unrelated `daily_flags` changes.

---

## New FIX items

### FIX 38 — Eliminate the split-brain conversation continuation system

**Classification:** CONFIRMED DEFECT

**Area:** Conversation state, onboarding, restart recovery, navigation

**Current behavior:**
`conversation.py` defines `active_flow` as the single active flow per user. Production code still persists independent continuation records in `conversation_state` through `noam_coach/services/core.py::set_flow_state()`.

Secondary flows include:

* `plan_completion`
* `goal_wizard`
* `profile_field_edit`
* `deferred_plan`
* `pending_plan_action`
* `next_meal_recommendation`
* Health confirmation/post-wizard state
* Workout split-set state

`menu:home`, `/cancel`, and `conversation.clear_all_flows()` clear only `active_flow`.

At startup, `load_pending_state()` rebuilds `active_flow` caches but deletes only selected legacy `conversation_state` names, including `deferred_plan`, while leaving most other secondary continuation rows untouched.

**Problem:**
The system claims to allow one active flow, but it can retain multiple invisible continuations. Home/cancel can appear to end an interaction while a secondary row later resumes it. Restart recovery treats secondary flows inconsistently: some survive, while `deferred_plan` is deleted and loses its continuation.

**Evidence in code:**

* `conversation.py::set_active_flow`, `clear_all_flows`, `resume_suspended`
* `noam_coach/services/core.py::set_flow_state`, `get_flow_state`, `clear_flow_state`
* `noam_coach/bot/onboarding.py::load_pending_state`
* `continue_after_plan_completion_answer`
* `noam_coach/bot/callback_plans.py::resume_pending_plan_action`
* `noam_coach/services/next_meal.py::remember_active_recommendation`
* `noam_coach/services/health_jobs.py`
* `noam_coach/bot/workout_runtime.py`

**Cross-system propagation:**
Stale continuation state can intercept later onboarding answers, resume old plan generation, finish the wrong profile edit, or interpret unrelated text as a prior goal-wizard answer. Restart changes which of these continuations remain possible.

**User impact:**
The user can press Home or cancel, begin a different task, and then have an old wizard unexpectedly continue. A plan completion flow can work before restart but disappear after restart.

**Required behavior:**
All interactive and resumable continuations must share one state model with explicit parent/child semantics, expiry, cancellation, and restart behavior.

**Recommended implementation direction:**
Move resumable domain continuations into the `active_flow` model or introduce an explicit continuation stack owned by the conversation service. Do not use generic `conversation_state` as a second flow engine. Make Home/cancel define whether it clears the active child only or the complete stack.

**Tests required:**

* Home clears every user-interactive continuation.
* `/cancel` followed by an unrelated answer cannot resume a prior wizard.
* Plan completion survives restart.
* Goal wizard and profile edit cannot coexist invisibly.
* Restart produces the same next handler as uninterrupted execution.
* Expired secondary state cannot consume input.

**Priority:** CRITICAL

**Dependencies or overlap:** FIX 2, FIX 3, FIX 5, FIX 16, FIX 35, FIX 51

---

### FIX 39 — Convert workout clarification into a coherent domain event

**Classification:** CONFIRMED DEFECT

**Area:** Workout state, next-meal advice, daily status, proactive jobs

**Current behavior:**
Buttons such as “סיימתי אימון” and “ביטלתי היום” call `save_next_meal_workout_status()`, which writes only:

* `daily_flags.next_meal_workout_status`
* `daily_flags.next_meal_workout_status_at`

`user_state.resolve_workout_state()` honors this for several hours, so next-meal logic can behave as post-workout or cancelled.

`daily_state.workout_completed_today()` intentionally ignores explicit self-report and accepts only a completed bot session or HealthKit workout. `DailyContext`, evening review, and proactive jobs consequently do not necessarily accept the same answer.

**Problem:**
The same user action has different truth status in different subsystems. “I finished” is treated as completion for meal timing but as “no workout recorded” in the evening summary.

**Evidence in code:**

* `noam_coach/services/next_meal.py::save_next_meal_workout_status`
* `noam_coach/services/user_state.py::resolve_workout_state`
* `_explicit_clarification_candidate`
* `noam_coach/services/daily_state.py::workout_completed_today`
* `noam_coach/jobs/proactive.py::build_daily_context`
* `noam_coach/services/health_jobs.py::_evening_coach_review_lines`
* Tests explicitly preserving “self-report is not actual evidence” in `tests/test_shared_user_state_corrective_pass.py`

**Cross-system propagation:**
Next-meal recommendations, post-workout allocations, morning briefing, workout prompts, motivation, Mini App operations, and evening summaries can disagree.

**User impact:**
The coach can say “eat after the workout” and later say “no workout was recorded today.”

**Required behavior:**
A clarification must create one typed workout-state event with source and confidence. Every consumer must follow the same policy. If the product distinguishes reported from verified completion, that distinction must be shown consistently rather than silently changing truth between screens.

**Recommended implementation direction:**
Create a workout-day event/override model with statuses such as reported-completed, verified-completed, cancelled, active, and rescheduled. Make the workout resolver return both status and evidence level. Migrate all status consumers to it.

**Tests required:**

* Reported completion produces consistent Telegram, Mini App, nutrition, and summary behavior.
* Cancelled workout suppresses workout prompts and motivation.
* Later verified HealthKit evidence upgrades reported completion without double-counting.
* A newly started session outranks an older completion report.

**Priority:** HIGH

**Dependencies or overlap:** FIX 8, FIX 27, FIX 28, FIX 40, FIX 52

---

### FIX 40 — Make “postpone workout” a real reschedule operation

**Classification:** CONFIRMED DEFECT

**Area:** Workout scheduling and nutrition timing

**Current behavior:**
The “אדחה את האימון” action writes `next_meal_workout_status="later"` but does not request or store a new time.

The active workout plan retains its original time. `_planned_session_candidate()` can reinterpret a passed workout window as `PRE_WORKOUT_NEAR`, while still carrying the old planned start. Morning briefing and workout prompts independently continue reading the original plan time.

**Problem:**
The UI promises postponement, but the backend stores only an ambiguous temporary label.

**Evidence in code:**

* `noam_coach/services/next_meal.py::workout_clarification_actions`
* `save_next_meal_workout_status`
* `noam_coach/services/user_state.py::_planned_session_candidate`
* `noam_coach/services/health_jobs.py::_todays_workout_time`
* `noam_coach/bot/assistant.py::build_workout_prompt_text`

**Cross-system propagation:**
Nutrition may treat the workout as still near, while morning/workout screens continue showing the old time and background jobs fire around the original schedule.

**User impact:**
The user postpones an 18:00 workout, but the coach continues referring to 18:00 and may send pre-workout advice after that time.

**Required behavior:**
Postponement must either collect a new time or clearly mark the workout unscheduled. The day plan, nutrition timing, workout prompt, and Mini App must all consume the same override.

**Recommended implementation direction:**
Persist a date-scoped workout schedule override containing original time, new time or unscheduled status, source, timestamp, and revision. Do not encode scheduling changes as a next-meal-only flag.

**Tests required:**

* Rescheduling changes every workout-time consumer.
* Rescheduling past midnight follows the selected day policy.
* “Later, time unknown” does not retain the old time as current.
* Old schedule buttons cannot restore an obsolete time.

**Priority:** HIGH

**Dependencies or overlap:** FIX 8, FIX 27, FIX 28, FIX 39, FIX 41

---

### FIX 41 — Define a canonical coaching-day boundary

**Classification:** NEEDS PRODUCT DECISION

**Area:** Timezone, date rollover, nutrition day, workout day

**Current behavior:**
Meals, `daily_flags`, active daily menu, planned meals, job state, workout completion, and recommendations reset at local calendar midnight.

Bedtime calculations can roll a bedtime such as 00:30 or 01:00 into the next calendar day. Therefore at 00:05 the coach may correctly report that sleep is still ahead while treating meals, remaining macros, flags, plans, and workout state as a brand-new day.

**Problem:**
Calendar day and lived coaching day are not the same model for late sleepers.

**Evidence in code:**

* `noam_coach/services/daily_state.py::local_day_bounds_utc`
* `health_service.py::local_day_str`
* `daily_menu_state.py::_local_day`
* `next_meal.py` local-day flag reads/writes
* `nutrition_context.py`
* `proactive.py::claim_job_delivery`
* Bedtime rollover logic in `next_meal.py` and `proactive.py`

**Cross-system propagation:**
A late meal can enter tomorrow’s totals while hours-until-sleep and meal timing still treat it as tonight. Planned meals and pending alerts disappear at midnight, and job delivery budgets reset.

**User impact:**
At 00:05 the coach can say “you still have time for a final meal before sleep,” while showing zero calories consumed for the new day.

**Required behavior:**
The product must choose and document either:

* strict local calendar days, with bedtime-aware UX explaining rollover; or
* a configurable coaching-day boundary tied to sleep/wake routine.

All subsystems must use the same decision.

**Recommended implementation direction:**
Introduce `CoachingDayService` returning day key, start/end instants, current phase, and rollover reason. Stop deriving day keys with independent `date()` calls.

**Tests required:**

* 23:59 → 00:01 around a 01:00 bedtime.
* Meal, workout, menu, flags, and job-state rollover.
* DST and timezone boundaries.
* Delayed HealthKit samples crossing midnight.
* User changes sleep routine during the day.

**Priority:** HIGH

**Dependencies or overlap:** FIX 6, FIX 7, FIX 18, FIX 19, FIX 20, FIX 22, FIX 28, FIX 34

---

### FIX 42 — Refresh learned routine state after meal lifecycle changes

**Classification:** CONFIRMED DEFECT

**Area:** Learned routine, meal timing, personalization

**Current behavior:**
`routine.learn_eating_windows()` reads meal history live, but consumers generally load the persisted `routine_profile`.

`save_routine_profile()` is called after HealthKit imports and after the evening job. It is not called after normal meal creation, correction, or undo.

Learned foods are queried live from `meal_items`, while learned eating times remain in the older routine snapshot.

**Problem:**
Two learning systems have different freshness after the same meal event.

**Evidence in code:**

* `routine.py::learn_eating_windows`, `learn_profile`
* `health_service.py::save_routine_profile`, `load_routine_profile`
* `noam_coach/bot/meals.py::persist_meal`
* `noam_coach/bot/callback_meals.py` undo flow
* `noam_coach/services/learned_foods.py`
* `noam_coach/services/health_jobs.py::job_evening`

**Cross-system propagation:**
Daily-menu timing, next-meal opportunities, motivation timing, and bedtime planning can use an outdated routine even when learned-food recognition already reflects the new meal.

**User impact:**
The coach learns what the user eats but not when they eat until an evening refresh or HealthKit import.

**Required behavior:**
Meal lifecycle events must either invalidate the stored routine projection or trigger a bounded recomputation policy.

**Recommended implementation direction:**
Add version/dependency metadata to `routine_profile`. Mark its eating component stale on meal create/edit/undo and rebuild lazily or asynchronously before timing-sensitive decisions.

**Tests required:**

* First/last meal time changes after logging.
* Correction of `eaten_at` changes the learned window.
* Undo removes the meal from learned timing.
* Correction after the evening job is not stale the next morning.

**Priority:** MEDIUM

**Dependencies or overlap:** FIX 7, FIX 17, FIX 18, FIX 19, FIX 43

---

### FIX 43 — Propagate meal create, edit, and undo across the complete day state

**Classification:** CONFIRMED DEFECT

**Area:** Meal lifecycle, derived day state, cached recommendations

**Current behavior:**
`persist_meal()` writes or edits meal records and logs events. Undo deletes the meal and records `MEAL_UNDONE`.

These transitions do not invalidate:

* Active daily menu
* Active next-meal recommendation
* Planned-meal state
* Routine profile
* Follow-up/job state
* Open Telegram keyboards
* Open Mini App state

Live totals become correct only when a later request rebuilds them.

**Problem:**
The durable meal ledger changes without updating the other representations of the same day.

**Evidence in code:**

* `noam_coach/bot/meals.py::persist_meal`
* `noam_coach/bot/callback_meals.py` undo handler
* `noam_coach/services/daily_menu_state.py`
* `noam_coach/services/next_meal.py::remember_active_recommendation`
* `noam_coach/services/meal_followup.py`
* `miniapp/static/app.js`

**Cross-system propagation:**
Old recommendation buttons can save stale options; a daily menu remains sized for the pre-meal balance; planned-meal follow-ups still fire; the Mini App remains stale.

**User impact:**
The coach confirms that the meal changed, but another screen or old message continues reasoning from the prior version.

**Required behavior:**
Meal create/edit/undo must be treated as one domain event that increments a day-state revision and invalidates every dependent projection.

**Recommended implementation direction:**
Add a transactional meal domain service that writes the meal and emits/records a `DayStateChanged` revision. Derived menu/recommendation/follow-up state must record the revision from which it was built.

**Tests required:**

* Create/edit/undo invalidates active recommendation.
* Menu save action fails after relevant meal revision.
* Planned follow-up is cancelled after matching consumption.
* Mini App refresh detects the new day version.
* Evening summary reflects the final corrected ledger.

**Priority:** HIGH

**Dependencies or overlap:** FIX 15, FIX 16, FIX 17, FIX 19, FIX 20, FIX 42, FIX 49, FIX 57

---

### FIX 44 — Add cross-channel freshness to the Mini App

**Classification:** CONFIRMED DEFECT

**Area:** Mini App, Telegram continuity

**Current behavior:**
The Mini App loads dashboard, profile, next meal, and today’s meals on initial page load. Meals and next meal have manual refresh buttons.

There is no polling, focus/visibility refresh, server push, or shared revision token. Saving profile reloads only the dashboard. Setting workout status reloads only the next-meal panel.

**Problem:**
The Mini App is a static snapshot while Telegram and jobs continue changing the same user state.

**Evidence in code:**

* `miniapp/static/app.js::loadDashboard`
* `loadProfile`
* `loadNextMeal`
* `loadTodayMeals`
* Initial load calls at the bottom of the file
* `mini_api.py` endpoints

**Cross-system propagation:**
Telegram meal logs, goal changes, pain reports, plan activation, workout completion, and HealthKit imports are invisible in an already-open Mini App.

**User impact:**
Telegram and the Mini App can show different calories, workout state, pain status, plans, and recommendations at the same time.

**Required behavior:**
Every Mini response should expose a state revision. The client must refresh when returning to focus and reject or refresh stale write operations.

**Recommended implementation direction:**
Introduce a `user_reality_version`/`day_state_version`, include it in all Mini payloads, refresh on `visibilitychange`/focus, and use optimistic concurrency for mutations.

**Tests required:**

* Telegram meal log while Mini is open.
* Goal/allergy/pain change while Mini is open.
* Midnight rollover while Mini remains open.
* Stale Mini mutation receives a conflict and refreshes.
* Profile save refreshes all affected panels.

**Priority:** HIGH

**Dependencies or overlap:** FIX 15, FIX 16, FIX 32, FIX 33, FIX 43, FIX 54, FIX 57

---

### FIX 45 — Make workout rest timers restart-safe

**Classification:** CONFIRMED DEFECT

**Area:** Workout execution and restart recovery

**Current behavior:**
Rest timers live only in the Telegram `JobQueue`. Their deadline is stored as `time.monotonic()` in job data. No database deadline or message mapping is persisted.

The session step is advanced before the rest timer completes. Startup restores global jobs and conversation caches but not rest timers.

**Problem:**
A restart destroys the timer while leaving the durable session on the next set.

**Evidence in code:**

* `noam_coach/bot/workout_runtime.py::start_rest_timer`
* `rest_timer_tick`
* `cancel_rest_timer`
* `time.monotonic()`
* `noam_coach/app/runtime.py::schedule_jobs`, `run`
* `noam_coach/bot/onboarding.py::load_pending_state`

**Cross-system propagation:**
The Telegram card freezes at an old remaining time; the database believes the set has advanced; subsequent input can operate on a different step than the message shows.

**User impact:**
After restart during rest, the user sees an abandoned timer and may log the wrong set.

**Required behavior:**
The rest deadline and rendered session step must survive restart, or the stale card must be invalidated and the session resumed explicitly.

**Recommended implementation direction:**
Persist a wall-clock rest deadline, session-step revision, chat/message identity, and timer status. On startup, restore remaining timers or edit stale cards into a resume state.

**Tests required:**

* Restart midway through rest.
* Restart after deadline but before final tick.
* Timer card from an old session cannot advance a new session.
* Pause/cancel during restored rest.
* Multiple rapid timer starts retain only the latest revision.

**Priority:** HIGH

**Dependencies or overlap:** FIX 29, FIX 30, FIX 35, FIX 51

---

### FIX 46 — Give manual meal interpretation the same task-appropriate context as photo entry

**Classification:** CONFIRMED DEFECT

**Area:** Meal AI, manual entry, correction pipeline

**Current behavior:**
Photo analysis receives a structured `NutritionContext`, safety facts, and learned-food context.

`analyze_meal_text()` receives only the description, narrow allergy/diet facts, and learned foods. Manual corrections without an image call the same context-poor function.

Both paths eventually use the same approval and persistence pipeline.

**Problem:**
The shared downstream model hides a different upstream interpretation contract. Manual text cannot resolve references such as “what we planned,” and lacks day/workout/plan context available to photo analysis.

Conversely, the photo estimator receives an overly broad goal/balance context that could bias estimation even though consumed balance should not change the factual calorie estimate.

**Evidence in code:**

* `noam_coach/services/profile.py::analyze_meal_image`
* `analyze_meal_text`
* `_meal_safety_context`
* `reanalyze_meal_with_text_and_image`
* `noam_coach/bot/assistant.py::log_meal_from_text`
* `noam_coach/bot/meal_text.py`

**Cross-system propagation:**
Manual and photo entries can derive different item identities or assumptions from equivalent user evidence, then persist as apparently equivalent meals.

**User impact:**
The coach remembers the planned meal when a photo is sent but may act as though it has no context when the same meal is entered manually.

**Required behavior:**
Both entry modes must use one meal-interpretation request model, containing only context relevant to evidence resolution and safety. Nutritional targets must not bias objective food estimation.

**Recommended implementation direction:**
Create `MealInterpretationContext` separate from `NutritionRecommendationContext`. Pass planned meal identities, recent corrections, safety constraints, learned foods, evidence precedence, and source type to both paths.

**Tests required:**

* Equivalent manual/photo descriptions produce compatible meal models.
* “I ate what we planned” resolves a planned-meal identity.
* Allergies are enforced identically.
* Target calories cannot bias item calorie estimation.
* Manual correction preserves earlier locked corrections.

**Priority:** HIGH

**Dependencies or overlap:** FIX 15, FIX 17, FIX 23, FIX 49, FIX 55

---

### FIX 47 — Enforce confirmation and freshness in every fact read

**Classification:** CONFIRMED DEFECT

**Area:** User facts, HealthKit facts, planning safety

**Current behavior:**
`fact_is_usable_for_decision()` rejects stale facts, gaps, unconfirmed estimates, and unconfirmed derived facts.

`user_model.get_value()` calls `get_fact()` and returns its value without applying `fact_is_usable_for_decision()`.

Many production consumers use `get_value()` directly for planning, goals, restrictions, assistant context, workout-plan mirrors, and onboarding checks.

**Problem:**
The repository has a formal confirmation/freshness policy that decision paths can bypass.

**Evidence in code:**

* `user_model.py::fact_is_usable_for_decision`
* `compute_readiness`
* `get_value`
* Readers in `planning.py`
* `noam_coach/services/goals.py`
* `nutrition_context.py`
* `next_meal.py`
* `meal_validation.py`
* `noam_coach/bot/assistant.py`
* `noam_coach/bot/assistant.py::build_workout_prompt_text`

**Cross-system propagation:**
A fact can be shown as stale or unconfirmed in onboarding while still driving a live goal, meal restriction, workout plan, or AI context.

**User impact:**
The coach may ask the user to confirm a value while simultaneously acting on it as authoritative.

**Required behavior:**
All fact reads must declare their intended policy: decision-grade, draft, display, or raw audit.

**Recommended implementation direction:**
Replace ambiguous `get_value()` use with explicit APIs such as `get_decision_value`, `get_display_value`, and `get_raw_fact`. Make decision-grade reads enforce kind, confirmation, validity, and freshness centrally.

**Tests required:**

* Stale fact cannot drive plan activation.
* Unconfirmed HealthKit estimate is visible but not authoritative.
* Gap values never enter prompts or restriction logic.
* Confirmation immediately upgrades all decision consumers.
* Every planning reader is covered by a policy test.

**Priority:** CRITICAL

**Dependencies or overlap:** FIX 1, FIX 2, FIX 23, FIX 24, FIX 25, FIX 26, FIX 27, FIX 48, FIX 54

---

### FIX 48 — Add a pain recovery transition and unify pain expiry

**Classification:** CONFIRMED DEFECT

**Area:** Pain, injury, limitations, safety

**Current behavior:**
Pain reports insert `medical_constraints` rows with `status='active'`. The schema has `resolved_at`, but no production path was found that resolves or deactivates a constraint.

`active_constraints()` treats active rows as active indefinitely. `training_intelligence.active_pain_regions()` independently applies a 14-day TTL. The `training_limitations` fact has another expiry policy, and `get_value()` can ignore it.

The intent router supports `report_pain` but not “pain is gone.”

**Problem:**
The same pain can be active forever for planning/background context, expired for load decisions and Mini App, and stale-but-still-readable as a fact.

**Evidence in code:**

* `db.py` medical constraint schema
* `noam_coach/bot/onboarding.py::save_medical_constraint`, `active_constraints`
* `training_intelligence.py::active_pain_regions`
* `noam_coach/services/training.py::_exercise_pain_caution`
* `assistant.py::SYSTEM_PROMPT`
* `user_model.py` fact expiry and `get_value`

**Cross-system propagation:**
Workout planning, current-session safety, load progression, Mini App operations, AI context, and proactive messages can disagree about whether pain is active.

**User impact:**
The user cannot tell the coach that pain has resolved. Some screens remain cautious indefinitely, while others silently stop considering it after 14 days.

**Required behavior:**
Pain must have one lifecycle: reported, confirmed, improving, resolved, recurrent, or medically restricted, with explicit dates and policy.

**Recommended implementation direction:**
Make `medical_constraints` authoritative. Add a resolution/reassessment flow and derive planning facts and active-region views from it. Physician restrictions should not share the same silent TTL as temporary pain.

**Tests required:**

* “The pain is gone” resolves the correct constraint.
* Recurrent pain creates or reopens the right episode.
* Temporary pain and physician restrictions have different expiry rules.
* Every workout and Mini surface agrees immediately after resolution.
* Old pain does not silently remain in AI context.

**Priority:** CRITICAL

**Dependencies or overlap:** FIX 26, FIX 27, FIX 28, FIX 47, FIX 53

---

### FIX 49 — Give planned meals identity and a consumed/cancelled lifecycle

**Classification:** CONFIRMED DEFECT

**Area:** Planned meals, consumption, meal follow-ups

**Current behavior:**
`plan_chosen_meal()` appends a dictionary to `daily_flags.next_meal_planned`.

`save_chosen_meal()` inserts a consumed meal but does not transition a matching planned meal. Photo/manual meal records do not carry a planned-meal identity.

`NutritionContext` can therefore include the same conceptual meal as both consumed and planned. `planned_meal_followup()` suppresses a follow-up only when normalized meal names match exactly.

**Problem:**
Planned meals are anonymous snapshots, not lifecycle entities.

**Evidence in code:**

* `noam_coach/services/next_meal.py::plan_chosen_meal`
* `save_chosen_meal`
* `noam_coach/services/nutrition_context.py::_planned_next_meals`
* `noam_coach/services/meal_followup.py::planned_meal_followup`
* Manual/photo persistence in `noam_coach/bot/meals.py`

**Cross-system propagation:**
Post-meal planning, AI context, daily-menu reasoning, and proactive follow-ups can all believe a consumed meal is still pending.

**User impact:**
The coach may ask whether the user ate a planned meal after they already logged it under a slightly different name or corrected composition.

**Required behavior:**
A planned meal needs a durable ID and status transitions: planned, consumed, replaced, postponed, cancelled, expired.

**Recommended implementation direction:**
Create a planned-meal entity or typed day event. Allow meal persistence to link to a plan ID. Use ingredient/fingerprint matching only as a user-confirmed fallback, not exact title equality.

**Tests required:**

* Confirming a planned recommendation transitions it to consumed.
* Photo/manual entry can match a plan with confirmation.
* Correcting the consumed meal retains the plan link.
* Undo reopens or explicitly detaches the plan.
* Follow-up never asks about a consumed/cancelled plan.

**Priority:** HIGH

**Dependencies or overlap:** FIX 18, FIX 19, FIX 20, FIX 43, FIX 46

---

### FIX 50 — Resolve next-meal corrections before daily-menu edits

**Classification:** CONFIRMED DEFECT

**Area:** Free-text routing, recommendation correction, daily menu editing

**Current behavior:**
`handle_text_message()` comments that an active next-meal correction should be interpreted first.

The implementation calls `try_build_daily_menu_edit_reply()` before `handle_recommendation_correction()`.

When an active daily menu exists, generic text such as “גדול מדי”, “פחות”, or “בלי…” can be accepted as a menu edit, frequently against the default snack slot.

**Problem:**
The documented routing precedence is inverted.

**Evidence in code:**

* `noam_coach/bot/meal_text.py::handle_text_message`
* `noam_coach/services/daily_menu_edit.py`
* `noam_coach/services/next_meal.py::handle_recommendation_correction`

**Cross-system propagation:**
The active daily menu mutates while the active next-meal recommendation remains unchanged. The user’s correction is applied to a different object than the one they are looking at.

**User impact:**
Replying “smaller” to a next-meal card can edit the daily menu’s snack instead.

**Required behavior:**
Routing must use explicit active-object identity. If both objects could accept the text, the most recent rendered/selected object should win or the user should be asked which one they mean.

**Recommended implementation direction:**
Store a channel/message-scoped active coaching object with type and revision. Route corrections using reply-to message identity where available.

**Tests required:**

* Active menu plus active next-meal card.
* “Smaller,” “without eggs,” and “not suitable.”
* Reply-to an old next-meal card after a new menu.
* Ambiguous non-reply text produces a clarification.

**Priority:** HIGH

**Dependencies or overlap:** FIX 10, FIX 16, FIX 23, FIX 43, FIX 51

---

### FIX 51 — Version every state-changing Telegram callback

**Classification:** CONFIRMED DEFECT

**Area:** Telegram callbacks, stale messages, replay safety

**Current behavior:**
The callback router validates a flow version only if callback data includes one.

Important mutations omit identity/version:

* `nextmeal:save:N`
* `nextmeal:plan:N`
* Next-meal feedback actions
* `confirm:goal_cal:value`

A stale next-meal save callback loads the latest active options or regenerates a current recommendation. It can therefore save a different option than the old message displayed.

A stale goal callback carries a raw value and can clear/apply against the current confirmation state.

The 1.2-second debounce prevents rapid double taps, not replay from old messages.

**Problem:**
The mutation does not prove which rendered state the user approved.

**Evidence in code:**

* `conversation.py::encode_callback`, `check_version`
* `noam_coach/bot/callback_router.py`
* `noam_coach/bot/callback_menu.py`
* `next_meal.py::next_meal_action_rows`
* Goal buttons in `noam_coach/bot/assistant.py` and onboarding
* Daily-menu callbacks demonstrate the safer `menu_id` pattern

**Cross-system propagation:**
Old Telegram messages can overwrite goals, save wrong meals, or plan an option from a newer recommendation.

**User impact:**
The user taps “confirm eaten” under meal A but meal B from a later recommendation is stored.

**Required behavior:**
Every state-changing callback must identify the domain object, object revision, action, and single-use intent.

**Recommended implementation direction:**
Persist callback action tokens with object ID, revision, expiry, and consumed status. Keep callback data short by sending opaque IDs.

**Tests required:**

* Old next-meal button after a newer recommendation.
* Old goal confirmation after another pending goal.
* Replay after successful mutation.
* Replay after restart.
* Callback from yesterday after date rollover.

**Priority:** CRITICAL

**Dependencies or overlap:** FIX 16, FIX 24, FIX 25, FIX 38, FIX 43, FIX 45, FIX 55

---

### FIX 52 — Reconcile bot and HealthKit workout events across analytics

**Classification:** CONFIRMED DEFECT

**Area:** Workout history, HealthKit, adherence, learned routine

**Current behavior:**
The shared workout resolver chooses between current-day bot and HealthKit completion evidence, including a chronology comparison.

Historical consumers do not use a reconciled event stream:

* `routine.learn_workout_pattern()` reads HealthKit workouts only.
* `planning.adherence_snapshot()` counts bot sessions only.
* Weekly summary in `noam_coach/bot/assistant.py` counts bot sessions only.
* Load progression uses bot sessions and sets.
* There is no cross-source workout identity/deduplication.

**Problem:**
HealthKit-only workouts disappear from adherence summaries, while bot-only workouts disappear from learned routine. The same physical workout recorded by both sources has no canonical identity.

**Evidence in code:**

* `noam_coach/services/user_state.py::_healthkit_session_candidate`
* `routine.py::learn_workout_pattern`
* `planning.py::adherence_snapshot`
* `noam_coach/bot/assistant.py::build_weekly_summary_text`
* `noam_coach/services/training.py`

**Cross-system propagation:**
Today’s recommendation can recognize the workout while weekly adherence says zero. Historical routine may be biased toward Watch-tracked sessions only.

**User impact:**
The coach acknowledges an imported workout today but later reports that the user missed it.

**Required behavior:**
Bot and HealthKit records need canonical workout-event reconciliation with provenance and deduplication.

**Recommended implementation direction:**
Create a unified workout-event projection matching source events by time overlap, duration, and type. Preserve raw sources but let today, routine, adherence, and summary consumers use reconciled events.

**Tests required:**

* HealthKit-only workout counts in adherence.
* Bot-only workout influences routine.
* Same workout in both sources counts once.
* Two genuine workouts on the same day count twice.
* Partial/cancelled bot session plus HealthKit workout.

**Priority:** HIGH

**Dependencies or overlap:** FIX 27, FIX 28, FIX 39, FIX 47, FIX 54

---

### FIX 53 — Give the general assistant continuity context, not only routing hints

**Classification:** ARCHITECTURAL RISK

**Area:** General AI assistant and natural-language continuity

**Current behavior:**
The “general assistant” AI call is an intent classifier. Its user context contains only primary goal, weight, and weekly training frequency.

It receives no conversation history, active daily menu, active recommendation identity, today’s meals, workout state, pain, plan, time, confirmation state, or HealthKit freshness.

Special-case deterministic routing handles a few continuations, but only if the relevant state is correctly active.

**Problem:**
The user experiences every natural-language interaction as one coach, but the classifier cannot resolve references such as “yes,” “as you said,” “make it smaller,” or “I did that” from a shared coaching state.

**Evidence in code:**

* `noam_coach/bot/assistant.py::assistant_profile_summary`
* `route_free_text`
* `assistant.py::classify_intent`
* `assistant.py::SYSTEM_PROMPT`
* Secondary state in FIX 38
* Routing conflict in FIX 50

**Cross-system propagation:**
Information given to the meal, workout, goal, or Health flow is not available to general-language interpretation unless copied into one of three profile values.

**User impact:**
The coach appears to forget the immediately preceding recommendation or action.

**Required behavior:**
The natural-language router should receive a bounded, structured turn context: current coaching object, active flow, today revision, relevant recent events, confirmation state, and domain constraints.

**Recommended implementation direction:**
Build an `AssistantTurnContext` from canonical state. Keep the AI as a router if desired, but make reference resolution deterministic and auditable.

**Tests required:**

* “Yes” after a confirmation.
* “Make it smaller” after next-meal output.
* “I did it” after a workout prompt.
* “Use what I told you” after allergy/pain update.
* No stale continuation after Home/cancel/restart.

**Priority:** HIGH

**Dependencies or overlap:** FIX 23, FIX 26, FIX 34, FIX 38, FIX 47, FIX 50

---

### FIX 54 — Invalidate live coaching projections after HealthKit import

**Classification:** CONFIRMED DEFECT

**Area:** HealthKit import, routine, menus, recommendations

**Current behavior:**
Health import writes health rows, synchronizes facts, and rebuilds `routine_profile`.

It does not invalidate:

* Active daily menu
* Active next-meal recommendation
* Planned meals
* Active workout/nutrition plans
* Previously computed Mini App DOM
* Already-sent proactive decisions

Live HealthKit endpoints behave similarly.

**Problem:**
The import can change current weight, sleep routine, step baseline, workout completion, and training routine while old decisions remain actionable.

**Evidence in code:**

* `noam_coach/services/health_jobs.py::import_health_export_file`
* `noam_coach/api/health_routes.py::healthkit_samples`
* `shortcut_health`
* `sync_health_measurements_to_facts`
* `save_routine_profile`
* No calls to menu/recommendation/day-state invalidators

**Cross-system propagation:**
A new HealthKit workout may change next-meal phase immediately, while an old recommendation remains stored and old menu/workout prompts remain valid.

**User impact:**
The user imports current data and receives confirmation, but the coach continues using a plan or message created from the pre-import reality.

**Required behavior:**
Health import must record what semantic facts changed and invalidate only dependent projections. Unconfirmed imported facts must not silently become decision-grade.

**Recommended implementation direction:**
Return a change set from health synchronization, increment user/day revisions, and mark affected goals, plans, routine, menu, recommendation, and proactive decisions stale.

**Tests required:**

* Importing today’s workout invalidates pre-workout recommendations.
* New sleep schedule changes timing projections.
* Weight change marks goal-derived plans for review.
* Duplicate-only import causes no invalidation.
* Unconfirmed measurements do not drive strong decisions.

**Priority:** HIGH

**Dependencies or overlap:** FIX 1, FIX 15, FIX 23, FIX 27, FIX 28, FIX 32, FIX 33, FIX 43, FIX 47, FIX 52

---

### FIX 55 — Invalidate and revalidate food actions after safety-fact changes

**Classification:** CONFIRMED DEFECT

**Area:** Allergies, restrictions, old recommendations, food safety

**Current behavior:**
Adding an allergy or restriction updates user facts but does not invalidate an active menu or recommendation.

`save_chosen_meal()` persists a recommendation directly without validating it against the user’s current restrictions. The daily-menu save handler checks `menu_id`, but not whether restrictions changed since generation.

Photo/manual meal approval does perform a final restriction validation, so the safety policy differs by entry path.

**Problem:**
An old food action can bypass a newly reported allergy.

**Evidence in code:**

* `noam_coach/bot/assistant.py::record_dietary_preference`
* Onboarding allergy handlers
* `mini_api.py::mini_update_profile`
* `noam_coach/services/next_meal.py::save_chosen_meal`
* `noam_coach/bot/callback_menu.py` next-meal and daily-menu save handlers
* `noam_coach/bot/meals.py::persist_meal` demonstrates the missing final gate

**Cross-system propagation:**
Telegram old buttons, Mini recommendations, daily menu, planned meals, and AI context can all retain unsafe food after the user changes a safety fact.

**User impact:**
The coach says it will respect a new allergy in every recommendation, but an old “confirm eaten” or “plan this” button remains active.

**Required behavior:**
A safety-fact change must immediately invalidate every food recommendation/menu generated before that fact revision. Every final food mutation must revalidate current restrictions.

**Recommended implementation direction:**
Maintain a `safety_revision`. Store it on menu/recommendation/planned-meal objects and reject mismatched actions. Add a final safety validator inside the shared save service.

**Tests required:**

* Add allergy after recommendation generation, then press old save/plan.
* Add allergy after daily-menu generation.
* Remove/reclassify restriction and regenerate.
* Telegram and Mini behavior are identical.
* Final validation occurs even if generation previously passed.

**Priority:** CRITICAL

**Dependencies or overlap:** FIX 15, FIX 16, FIX 21, FIX 23, FIX 46, FIX 47, FIX 51, FIX 54

---

### FIX 56 — Remove duplicate Mini App workout-status requests and routes

**Classification:** CONFIRMED DEFECT

**Area:** Mini App client/API

**Current behavior:**
The Mini App click handler calls `setNextMealWorkoutStatus()` twice for one click.

`mini_api.py` also declares the same `GET /mini/api/next-meal` decorator twice.

**Problem:**
One user action creates two concurrent POST requests, two recommendation builds, and two DOM updates. Responses can arrive out of order. The duplicated route registration is additional evidence of unreviewed duplicate wiring.

**Evidence in code:**

* `miniapp/static/app.js` next-meal click handler
* `mini_api.py` duplicate `@router.get("/mini/api/next-meal")`
* `mini_next_meal_workout_status`

**Cross-system propagation:**
The duplicate requests increase the chance of a `daily_flags` lost update and can race with Telegram or background changes.

**User impact:**
The visible recommendation can be whichever duplicate response finishes last.

**Required behavior:**
One click must create one idempotent mutation and one refreshed response.

**Recommended implementation direction:**
Remove duplicate client invocation and duplicate route decoration. Add a pending/disabled state and an idempotency token for state-changing Mini requests.

**Tests required:**

* Browser-level click produces exactly one POST.
* Rapid repeated clicks are idempotent.
* Out-of-order responses cannot replace newer state.
* Route table contains one handler per method/path.

**Priority:** HIGH

**Dependencies or overlap:** FIX 33, FIX 44, FIX 57

---

### FIX 57 — Prevent lost updates in the shared daily_flags JSON document

**Classification:** ARCHITECTURAL RISK

**Area:** Daily state persistence and concurrency

**Current behavior:**
Multiple modules read the complete `daily_flags.flags` JSON object, mutate one key, and upsert the complete object.

Independent writers include:

* Morning flags and medication
* Next-meal state and feedback
* Planned meals
* Active daily menu and message identity
* Workout clarification
* Recommendation save guards

There is no version, compare-and-swap, key-level update, or transaction spanning the read-modify-write sequence.

**Problem:**
Two concurrent writers can each read version A, modify different keys, and write A1/A2. The last writer silently removes the first update.

**Evidence in code:**

* `health_service.py::get_daily_flags`, `set_daily_flags`
* `next_meal.py::_daily_flags`, `_save_daily_flags`
* `daily_menu_state.py::_daily_flags`, `_save_daily_flags`
* `daily_menu_edit.py`
* Only the final write is atomic; the read-modify-write is not

**Cross-system propagation:**
A meal plan can erase a workout clarification; a menu refresh can erase medication/fasting state; duplicate Mini requests amplify the race.

**User impact:**
The coach appears to forget a fact reported moments earlier with no error or audit trail.

**Required behavior:**
Day-state mutations must be atomic, versioned, and merge-safe.

**Recommended implementation direction:**
Prefer normalized typed tables for important state. If JSON remains, implement transactional key patching with a revision/CAS check and retry. Record the day-state revision on dependent projections.

**Tests required:**

* Concurrent updates to unrelated keys both survive.
* Telegram and Mini mutations in parallel.
* Job update concurrent with user input.
* Duplicate request idempotency.
* Revision conflict is observable and retried safely.

**Priority:** HIGH

**Dependencies or overlap:** FIX 22, FIX 38, FIX 43, FIX 44, FIX 49, FIX 54, FIX 56

---

## Existing FIX items requiring expansion

| Existing FIX | Additional evidence and required expansion |
|---|---|
| FIX 1 | Readiness/confirmation must account for FIX 47: production `get_value()` readers can act on facts that readiness correctly rejects. Readiness is not currently an enforcement boundary. |
| FIX 2, FIX 3 | Add the secondary `conversation_state` continuations and inconsistent startup deletion described in FIX 38. “Deferred” is not one lifecycle: some rows survive restart, while `deferred_plan` is deleted. |
| FIX 5 | Expand the active-flow source-of-truth issue to Home/cancel semantics, secondary continuation priority, and old callback replay. |
| FIX 6, FIX 7 | Add calendar-midnight splitting from FIX 41 and signed-vs-clamped remaining values: `NutritionContext` preserves negative balances, while `DailyContext` clamps remaining calories/protein to zero. |
| FIX 8 | Add `_ctx_has_workout(ctx) = completed OR usual-day`. It can ignore a real scheduled workout on an atypical weekday. Morning briefing separately reads the active plan and can call the same day a workout day. |
| FIX 14 | `active_daily_menu.context_version` exists but normal generation does not supply one. The schema anticipates invalidation but does not implement it. |
| FIX 15 | Expand to all meal lifecycle propagation in FIX 43, including Mini App, recommendation state, routine profile, planned follow-up, and old Telegram messages. |
| FIX 16 | Add the exact replay path: an old `nextmeal:save:N` can select from the latest active recommendation or freshly generated options rather than the options displayed in the old message. |
| FIX 17 | Learned foods refresh live, but learned meal timing is a stored `routine_profile` refreshed only later. |
| FIX 18, FIX 19, FIX 20 | Planned meals require durable identity and transitions. Exact title comparison is not a sufficient consumed-state resolver. |
| FIX 21 | Add the final-action safety bypass in FIX 55: generated recommendations are validated, but `save_chosen_meal()` does not revalidate current facts. |
| FIX 22 | Expand `daily_flags` from “fragmented keys” to a concurrency problem: unrelated state can be lost through whole-document writes. |
| FIX 23 | Add the AI discontinuities in FIX 46 and FIX 53. Recent meal corrections remain inside approval/reanalysis state and are not a shared coaching-memory input. |
| FIX 24, FIX 25 | Goal-change invalidation currently clears only recent-title/size keys. It does not invalidate active menu, active recommendation, planned meals, old callbacks, or Mini DOM. |
| FIX 26 | Expand to the missing pain-resolution transition and conflicting infinite/14-day/fact-expiry policies in FIX 48. |
| FIX 27 | Add cross-source workout-history reconciliation from FIX 52 and the fact that active workout plans have both `active_plans` and `user_facts.active_workout_plan` representations. |
| FIX 28 | Proactive workout/motivation logic still reads raw active sessions, routine time, or mirrored plans instead of the shared workout resolver. Explicit cancellation/completion can therefore be ignored. |
| FIX 32, FIX 33 | Expand Mini risk to open-client staleness, raw active-session reads, duplicate status POSTs, and missing revision/conflict behavior. |
| FIX 34 | Evening summary reads current meal rows, but workout completion still excludes explicit reports. The evening job refreshes `routine_profile` only after sending the summary. |
| FIX 35 | Runtime restart recovery must include rest timers, secondary continuation rows, stale Telegram cards, missed scheduled jobs, and in-memory debounce loss. |
| FIX 37 | Add the cross-system regression scenarios listed below; current tests heavily protect isolated helpers while leaving lifecycle propagation and cross-channel consistency untested. |

---

## Canonical User Reality Map

There is no complete canonical reality object today. The closest foundation is `SharedUserState`, but it covers only current time, workout state, consumed meals, planned meal titles, and today’s session plan.

| Concept | Current authority | Secondary/cache/AI/UI representations | Freshness/confirmation/invalidation problem |
|---|---|---|---|
| User identity | `users`, configured Telegram user ID | Telegram update, Mini session token | Mostly coherent; Mini and Telegram assume the one configured user. |
| Profile facts | `user_facts` | `planning.profile_snapshot`, prompts, Mini form | Reader policy differs; `get_value()` bypasses decision usability. |
| Confirmed facts | `user_facts.confirmed` | Readiness/profile display | Not consistently enforced by live readers. |
| Unconfirmed facts | Same table with estimate/derived kind | Health confirmation wizard | Can still enter decision paths through `get_value()`. |
| HealthKit facts | Health rows; selected mirrors in `user_facts` | `routine_profile`, health screens, workout resolver | Different direct/derived consumers; import invalidation absent. |
| Learned facts | `routine_profile`, live meal history | Menu, next meal, motivation, prompts | Learned foods are live; learned meal timing is cached. |
| Goal state | `goal_versions` + active goal pointer/status | `fetch_goal`, Mini dashboard, AI explanation | Old callbacks and old projections survive goal change. |
| Calorie target | Active goal | `DailyContext`, `NutritionContext`, menus/messages | Default fallbacks and stale projections can differ. |
| Protein target | Active goal | Same | Goal changes preserve old protein during manual calorie-only update; dependent projections are not fully invalidated. |
| Consumed nutrition today | Meals with consumed status and local bounds | `SharedUserState`, `NutritionContext`, `DailyContext`, Mini | Usually recomputed live, but open messages/clients remain stale. |
| Remaining nutrition | Derived target minus consumed | Signed in `NutritionContext`; clamped in `DailyContext`; allocated in next-meal logic | Competing mathematical meaning. |
| Planned meals | Active nutrition plan plus `daily_flags.next_meal_planned` | Nutrition AI, follow-up job, `SharedUserState` titles | No durable lifecycle or consumption link. |
| Active daily menu | `daily_flags.active_daily_menu` | Telegram message and buttons | No meaningful context version/invalidation. |
| Active next-meal recommendation | `conversation_state.next_meal_recommendation` | Telegram/Mini cards | Six-hour TTL only; no day-state/safety revision. |
| Meal corrections | Approval payload and edited meal rows | Meal UI; later meal totals | Correction intent is not shared coaching memory; dependent projections remain stale. |
| Meal undo | Physical meal deletion plus audit event | Undo message/draft approval | No day-state invalidation or planned-meal reconciliation. |
| Learned foods | Derived live from approved `meal_items` | Meal AI, menu, next meal | Fresher than learned routine; deleted/corrected rows change it only on next query. |
| Food dislikes/preferences | `user_facts` | Preference profile, recommendation filters | Old menu/recommendation actions remain valid. |
| Allergies/restrictions | `user_facts` | Typed restriction projection, AI prompt, final photo/manual validator | Old saved recommendation/menu bypasses new safety fact. |
| Workout plan | `active_plans`/`plan_versions` | Mirrored `active_workout_plan` fact | Two persistent representations can diverge. |
| Today’s workout | `resolve_workout_state()` is closest resolver | Raw session queries, `DailyContext`, morning briefing, Mini | Several consumers bypass resolver. |
| Workout time | Active plan session time | Routine typical time, “later” flag, morning direct query | No date-scoped schedule override. |
| Workout status | Resolver candidates | `daily_flags`, session status, HealthKit, raw helpers | Explicit report has inconsistent authority. |
| Workout completion | Bot session or HealthKit for strict function | Explicit self-report for next-meal only | Not one definition. |
| Fatigue | Daily flags plus training assessment | Workout load recommendations and banners | Not part of general AI or canonical day state. |
| Pain | `medical_constraints` should be authority | `training_limitations` fact, active-region TTL, Mini | No resolution path; competing expiry policies. |
| Injury | `medical_constraints`/free-text facts | Planning constraints | Not clearly separated from temporary pain. |
| Medical restrictions | Constraint rows/facts | Planning adaptation and AI context | Permanent restriction and temporary pain share fragmented models. |
| Wake/sleep routine | `routine_profile.sleep` | Health facts, hours-until-bedtime logic | Stored projection can be updated by Health import without invalidating day outputs. |
| Current local time | `TZ` and per-function `datetime.now(TZ)` | `SharedUserState` snapshot, independent job/UI snapshots | Same request is sometimes unified; cross-feature actions are not. |
| Current user day | Local calendar date | Meal bounds, daily flags, job state, menu | Conflicts with after-midnight pre-sleep behavior. |
| Onboarding state | Onboarding stage plus `active_flow` | Secondary plan/goal continuations | Can split across multiple state engines. |
| Active conversation flow | `active_flow` | In-memory pending caches and `conversation_state` | Home/cancel/restart do not clear or restore all representations consistently. |

---

## “TODAY” source-of-truth map

### Explicit conclusion

There are **10 competing production models/projections of TODAY**.

They often share tables, but each independently selects fields, applies different precedence, or renders a persisted snapshot.

| # | TODAY implementation | Main files | Important divergence |
|---:|---|---|---|
| 1 | Canonical meal/workout evidence ledger | `noam_coach/services/daily_state.py` | Calendar-day bounds; strict workout evidence only. |
| 2 | Shared request snapshot | `noam_coach/services/user_state.py::SharedUserState` | Adds explicit clarification and plan/routine precedence. |
| 3 | Nutrition/AI day snapshot | `nutrition_context.py::build_nutrition_context` | Signed remaining values, planned meals, restrictions, routine, quality. |
| 4 | Next-meal workout/nutrition context | `next_meal.py::build_workout_nutrition_context` | Own meal recency, bedtime, opportunities, budget, workout phases. |
| 5 | Proactive `DailyContext` | `jobs/proactive.py::build_daily_context` | Clamps remaining to zero; raw active session; completed-or-routine workout model. |
| 6 | Telegram daily/post-meal status | `bot/workout.py` | Early return with zero meals omits workout/day timeline; otherwise uses shared state. |
| 7 | Morning menu/briefing/evening projection | `services/health_jobs.py` | Menu uses completed-or-usual workout; briefing directly reads plan time; summary uses strict completion. |
| 8 | Workout prompt/weekly review | `bot/assistant.py` | Mirrored workout-plan fact, raw session, strict completion; weekly counts bot sessions only. |
| 9 | Mini App day view | `mini_api.py`, `miniapp/static/app.js` | Separate dashboard, meal query, raw operations snapshot, client-side stale DOM. |
| 10 | Persisted day projections | `daily_flags`, `daily_menu_state`, next-meal state, `job_state` | Menu/recommendation/planned meals/messages are not tied to a common day revision. |

Specific contradictions:

* Remaining calories: signed in `NutritionContext`, clamped to zero in `DailyContext`.
* Workout completed: explicit report in shared workout state, but not in strict completion.
* Workout day: plan time in morning briefing, historical routine in menu generation.
* Active workout: stale raw session can remain visible in Mini/jobs after canonical resolver drops it.
* Day boundary: midnight for storage, bedtime rollover for timing.
* Pain: infinite active constraint versus 14-day active-region projection.

---

## AI Context Continuity Matrix

Legend: ✓ full; △ partial/indirect; ✗ missing; N/A deterministic/no model call.

| AI feature | Goal + targets | Signed balance + meals/times | Plans/menu | Workout/time/completion | Pain/medical | Allergy/dislike | Learned food/routine/corrections | Health confirmation/freshness | Local time/TZ |
|---|---|---|---|---|---|---|---|---|---|
| Meal image analysis | ✓ via full `NutritionContext` | ✓ | ✓ planned meals; active menu identity ✗ | ✓ | △ food constraints; broader medical context partial | ✓ | learned foods ✓; routine ✓; recent corrections only in reanalysis | △ freshness metadata; confirmation policy inherited from readers | ✓ |
| Manual meal interpretation | ✗ | ✗ | ✗ | ✗ | ✗ except narrow diet safety | allergy/restriction ✓; dislikes ✗ | learned foods ✓; routine/corrections ✗ | ✗ | ✗ |
| Meal image correction | ✓ | ✓ | ✓ planned meals | ✓ | △ | ✓ | learned foods + locked corrections ✓ | △ | ✓ |
| Manual correction without image | ✗ | ✗ | ✗ | ✗ | ✗ | narrow safety ✓ | learned foods ✓; prior correction continuity weak | ✗ | ✗ |
| Daily menu generation | ✓ | ✓ | nutrition plan summary ✓; planned meals ✓ | ✓, but caller’s workout-day boolean can contradict context | ✓ food/medical constraints | ✓ | foods/routine ✓; recent meal corrections ✗ | △ freshness present; confirmation policy inconsistent | ✓ |
| Daily menu editing | N/A deterministic | Reads stored menu, not necessarily current balance | Active menu only | ✗ | Restriction revalidation exists only within edit path | ✓ at edit time | learned foods ✓ | ✗ | Current calendar day only |
| Next-meal recommendation | N/A deterministic | ✓ | Planned meals/titles ✓; active daily menu not authoritative | ✓ via resolver | restrictions partial; pain generally absent | ✓ | learned foods/routine ✓; corrections ✗ | Direct health workout; fact confirmation inconsistent | ✓ |
| Workout assistant/motivation AI | N/A or minimal | ✗ | ✗ | Only a text hint/label | ✗ | ✗ | routine time triggers job; no complete context | ✗ | Current time only outside prompt |
| General assistant intent AI | Goal/weight/frequency only | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Goal explanation AI | Final deterministic target + basis ✓ | N/A | N/A | Only activity assumptions from target basis | ✗ | N/A | ✗ | No source confirmation/freshness metadata | ✗ |
| Evening summary AI | ✓ | ✓ | Planned meals in structured context; active menu identity ✗ | Structured nutrition workout state ✓, but deterministic outer review uses strict completion | △ constraints in structured context | ✓ | routine/foods ✓; recent correction reasons ✗ | △ freshness present | ✓ |

Continuity breaks visible to the user:

* Manual meal AI behaves as though today’s menu, plan, workout, and balance do not exist.
* General assistant AI does not know what another AI feature just recommended.
* Motivation AI receives only a short hint, not the structured workout request that was constructed.
* Recent meal correction text is not a reusable user preference or current-day context field.
* Goal explanation receives deterministic values but not the evidence confirmation/freshness state behind them.
* Active daily-menu identity is not part of next-meal or general-assistant conflict resolution.

---

## State Change Propagation Matrix

| State change | Persistent state updated | Derived state updated immediately | Missing invalidation/continuity |
|---|---|---|---|
| Log meal | `meals`, `meal_items`, approval/event | Fresh totals on next request | Menu, active recommendation, planned meal, routine, Mini DOM, follow-up state |
| Correct meal | Existing meal/items | Fresh totals on next request | Same gaps; old messages and AI correction memory |
| Undo meal | Meal deleted; draft/audit event | Fresh totals on next request | Menu/recommendation/routine/planned meal/follow-up/Mini |
| Change goal/calories | New active goal version | New live target reads | Active menu, active recommendation, planned meals, old callback, Mini DOM; invalidator clears only a few next-meal flags |
| Add allergy | `user_facts` | New generation/final photo validator | Old menu/recommendation/planned actions; `save_chosen_meal` final gate |
| Report pain | Constraint row/facts depending flow | Some load decisions | No one lifecycle; Mini/planning/jobs can disagree |
| Pain resolved | No production transition | None | Constraint can remain active indefinitely |
| Change workout time | Plan/profile update depending path | Some future readers | No today override; old messages/jobs/menu/recommendation remain stale |
| Postpone workout | Daily flag only | Shared resolver/next meal | Plan time, morning, jobs, Mini operation state |
| Cancel workout | Daily flag only unless session cancellation flow used | Shared resolver | Strict completion/jobs/prompts/plan schedule not uniformly updated |
| Complete workout | Session or Health row; clarification may be flag only | Depends on evidence source | Routine/adherence mismatch; old menu/recommendation/messages |
| HealthKit import | Health rows, facts, routine | Fresh direct health readers | Menus/recommendations/plans/Mini DOM/job decisions |
| Server restart | DB remains; global jobs rebuilt | `active_flow` cache partially rebuilt | Rest timers, some secondary continuations, stale cards, missed daily jobs |
| Date changes | New calendar-day keys | New live queries | Open Mini DOM and old Telegram buttons remain; pre-sleep continuity breaks |
| Old Telegram action | Depends on callback | May mutate current object | Many actions lack object/revision identity |
| Mini remains open | None until manual action/refresh | None | All Telegram/job state changes invisible |

---

## Cross-Feature Competition Map

| Competing features | Current winner | Is priority intentional? | Problem |
|---|---|---|---|
| Daily menu vs next meal | No global winner; both shown | No | They can offer competing meal decisions from different snapshots. |
| Daily-menu text edit vs next-meal correction | Daily-menu editor runs first | Contradicts code comment | Correction can mutate the wrong object. |
| Planned meal vs next meal | Planned meals influence context, but recommendation can still offer alternatives | Partial | No rule for whether a plan is commitment, suggestion, or expired intent. |
| Planned meal vs consumed meal | Both remain unless exact title happens to suppress follow-up | No | Same meal can be planned and consumed. |
| Explicit workout clarification vs plan | Explicit wins in shared resolver | Yes locally | Other consumers ignore the resolver, so priority is not system-wide. |
| HealthKit workout vs bot session | More recent wins for current-day resolver | Locally intentional | Historical routine/adherence use different sources. |
| Explicit facts vs learned routine | Depends on feature | No central policy | Stored routine may continue guiding timing after explicit schedule changes. |
| `active_flow` vs secondary continuation | `active_flow` routes first; secondary continuation resumes after answer | No | Invisible old intent can win later. |
| Telegram vs Mini state | Whichever was most recently fetched/rendered | No | No shared revision or push/focus refresh. |
| Proactive job vs live interaction | Nonurgent job checks `active_flow` only | Partial | Secondary flows and just-changed state are not rechecked before send. |
| Safety fact vs old recommendation | Old action remains executable | No | Newly reported allergy can be bypassed. |
| Old callback vs current state | Current active object or raw callback value | No | User can approve something different from what was displayed. |

---

## Restart and Recovery Risk Map

| State | Persists? | Restored? | Restart risk |
|---|---|---|---|
| `active_flow` | Yes | Yes | Generally restart-safe, including suspended snapshot. |
| In-memory pending caches | No | Rebuilt from `active_flow` | Correct only for supported flow types. |
| Secondary `conversation_state` | Yes | Not systematically | Some flows survive invisibly; `deferred_plan` is deleted. |
| Pending approvals/meal drafts | Yes | On demand | Old callback identity can still be stale. |
| Active next-meal recommendation | Yes | Read lazily within six-hour TTL | Not tied to current meal/goal/safety revision. |
| Active daily menu | Yes per calendar day | Read lazily | Remains actionable after relevant state changes. |
| Active workout session | Yes | Raw readers see it | Canonical resolver drops it after six hours, but raw readers do not. |
| Rest timer | No | No | Session step survives while timer/card is lost. |
| Global proactive schedules | No | Recreated | Jobs missed during downtime are not caught up. |
| Job delivery state | Yes | Claim TTL permits later retry | In-memory retry timers disappear; repeating jobs may recover later. |
| Callback debounce map | No | No | Restart removes short replay protection. |
| Telegram message keyboards | Telegram retains them | No centralized invalidation | Old actions remain clickable. |
| Mini App DOM | Browser retains it | No focus refresh | Can survive server restart and show obsolete data. |
| Current day | Recomputed from clock | Yes | Midnight can invalidate state while old clients/messages remain open. |

---

## Telegram vs Mini App consistency map

| Concept | Telegram | Mini App | Consistency |
|---|---|---|---|
| Daily totals | Fresh request through daily-state services | Dashboard fresh only when loaded; meal panel separate | Same underlying data, different client freshness |
| Meal list | Canonical consumed-meal helpers in most screens | Direct query without explicit consumed-status filter | Potential status/filter drift |
| Goal | Active goal reads | Dashboard goal on last load | Mini remains stale after Telegram goal change |
| Next meal | Stores active recommendation for corrections/buttons | Generates response but does not persist the same message-object lifecycle | Cross-channel correction identity absent |
| Workout clarification | Writes shared daily flag | Same endpoint behavior, but client calls it twice | Backend state shared; mutation behavior differs |
| Active workout | Some Telegram flows use canonical resolver; others raw | Operations uses raw `active_session()` | Mini can show stale active session after resolver drops it |
| Pain | Telegram planning/jobs can use active rows indefinitely | Mini uses 14-day `active_pain_regions` | Can disagree after 14 days |
| Profile/allergies | Telegram fact writers | Mini PATCH writes same facts | Neither invalidates old food actions; Mini only says plan review recommended |
| Plans | Active-plan tables | Same tables | Better aligned, but mirrored workout-plan fact can still diverge |
| Date rollover | New requests use new day | Already-open page does not refresh | Different days visible simultaneously |
| Health import | Telegram/Mini share importer | Mini reloads page after its own upload | Import via Telegram/live endpoint does not refresh an open Mini page |

---

## Continuous-day journey analysis

| Step | What the user believes the coach knows | What the system actually knows |
|---|---|---|
| Morning update | Today’s condition, routine, goals, workout, and menu are one plan | Check-in and menu are separate messages. Menu workout classification uses completed-or-usual-day, not necessarily today’s scheduled plan. |
| Daily status before breakfast | Complete picture of today | Zero-meal early return omits workout state, planned workout, timeline, and next action. |
| Breakfast logged | All remaining plans now account for breakfast | Durable totals update, but active menu, active recommendation, Mini DOM, routine snapshot, and old buttons do not. |
| Next-meal request | Recommendation reconciles breakfast and the menu | Fresh recommendation sees breakfast; existing menu remains a competing stale plan. |
| Meal planned for later | Coach now tracks a specific future meal | Anonymous dictionary stored in `daily_flags`; no lifecycle entity or future consumption link. |
| Workout rescheduled | Every part of the coach now knows the new time | “Later” stores no new time; plan, morning briefing, and jobs retain the original time. |
| Pre-workout recommendation | Recommendation and workout schedule are coordinated | Next-meal resolver may honor the temporary flag; workout job and menu can use old plan/routine state. |
| Workout started | Coach knows an actual session is active | Canonical resolver and raw readers agree initially; stale-session policy later diverges. |
| Pain reported | Every exercise and plan now respects the limitation | Constraint is saved, but pain representations and expiry differ; no later recovery transition exists. |
| Workout completed | Today’s status, meals, summaries, and jobs switch to recovery | If completed in bot/HealthKit, most fresh readers update; if self-reported through next-meal, strict summary/jobs may still say no workout. |
| Post-workout meal logged | Remaining day and planned meal are reconciled | Fresh post-meal status updates, but planned meal/menu/recommendation/follow-up state can remain. |
| Meal corrected | Every summary now uses corrected composition | Live meal totals do; old menu/recommendation/Mini/learned routine do not necessarily update. |
| Evening summary | Coach narrates the final accepted day | It reads current meal rows, but uses strict workout completion and routine cached before the post-summary refresh. Old planned meals may remain pending. |

The largest narrative breaks are:

* “You finished the workout” → “No workout was recorded.”
* “I will respect this allergy everywhere” → old food action remains executable.
* “I updated today after your meal” → daily menu and Mini App remain unchanged.
* “I postponed the workout” → old workout time continues to drive prompts.
* “I know what you planned” → follow-up cannot identify the meal after manual/photo logging.
* “Home/cancel ended that flow” → secondary continuation can resume later.

---

## Revised root causes

1. **No versioned user-reality aggregate.** Facts, day ledger, plans, recommendations, UI messages, and jobs do not share a revision.
2. **Persistence is organized around feature storage, not domain events.** Logging a meal updates the meal table but does not describe every dependent representation that became stale.
3. **Conversation state migration is incomplete.** `active_flow` and `conversation_state` are both production flow engines.
4. **Reader policy is optional.** Confirmation/freshness rules exist, but `get_value()` lets consumers bypass them.
5. **“Today” is a convention, not a service.** Calendar day, sleep day, workout day, and proactive-delivery day are independently derived.
6. **Projections lack dependency metadata.** Daily menu, next-meal recommendation, planned meals, Mini responses, and Telegram buttons do not record the facts/day version used to build them.
7. **Safety validation is generation-centric.** Some generation paths validate restrictions, but final actions do not always revalidate current safety state.
8. **Cross-channel UI is request/response only.** Telegram and Mini App have no common freshness token or invalidation protocol.
9. **Workout history is source-specific.** Current-day resolution improved, but historical learning and adherence still select different sources.
10. **Tests favor isolated behavior.** They verify many helper contracts but rarely simulate mutation → stale projection → old action → restart/job/Mini behavior.

---

## Revised dependency map

| New FIX | Main existing dependencies | New dependencies |
|---|---|---|
| FIX 38 | FIX 2, 3, 5, 16, 35 | FIX 51 |
| FIX 39 | FIX 8, 27, 28 | FIX 40, 52 |
| FIX 40 | FIX 8, 27, 28 | FIX 39, 41 |
| FIX 41 | FIX 6, 7, 18–20, 22, 28, 34 | FIX 44, 49, 57 |
| FIX 42 | FIX 7, 17–19 | FIX 43 |
| FIX 43 | FIX 15–20 | FIX 42, 44, 49, 57 |
| FIX 44 | FIX 15, 16, 32, 33 | FIX 43, 54, 56, 57 |
| FIX 45 | FIX 29, 30, 35 | FIX 51 |
| FIX 46 | FIX 15, 17, 23 | FIX 49, 55 |
| FIX 47 | FIX 1, 2, 23–27 | FIX 48, 54, 55 |
| FIX 48 | FIX 26–28 | FIX 47, 53 |
| FIX 49 | FIX 18–20 | FIX 43, 46 |
| FIX 50 | FIX 10, 16, 23 | FIX 43, 51 |
| FIX 51 | FIX 16, 24, 25, 35 | FIX 38, 43, 45, 55 |
| FIX 52 | FIX 27, 28 | FIX 39, 54 |
| FIX 53 | FIX 23, 26, 34 | FIX 38, 47, 50 |
| FIX 54 | FIX 1, 15, 23, 27, 28, 32, 33 | FIX 43, 47, 52 |
| FIX 55 | FIX 15, 16, 21, 23 | FIX 46, 47, 51, 54 |
| FIX 56 | FIX 33 | FIX 44, 57 |
| FIX 57 | FIX 22 | FIX 38, 43, 44, 49, 54, 56 |

---

## Revised implementation batches — audit recommendation only

No implementation was authorized during the audit phase that produced this document. The following is an execution-order recommendation, not proof that work has started.

### Batch A — Reality versions and atomic state changes

Address FIX 41, FIX 43, FIX 57 and the relevant parts of FIX 15, FIX 22, and FIX 35.

Define:

* Canonical coaching-day key
* User/day state revision
* Atomic typed state mutations
* Projection dependency metadata
* Central invalidation contract

This foundation should precede menu, Mini, callback, and job fixes.

### Batch B — Fact and safety authority

Address FIX 47, FIX 48, FIX 55 and existing FIX 1, FIX 21, FIX 26.

Create explicit fact-reader policies, constraint lifecycle, safety revision, and final-action validation.

### Batch C — Conversation and callback identity

Address FIX 38, FIX 45, FIX 50, FIX 51 and existing FIX 2, FIX 3, FIX 5, FIX 16, FIX 35.

Complete the `active_flow` migration and give every mutation an object/revision identity.

### Batch D — Workout day domain

Address FIX 39, FIX 40, FIX 52 and existing FIX 8, FIX 27, FIX 28.

Centralize:

* Today’s workout event
* Evidence level
* Reschedule/cancel/complete transitions
* Bot/HealthKit reconciliation
* Proactive suppression

### Batch E — Meal and planned-meal lifecycle

Address FIX 42, FIX 43, FIX 46, FIX 49 and existing FIX 15, FIX 17–20.

Unify manual/photo interpretation and introduce planned-meal identity and day propagation.

### Batch F — Menu/recommendation competition

Address FIX 43, FIX 50, FIX 55 and existing FIX 6–16, FIX 21–23.

Tie active menu and recommendation objects to day, goal, meal, workout, preference, and safety revisions.

### Batch G — Telegram/Mini synchronization

Address FIX 44, FIX 56 and existing FIX 32, FIX 33.

Add revision-bearing APIs, focus refresh, optimistic concurrency, and one-request-per-action client behavior.

### Batch H — AI continuity and summaries

Address FIX 46, FIX 53 and existing FIX 23, FIX 34 after the canonical contexts exist.

Build task-specific AI contexts from the same authoritative reality snapshot.

### Batch I — Cross-system regression suite

Address FIX 37 last, but write tests alongside every earlier batch. The final batch should add full-day, restart, old-message, and cross-channel scenarios.

---

## Test coverage gaps

Important missing or insufficiently protected behaviors:

1. Meal creation invalidates active menu and recommendation.
2. Meal correction invalidates old Telegram actions.
3. Meal undo cancels or reopens planned-meal state correctly.
4. Planned meal consumed through photo/manual entry.
5. Allergy added after menu/recommendation generation.
6. Old next-meal callback after a newer recommendation.
7. Old goal callback after a newer confirmation.
8. Home/cancel clears every continuation system.
9. Restart during plan completion/deferred-plan flow.
10. Restart during workout rest timer.
11. Explicit workout completion reflected in every screen/job.
12. Workout cancellation suppresses motivation and workout prompts.
13. Rescheduled workout changes all time consumers.
14. HealthKit-only workout appears in adherence and weekly summary.
15. Bot-only workout affects learned routine.
16. Bot and HealthKit duplicate workout counts once.
17. Pain resolution propagates to plan, load, Mini, AI, and jobs.
18. Calendar midnight before the user’s bedtime.
19. Mini App open while Telegram logs/corrects/undoes a meal.
20. Mini App open while goal/allergy/pain changes.
21. Concurrent `daily_flags` writes preserve unrelated keys.
22. Browser next-meal workout click produces one POST.
23. Zero-meal daily status still shows today’s workout reality.
24. Stale raw active session is hidden consistently.
25. Health import invalidates affected projections but not duplicate-only imports.
26. Manual/photo equivalent meal evidence receives equivalent interpretation context.
27. General-assistant references to the immediately preceding coaching object.
28. Evening summary after late correction and explicit workout report.
29. Old Telegram buttons after date rollover.
30. Proactive sender revalidates live state after claim and before delivery.

---

## Audit-time repository status

Final verification recorded by the audit:

```text
## codex/complete-rec-program-04...origin/codex/complete-rec-program-04
HEAD: bfb56d7f629ac96ac6909f63f140cbfa67af7ca1
```

At the end of that audit, `git diff --exit-code` and `git diff --cached --exit-code` both returned clean. The repository remained unchanged during the audit:

* No code modified
* No migrations created
* No commits created
* No branch changes
* No push
* No PR created

This status records the audit boundary only; later documentation or implementation commits are outside that statement.
