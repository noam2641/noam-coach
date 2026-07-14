# Final Master Implementation Report — FIX 38–57 Correction Backlog

Anchor: `bfb56d7f629ac96ac6909f63f140cbfa67af7ca1` (branch `codex/complete-rec-program-04`).
This report covers commits `752fbd7`..`ea2e5fa` (Batches A–I), produced against
`docs/MASTER_CORRECTION_BACKLOG_ADDENDUM_38_57.md` and
`docs/CLAUDE_REMOTE_IMPLEMENTATION_PROMPT.md`. FIX 1–37's original text was
never provided in this repository; per instruction, no FIX 1–37 item was
reconstructed or guessed — only the addendum's own expansion notes on those
items were acted on, and only where they pointed at concrete, independently
verified code.

## Status legend

- **DONE** — the complete path (handler → service → persistence → derived
  state → UI/old action → restart) was traced, closed, and covered by a new
  regression test that reproduces the originally-reported gap.
- **PARTIAL** — the concretely-evidenced gap named in the addendum is closed
  and tested, but the item's full required behavior (as described in the
  addendum) is broader than what was implemented. The remaining scope is
  named explicitly, not left implicit.
- **NOT TOUCHED** — no code change; either out of batch scope, too large to
  implement safely in this pass, or (for FIX 56) not reproducible against
  current code.
- **BLOCKED BY PRODUCT DECISION** — required a decision that was made
  explicitly during this session (recorded below), not silently assumed.

## FIX 38–57 status table

| FIX | Title | Status | Batch | Evidence |
|---|---|---|---|---|
| 38 | Split-brain conversation continuation | NOT TOUCHED | — | `active_flow` and `conversation_state` (via `noam_coach/services/core.py::set_flow_state`) remain two separate persistence mechanisms. A full unification is a large architectural project; not attempted this pass to avoid a rushed, under-tested change to the core routing engine. |
| 39 | Workout clarification as coherent domain event | PARTIAL | D | `daily_state.workout_completed_today()`'s evidence-only policy is intentional and unchanged (documented, correct). Closed: evening summary (`_evening_coach_review_lines`) now acknowledges a self-report/cancellation instead of flatly contradicting it (`DailyContext.workout_self_reported`/`workout_cancelled_today`). Not closed: `job_motivation`/workout-prompt suppression on cancellation (those jobs don't consult `DailyContext` at all today — a larger refactor). |
| 40 | Postpone workout as real reschedule | PARTIAL | D | Closed: `_planned_session_candidate` no longer reinterprets a postponed-with-no-new-time workout as `PRE_WORKOUT_NEAR` with the stale original time — resolves to `WORKOUT_STATUS_UNKNOWN` with no `planned_start`/`planned_end`, so nutrition timing can't treat it as imminent. Not closed: collecting an actual new time (full reschedule UX) — no text-input flow was added. |
| 41 | Canonical coaching-day boundary | PARTIAL / BLOCKED BY PRODUCT DECISION (resolved) | A | Product decision made explicitly by the user: sleep/wake-anchored coaching day (not strict calendar day). `noam_coach/services/coaching_day.py::resolve_coaching_day` implements this with a confirmed-fact-only policy and calendar-day fallback. NOT migrated: `daily_state.py::local_day_bounds_utc`, `health_service.py::local_day_str`, `daily_menu_state.py::_local_day`, and ad-hoc `datetime.now(TZ).date()` calls in `next_meal.py` still compute independently — the service exists and is tested, but is not yet the sole source of day keys. |
| 42 | Routine profile staleness after meal events | DONE | E | `day_state_invalidation.invalidate_day_projections` (Batch A's contract) now also calls `health_service.save_routine_profile` on every meal create/edit/undo, reusing the existing recomputation path. Tested. |
| 43 | Meal create/edit/undo propagation | DONE | A | `invalidate_day_projections` wired into `meals.py::persist_meal` and `callback_meals.py`'s undo handler; marks active menu stale, clears active recommendation. Tested (Batch A) and further exercised in Batches E/F/I. |
| 44 | Mini App cross-channel freshness | PARTIAL | G | Closed: `visibilitychange`/`window.focus` listeners re-run the read-only loaders (dashboard, next meal, today's meals) on regained focus, debounced. Not closed: revision-token/optimistic-concurrency protocol on mutation endpoints; server never exposes a `day_state_version`. |
| 45 | Rest timer restart safety | DONE | C | New `rest_timers` table with wall-clock deadline; `restore_rest_timers_on_startup` re-arms live timers or resolves expired ones into the "rest ended" card state on process start; a timer whose session already advanced past that step is dropped. Tested (6 scenarios). |
| 46 | Manual meal interpretation context parity | PARTIAL | E | Closed: `analyze_meal_text` gained an optional, additive `nutrition_context` parameter (mirroring `analyze_meal_image`'s payload) and is wired at the primary manual-entry call site with the same context builder photo entry uses. Not closed: full context-model unification (a single `MealInterpretationContext`); photo's overly-broad goal/balance context bias concern also not addressed. |
| 47 | Fact read policy (confirmation/freshness enforcement) | PARTIAL | B | `user_model.get_decision_value`/`get_display_value`/`get_raw_fact` added, declaring explicit policy per the addendum's requirement. Wired into the one new safety-critical read this program added (FIX 55's gate). NOT migrated: ~55 of the ~56 existing `get_value()` call sites across `planning.py`, `goals.py`, `nutrition_context.py`, `next_meal.py`, `meal_validation.py`, `assistant.py` still bypass the policy — a large, individually-risky migration not attempted wholesale. |
| 48 | Pain recovery transition | DONE | B | `resolve_medical_constraints` (by location or by kind) plus a new `pain_resolved` intent (keyword fallback + AI prompt entry + handler) closes the missing lifecycle transition `active_pain_regions()`'s own docstring already anticipated. Tested (4 scenarios, including the exact "pain is gone → every reader agrees immediately" case). |
| 49 | Planned meal identity/lifecycle | PARTIAL | E | Closed: `planned_meal_followup`'s consumed-meal matching widened from exact-string equality to substring containment (catches the common "shorter/longer logged title" case). Not closed: durable planned-meal ID with formal status transitions (planned/consumed/replaced/postponed/cancelled/expired) — plans remain dict entries keyed by fingerprint, not first-class entities. |
| 50 | Next-meal correction vs daily-menu-edit precedence | DONE | C | `meal_text.py::handle_text_message` reordered to try `handle_recommendation_correction` before `try_build_daily_menu_edit_reply`, matching the function's own long-standing comment. Proven with a genuine collision-text test (not vacuous — verified the same text matches both classifiers' trigger conditions). |
| 51 | Versioned state-changing callbacks | PARTIAL | C, F | Closed: `nextmeal:save:N` no longer falls back to freshly-generated options when no active recommendation exists (was the exact mechanism enabling a stale save); `dailymenu:save:` now also rejects a menu marked stale by a meal event, not just a `menu_id` mismatch. Not closed: `confirm:goal_cal:` still carries a raw unversioned value; no general object/revision/single-use token infrastructure was built — each fix is a targeted guard on its specific handler, not a shared mechanism. |
| 52 | Bot/HealthKit workout reconciliation | PARTIAL | D | New `noam_coach/services/workout_reconciliation.py::reconciled_workout_days` (day-level, deduplicated) wired additively into `planning.adherence_snapshot` (`workout_days_reconciled` field) and `assistant.py::build_weekly_summary_text` (HealthKit-only acknowledgment line). Existing bot-only fields (`workouts_completed`, `actual_workouts`) kept their prior meaning for backward compatibility. Not migrated: `routine.learn_workout_pattern` still reads HealthKit only. |
| 53 | General assistant continuity context | PARTIAL | H | `assistant_profile_summary` (the sole context handed to the intent classifier) now includes active recommendation option titles, active daily menu presence/staleness, and active pain locations, each independently best-effort. Not built: a structured, deterministic `AssistantTurnContext` the router consults directly instead of an LLM re-interpreting a text summary. |
| 54 | HealthKit import invalidation | DONE | I | `import_health_export_file` now calls `invalidate_day_projections` when `inserted > 0`, correctly skipping invalidation on a duplicate-only import. Tested end-to-end with a real minimal Apple Health export XML fixture (not mocked) for both branches. |
| 55 | Safety-fact invalidation of food actions | DONE | B | `save_chosen_meal` now revalidates current allergy/diet-restriction facts immediately before persisting via a new `MealSafetyRejected` exception; both `nextmeal:save:` and `dailymenu:save:` handlers catch it and reject instead of silently persisting. Tested with the exact "allergy added after generation" scenario, plus a false-positive guard test. |
| 56 | Duplicate Mini App requests/routes | NOT TOUCHED (re-verified, not reproducible) | G | Both named symptoms (double `setNextMealWorkoutStatus()` call; duplicate `GET /mini/api/next-meal` route) were checked directly against current code and are not present — one click listener, one GET route. The endpoint is also naturally idempotent (status upsert, not append). Recorded honestly as not-reproducible rather than silently marked done. |
| 57 | daily_flags lost-update prevention | DONE | A | New `daily_flags.revision` column (migration 12) plus `noam_coach/services/daily_flags_cas.py::patch_daily_flags` — a bounded-retry compare-and-swap helper. Tested for the exact "two concurrent writers, different keys, both survive" scenario. NOT migrated: existing writers (`health_service.set_daily_flags`, `next_meal._save_daily_flags`, `daily_menu_state._save_daily_flags`) still do unconditional last-writer-wins upserts — the atomic primitive exists and is proven correct, but production call sites were not swept onto it (broad-blast-radius change, deliberately deferred). |

## Existing FIX 1–37 items touched by this program (via addendum expansion notes only)

No FIX 1–37 item was implemented from scratch — their original text was never
available. Where addendum expansion notes pointed at code this program
touched, the note is listed for traceability, not as closure of the
original item:

- **FIX 21 note** (final-action safety bypass) — addressed by FIX 55's work (Batch B).
- **FIX 26 note** (pain-resolution/expiry conflict) — addressed by FIX 48's work (Batch B).
- **FIX 47 dependency on FIX 1/23-27** — the decision-grade API (Batch B) is the mechanism those items would need, but only 1 of ~56 call sites was migrated.

## Architecture-summary sections (per the remote-implementation prompt's Final Completion Standard)

**State/revision architecture**: A `daily_flags.revision` column and CAS
helper now exist (Batch A) and are proven correct, but are not yet the
mandatory path for all `daily_flags` writers. A day-scoped invalidation
contract (`invalidate_day_projections`) exists and is now called from four
independent domain-event sources (meal create/edit/undo — Batch A; routine
profile — Batch E; daily-menu staleness consulted at save time — Batch F;
HealthKit import — Batch I). This is the closest the codebase has to a
shared "something changed today" signal, but it is not a formal versioned
aggregate — it is an imperative call, not a subscribed event bus.

**Canonical coaching-day behavior**: Decided (sleep/wake-anchored) and
implemented as a standalone service (`coaching_day.py`), tested for the
exact 00:30-with-a-01:00-bedtime scenario named in the addendum. Not yet the
system's actual day-key source — every pre-existing day-key call site keeps
computing independently. This is the single largest remaining architectural
gap: the service is correct but not yet authoritative.

**Conversation-state model**: Unchanged. `active_flow` and
`conversation_state` remain two engines. FIX 38 was not attempted.

**Callback stale-action policy**: No general-purpose versioned-token
infrastructure was built. Two specific handlers (`nextmeal:save:`,
`dailymenu:save:`) now have targeted staleness guards proven with tests.
`confirm:goal_cal:` and most other mutating callbacks remain unversioned.

**Workout evidence/reconciliation policy**: The current-day resolver
(`resolve_workout_state`, pre-existing from earlier session work) remains
the single strongest piece of this architecture — evidence-ranked,
temporally-bounded. This program added a day-level bot/HealthKit
reconciliation primitive for historical/adherence reporting (Batch D),
additive to, not replacing, the current-day resolver. Historical routine
learning (`learn_workout_pattern`) was not migrated onto it.

**Meal/planned-meal lifecycle**: Meal create/edit/undo is now a single
domain event with defined downstream effects (menu staleness, recommendation
clearing, routine refresh). Planned meals remain fingerprint-keyed dict
entries in `daily_flags`, not durable entities with a lifecycle state
machine — FIX 49's matching was widened but its identity model was not
replaced.

**Telegram/Mini freshness protocol**: The Mini App now self-refreshes on
regained focus (Batch G). No shared revision token exists between Telegram
and Mini App responses; a mutation from one channel is invisible to the
other until the next poll/focus event, not detected via a conflict/version
check.

**AI continuity context architecture**: The general assistant's classifier
context is meaningfully richer (Batch H) but remains a text summary handed
to an LLM to re-interpret, not a structured context object the router
consults deterministically. Manual meal entry gained planned-meal awareness
(Batch E) but photo and manual paths still use separate context-building
code, not one shared model.

## Full test results

- `tests/` (excluding `regression/` and `acceptance/`): **PASS**, run after
  every batch (9 times) and once more at the end of Batch I. Two
  pre-existing, already-documented flaky tests
  (`test_select_todays_workout_code_still_picks_todays_session`,
  `test_build_daily_status_resolves_workout_state_once`) were deselected in
  batch runs after confirming via `git stash` against the anchor that they
  fail identically with no changes present — unrelated to this program,
  confirmed each time they reappeared.
- `tests/regression/`: **PASS**, run after every batch.
- `tests/acceptance/`: **PASS**, run after every batch.
- New tests added this program: 14 new test files, ~70 new test functions,
  all passing. Ruff clean on every touched/new file at commit time.

## Migrations

One schema migration added: migration 12, `daily_flags_revision` (adds
`daily_flags.revision INTEGER NOT NULL DEFAULT 1`), backward-compatible
(existing rows default to revision 1). One new table added without a
migration entry (`rest_timers` — new `CREATE TABLE IF NOT EXISTS`, applies
automatically on every `init()` per the existing pattern for brand-new
tables). No destructive schema changes. No data migration/backfill required
beyond the column default.

## Commits (this program)

```
752fbd7 feat(batch-a): canonical coaching day, daily_flags CAS, meal-lifecycle invalidation
02de5cb feat(batch-b): fact-read policy, pain recovery lifecycle, meal-save safety gate
f52d7b2 feat(batch-c): rest-timer restart safety, correction-vs-menu precedence, stale save rejection
dc2018b feat(batch-d): workout self-report acknowledgment, honest postpone state, HealthKit reconciliation
79ba23b feat(batch-e): planned-meal match widening, routine-profile refresh, manual-meal context
8c10c17 feat(batch-f): reject daily-menu save when menu was invalidated by a meal event
974e8c3 feat(batch-g): Mini App refreshes on regained focus/visibility
9291876 feat(batch-h): general assistant gains active recommendation/menu/pain awareness
ea2e5fa feat(batch-i): HealthKit import invalidates day-state projections on real change
```

All 9 commits pushed to `origin/codex/complete-rec-program-04`. No PR
created (per explicit instruction). No force-push, no history rewrite.

## Final git status

```
## codex/complete-rec-program-04...origin/codex/complete-rec-program-04
 M coach_bot.py
 M config.py
 M conversation.py
 M models.py
 M noam_coach/bot/assistant.py
 M noam_coach/bot/callback_menu.py
 M noam_coach/bot/callback_plans.py
 M noam_coach/bot/callback_router.py
 M noam_coach/bot/checkins.py
 M noam_coach/bot/meal_text.py
 M noam_coach/bot/meals.py
 M noam_coach/bot/onboarding.py
 M noam_coach/bot/ui.py
 M noam_coach/services/health_jobs.py
 M noam_coach/services/profile.py
 M tests/regression/test_re10_regression.py
 M tests/test_jobs.py
?? tests/test_debug_append_only_messages.py
?? tests/test_routine_confirm_session_minutes.py
```

This is **the exact pre-existing, uncommitted local work from before this
program started** (the earlier-session button/routine_confirm/
confirmed-user-knowledge/debug-append-only-messages work), preserved
byte-for-byte across every batch's commit via hunk-level isolation (verified
after each batch that the working tree still matched this same 16-modified
+ 2-untracked file set). Nothing from this program was left uncommitted;
nothing pre-existing was swept into a batch commit by accident.

## Final answer: does the product now behave like one continuous, reliable coach?

**Not fully — and the gaps above are the honest reason why.** This program
closed 4 items completely (42, 45, 50, 54... plus 43, 48, 55 = 7 DONE
total), meaningfully narrowed 11 more (PARTIAL), left 1 large architectural
item untouched by design (38, given its size and risk), and found 1 item
not reproducible against current code (56).

The most significant remaining continuity breaks, in order of impact:

1. **The conversation-state split-brain (FIX 38) is completely unaddressed.**
   This is the largest single remaining risk — Home/cancel and restart
   recovery still interact with two independent state engines.
2. **The sleep/wake-anchored coaching day exists but is not authoritative.**
   Every legacy day-key call site still computes its own calendar-day
   boundary independently; a late-night user can still see the exact
   midnight-inconsistency the addendum described, because the new service
   was never wired in as the mandatory source.
3. **Most fact reads still bypass the confirmation/freshness policy.**
   `get_decision_value` exists and is correct, but ~55 of ~56 `get_value()`
   call sites were not migrated — the policy is available, not enforced.
4. **`daily_flags` writers still race.** The CAS primitive is proven
   correct in isolation; production writers do not use it yet.
5. **Callback versioning is two narrow patches, not a policy.** Most
   mutating Telegram callbacks (including goal confirmation) remain
   unversioned.

None of the above was silently declared closed. Each is named explicitly in
this report's status table and architecture summary, with the exact
remaining gap stated, so the next implementation pass — whether by this
agent or another — has an accurate starting point rather than a false
"complete" signal.
