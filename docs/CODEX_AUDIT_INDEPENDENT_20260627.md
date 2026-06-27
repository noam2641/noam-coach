# Independent Engineering Audit — Noam Coach — 2026-06-27

Read-only critical audit of the current working tree (Codex's committed work in
`HEAD` = `65fd5f0` plus uncommitted/untracked files). No code was changed.
The auditor's own in-progress edits were reverted before auditing so findings
reflect Codex's work only.

---

## 1. Executive summary

- **Ready for use?** **No — not as-is.** The build is close on the surface
  (lint clean, 572/573 tests pass) but the two highest-priority production
  failures from the supplied runtime log (`PlanningBlockedError` shown as a
  crash; Telegram polling/network instability) are **not addressed** in the
  committed diff, and the committed code **fails one of its own new tests**.
- **Risk level: HIGH.** The failing areas are exactly the ones the user is
  hitting in production (goal→plan flow and Telegram resilience), and a
  documented "fix" is missing from the code.
- **Top 3 failures:**
  1. **Failing regression test + missing fix in `menu:smartplan`.** Codex's
     own untracked test `tests/test_plan_completion_flow.py` asserts that
     opening the plan menu clears the plan-completion flow state; the committed
     `callback_plans.py` does **not** clear it (no `clear_flow_state` at all).
     `pytest` fails (`1 failed, 572 passed`). `CONTINUATION_STATE.md` claims
     this was fixed — it was not.
  2. **`PlanningBlockedError` is still a system error, not a UX flow.** The
     `planv2:generate:` handler catches it under a generic `except Exception`,
     logs it via `LOGGER.exception` (stack trace) and shows `friendly_error`
     ("משהו השתבש"). The user is **not** routed to goal approval and there is
     **no auto-continuation** after approval. This is precisely the log
     symptom #3/#4.
  3. **Telegram resilience unimplemented.** `on_error` logs every error
     (including transient `ReadError`/`PoolTimeout`/`TimedOut`/
     `BrokenResourceError`) at ERROR with full stack trace **and pings the
     admin every time**; there is no transient/fatal classifier, no
     backoff/dedup, no separate `get_updates` client, no bootstrap-retry
     config, and shutdown can skip `updater.stop()`/`stop()` (orphan-task /
     "Task exception was never retrieved" risk).

**Cross-cutting theme:** Codex's new code is well-*shaped* (clean dataclasses,
typed view models, good restriction taxonomy) but a large part of the new read
model is **not connected to any write path**, so in practice it is empty. The
scaffolding exists; the user-facing wiring does not.

---

## 2. Findings by severity

### CRITICAL

#### C-1 — Committed code fails its own regression test (`menu:smartplan` does not clear plan-completion flow)
- **Existing behavior:** `noam_coach/bot/callback_plans.py:376-378` —
  `if data in {"menu:smartplan","menu:plan"}: await render_smart_plan_hub(...)`.
  No `clear_flow_state` / `clear_pending`. The module never imports
  `clear_flow_state`.
- **Expected:** Opening the plan hub must clear the `plan_completion` flow so a
  user mid plan-completion is not silently stuck; `tests/test_plan_completion_flow.py:103`
  asserts `get_flow_state(... PLAN_COMPLETION_FLOW) is None`.
- **Repro:** `python -m pytest tests/test_plan_completion_flow.py` →
  `test_plan_menu_clears_completion_flow_without_name_error` FAILS
  (`assert {...'step':'sex'...} is None`).
- **Impact:** User stuck in plan-completion who taps the plan menu stays in a
  stale flow; subsequent free text is misrouted to the old question. Also: the
  whole suite is red (`--maxfail=1` in `pytest.ini`).
- **Suggested fix:** In the `menu:smartplan/menu:plan` branch, import and call
  `clear_flow_state(user_id, onboarding.PLAN_COMPLETION_FLOW)` (and
  `clear_pending`) before rendering. Use the constant, not the literal.
- **Test to add:** keep the existing test; add one asserting free text after
  tapping the hub is **not** treated as a plan-completion answer.

#### C-2 — `PlanningBlockedError` surfaces as a generic crash; no goal-approval routing, no auto-continuation
- **Existing behavior:** `callback_plans.py` `planv2:generate:` (~line 474-492)
  wraps `planning.generate_candidates` in `try/except Exception`, calls
  `LOGGER.exception("Candidate generation failed")` and
  `friendly_error(exc, "plan generation")`. `build_nutrition_candidates`
  (`planning.py:441-448`) raises `PlanningBlockedError("צריך לאשר יעד לפני
  יצירת תוכניות תזונה", missing=["active_goal"])`.
- **Expected (master spec §4.1, §5 Task 2, Definition of Done #3):** a business
  precondition must be logged at INFO (no stack trace), shown as a clear flow,
  and route the user to goal approval; after approval the original request must
  continue automatically (`continuation_intent`).
- **Repro:** As a user with readiness met but no active goal, tap
  "צור הצעות תזונה" → log shows
  `planning.PlanningBlockedError ... ERROR` + user sees "משהו השתבש".
- **Impact:** Exactly the production log #3/#4. Dead-end UX; misleading ERROR;
  no path forward.
- **Suggested fix:** Catch `planning.PlanningBlockedError` *before* the generic
  `except`. If `"active_goal" in exc.missing`: persist a `goal_continuation`
  flow-state (`plan_type`) and render the goal-approval screen; in
  `handle_goal_callback`, after a successful (full **or** provisional)
  activation, read that flow-state and auto-run `generate_candidates` +
  `render_candidate_list`. For missing facts, show a specific
  "complete exactly these" CTA. Log blocked cases at INFO + a `PLAN_BLOCKED`
  event.
- **Test to add:** `blocked → goal screen → approve → candidates rendered`
  integration test; assert no `LOGGER.exception` / generic-error text.

#### C-3 — Telegram transient-error handling is missing (log spam, admin-alert storm, no backoff/classifier)
- **Existing behavior:** `noam_coach/bot/callback_router.py:249-274` `on_error`:
  logs **every** error with `exc_info` and calls `notify_admin` unconditionally.
  `noam_coach/app/runtime.py:173-209` `build_telegram_app` sets pool/connect/
  read/write timeouts but: no separate `get_updates_request`, no
  `pool_timeout`-aware split, no `bootstrap_retries`/`get_updates` polling
  timeout, no transient-vs-fatal classification, no backoff, no log dedup.
- **Expected (master spec §5 Task 3, DoD #11/#12):** classify
  `NetworkError/TimedOut/ReadError/BrokenResourceError/PoolTimeout` as transient
  → dampened log + exponential backoff w/ jitter; admin alert only on
  persistent/structural failure; separate polling client; graceful shutdown
  with no orphan tasks; health endpoint reporting `telegram_ready/degraded`.
- **Repro:** Disconnect network briefly while polling → repeated ERROR stacks +
  repeated admin pings (matches supplied log #1).
- **Impact:** Log/alert noise hides real faults; no resilience signal; possible
  user-facing "פעולה נכשלה" on transient blips.
- **Suggested fix:** Add a transient classifier; in `on_error` log transient at
  WARNING with rate-limited dedup and **no** admin alert; keep admin alert for
  unexpected/persistent. Configure a dedicated `get_updates` request object and
  `bootstrap_retries`. Add backoff counters + a degraded flag surfaced on the
  health route.
- **Test to add:** failure-injection unit tests feeding `ReadError`/`TimedOut`
  through `on_error` asserting WARNING + no admin alert + dedup.

### HIGH

#### H-1 — Shutdown path can skip `updater.stop()` / `application.stop()` (orphan task / "Task exception was never retrieved")
- **Existing:** `runtime.py:257-276` — the stop calls (`telegram.updater.stop()`,
  `telegram.stop()`) are placed **after** `await server.serve()` *inside* the
  `async with telegram:` block. If `server.serve()` raises or the task is
  cancelled, those lines are skipped; only `async with` `__aexit__` runs, and
  the `cleaner` cancel is the only `finally` cleanup. Polling may not be stopped
  deterministically before shutdown.
- **Expected (Task 3 §12):** explicit ordered shutdown — stop accepting work,
  stop scheduler, stop polling, await/cancel tasks, close clients; no orphan
  task warnings.
- **Impact:** Matches log #2 (`Task exception was never retrieved`) on shutdown.
- **Fix:** Move updater/app stop into a `finally`; await pending tasks; guard
  with `suppress(CancelledError)` only where genuinely expected.
- **Test:** startup/shutdown smoke with injected polling error asserting clean
  teardown (no pending-task warning).

#### H-2 — No DB-level guarantee of a single active goal
- **Existing:** `goal_versions` has only `idx_goal_versions_user` (non-unique);
  single-active invariant is enforced purely in app code
  (`planning.activate_goal`, `goals.activate_goal_version*`). Sessions get a
  unique partial index (`ux_one_active_session_per_user`) but goals do **not**.
  Two current statuses coexist by design (`active`, `active_provisional`).
- **Expected (master spec Task 1.3, audit §א):** no two active goals in
  parallel, ideally backstopped by a constraint.
- **Impact:** A bug or race in the two-step supersede/activate transaction could
  leave two `active`/`active_provisional` rows; nothing in the DB prevents it.
  `active_goal()` masks it via `ORDER BY id DESC LIMIT 1`, hiding duplicates.
- **Fix:** Add a unique partial index
  `ON goal_versions(user_id) WHERE status IN ('active','active_provisional')`
  via a new idempotent migration (after de-duping existing rows), OR add an
  invariant test + assertion. Verify the activate transaction supersedes
  **both** statuses (it does in `goals.py`; `planning.activate_goal` only
  supersedes `status='active'`, **not** `active_provisional` — see H-3).

#### H-3 — `planning.activate_goal` does not supersede an existing `active_provisional`
- **Existing:** `planning.py:227-254` supersedes only `WHERE status='active'`.
  If a user has an `active_provisional` goal and then a full goal is activated
  via this path, the provisional row is **not** superseded → two current goals.
- **Expected:** Activating a goal supersedes any current goal regardless of
  `active`/`active_provisional`.
- **Impact:** Split-brain target; `fetch_goal`/`active_goal` may return the wrong
  row depending on `id` ordering.
- **Fix:** Change the supersede clause to
  `status IN ('active','active_provisional')` (as `goals.activate_goal_version`
  already does). Add a test approving provisional then full.

#### H-4 — Preference/dislike model is read-only: `disliked_foods` / `preferred_foods` are never written
- **Existing:** `nutrition_context.py:328-329` reads `disliked_foods` /
  `preferred_foods` facts; **no code anywhere writes them** (grep: only the two
  read sites). The typed restriction taxonomy in
  `services/dietary_restrictions.py` supports `preference`, but
  `load_restrictions_from_facts` only consumes `diet_restrictions` (→avoidance)
  and `allergies` (→allergy). There is **no classifier** turning free text like
  "אני לא אוהב טורטיה" into a `disliked_foods`/`preference`.
- **Expected (audit §ג):** "אני לא אוהב טורטיה" stored as a *preference/dislike*,
  not an allergy, and fed into every menu/meal recommendation.
- **Impact:** Dislikes either get lost or, if a user puts them in the allergy/
  restriction field, get misclassified as critical allergies. `next_meal`'s
  `_restrictions()` never loads dislikes, so the "טורטייה חלבון" option is never
  filtered for someone who dislikes tortillas.
- **Fix:** Add a write path + classifier (free text → type) persisting to
  `disliked_foods`/`preferred_foods`; have `next_meal._filter_options` and the
  planner consume dislikes as `preference`-type (`warn`/soft-filter) distinct
  from allergy (`block`).
- **Test:** classify-and-store test for dislike vs allergy; menu excludes
  disliked item.

### MEDIUM

#### M-1 — `NutritionContext` input fields are largely unpopulated placeholders
- **Existing:** `nutrition_context.py:330-345` reads `recently_rejected_meals`,
  `appetite`, `hunger`, `energy`, `sleep_quality`, `available_prep_minutes`,
  `eating_location`, `available_equipment`, `available_ingredients` from
  `daily_flags`/facts that **no flow writes**. They are always empty/None.
- **Expected (audit §ב, Task 10):** the AI nutrition context reflects real
  hunger/appetite/equipment/history.
- **Impact:** The structured AI payload looks complete but carries empty values;
  "what to eat now" cannot truly account for hunger, prep constraints, or
  recommendation history (no anti-repetition).
- **Fix:** Wire at least hunger/appetite and recommendation history (store last
  N suggestions; exclude on next call). Mark the rest explicitly as
  "not yet captured" so behavior is honest.

#### M-2 — `next_meal` recommendation history / anti-repetition missing
- **Existing:** `next_meal._candidate_templates` returns fixed templates per
  phase every call; nothing records or excludes prior suggestions.
- **Expected (audit §ב last bullet):** avoid repetitive suggestions.
- **Impact:** Same 2 options repeat; "history to avoid repetition" requirement
  unmet.
- **Fix:** Persist recent suggestions per user/day; rotate/penalize repeats.

#### M-3 — `next_meal` ignores Health burn / activity calories
- **Existing:** budget uses target − consumed only; `WorkoutNutritionContext`
  has no active-energy field; Health "burn" not factored.
- **Expected (audit §ב):** consider available burn/activity data.
- **Impact:** Calorie budget can be off on high-activity days.
- **Fix:** Optionally add active-energy to the balance with a clear caveat.

#### M-4 — `on_error`/business errors: callback double-tap idempotency not centrally verified
- **Existing:** `fetch_approval` filters `status='pending'` (good idempotency
  for approvals), and a debounce exists (`_is_duplicate_tap`). But plan
  generation (`planv2:generate:`) is not idempotent — a fast double tap can
  start two `generate_candidates` runs (the first `safe_edit("בונה…")` is the
  only guard).
- **Fix:** Guard generation with a short-lived flow/debounce key; rely on
  `save_candidates`' supersede to avoid duplicate candidate sets (it does
  supersede, so DB stays consistent, but two AI/compute runs may occur).

### LOW

- **L-1** `friendly_error` always logs at ERROR; business-expected callers
  (e.g. unified-plan, candidate gen) should differentiate transient vs domain.
- **L-2** `verify_bot_identity` correctly avoids logging the token (good), but
  `on_error` logs `context.error!r` to the admin which may include URLs; ensure
  no token leaks via error reprs.
- **L-3** Several `# REC-...` inline comments reference dead code branches
  (e.g. unreachable block after `return` in `callback_plans.py:387-439`
  `planv2:complete_missing` — code after the early `return` is dead). Cleanup
  per Task 17.1.

---

## 3. Requirements table

| Area | Requirement | Status |
|---|---|---|
| א | Approved goal saved as the active goal | **Done** (`activate_goal`/`goal_versions`) |
| א | Goal persists after restart | **Done** (DB-backed; `load_pending_state`) |
| א | No two active goals in parallel | **Partial** — app-enforced only; `planning.activate_goal` misses `active_provisional` (H-3); no DB constraint (H-2) |
| א | Nutrition plan uses approved goal | **Done** (`build_nutrition_candidates` checks `active_goal`) |
| א | Creating plan with no goal | **Not done** — shows generic crash (C-2) |
| א | `PlanningBlockedError` not a crash | **Not done** (C-2) |
| א | Route to approval + auto-continue | **Not done** (C-2) |
| ב | Eat-now uses full context (cal/protein left, workout timing, meals, restrictions) | **Done (core)** via `next_meal.py` |
| ב | Hunger/appetite, prep, history, burn | **Partial/Not done** (M-1/M-2/M-3) |
| ב | Does not re-ask known info | **Mostly** (reads facts) |
| ג | Separate allergy/intolerance/medical/preference/dislike | **Partial** — taxonomy exists; dislike/preference never written (H-4) |
| ג | "לא אוהב טורטיה" → preference not allergy | **Not done** (H-4) |
| ג | Every menu gets all restrictions+prefs | **Partial** — allergies/avoidance yes; prefs/dislikes no (H-4) |
| ד | No unjustified duplicate exercises | **Cannot verify here** — uses curated templates; no runtime dedup validator (pre-existing) |
| ד | Set/RIR/rest/weight logging, swap, progression | **Pre-existing** (not in this diff); not re-verified |
| ד | One active session, no double-create | **Done** (`ux_one_active_session_per_user`) |
| ה | Buttons over typing, Health reuse, availability parse, recovery, clear next action | **Partial** — availability parsing present; plan-completion flow bug (C-1) |
| ו | Health ZIP valid/large/partial/corrupt, dedup, user id, partial data, no realtime-as-historical | **Pre-existing `health_import`/`health_jobs`**; Codex diff adds freshness fields only; not exhaustively re-verified |
| ז | Telegram pool/timeout/retry/backoff/shutdown/no orphan | **Not done** (C-3, H-1) |
| ח | Jobs dedup/quiet hours/TZ/failure isolation/idempotency/delivery | **Done** (`proactive.claim_job_delivery`) |
| ט | Migrations idempotent, FKs, indexes, constraints, tx/rollback, schema↔code | **Mostly Done**; missing single-active-goal constraint (H-2) |
| י | Tests/lint/compile/integration/failure-injection | **Partial** — lint/compile clean; **1 test fails** (C-1); failure-injection for Telegram absent |

---

## 4. Tests / checks run (actual results)

| Check | Command | Result |
|---|---|---|
| compileall | `python -m compileall -q .` | **PASS** (exit 0) |
| ruff | `python -m ruff check .` | **PASS** ("All checks passed!") |
| pytest (full) | `python -m pytest -q -o addopts=""` | **FAIL** — `1 failed, 572 passed, 3 warnings` (`test_plan_completion_flow.py::test_plan_menu_clears_completion_flow_without_name_error`) |
| pytest (default) | `python -m pytest -q` | **FAIL** — stops at first failure (`--maxfail=1` in `pytest.ini`) |
| preflight | `python scripts/preflight.py --skip-runtime-secrets` | **PASS** (does not run pytest) |
| evaluations | not re-run in this session | n/a |

Note: an earlier "all green" pytest run in this session was contaminated by the
auditor's own in-progress edits (since reverted). Against Codex's code alone the
suite is **red**.

---

## 5. Ordered fix list for Codex

1. **C-1** Make `menu:smartplan`/`menu:plan` clear `PLAN_COMPLETION_FLOW`
   (+`clear_pending`); import the helper; turn the suite green.
2. **C-2** Convert `PlanningBlockedError` to a UX flow: specific catch → goal
   approval routing + `goal_continuation` flow-state → auto-continue after
   approval; INFO log + `PLAN_BLOCKED` event; missing-facts CTA.
3. **C-3** Add transient/fatal Telegram classifier; dampen + dedup transient
   logs; admin alert only on persistent; dedicated `get_updates` request +
   `bootstrap_retries`; degraded flag on health route.
4. **H-1** Make shutdown ordered & in `finally` (stop polling → scheduler →
   await/cancel tasks → close clients); kill orphan-task warning.
5. **H-3** `planning.activate_goal` supersede `IN ('active','active_provisional')`.
6. **H-2** Add idempotent unique-partial-index migration for single active goal
   (after de-dupe) + invariant test.
7. **H-4** Add dislike/preference write path + classifier; feed dislikes
   (`preference`/warn) into `next_meal` and planner.
8. **M-1/M-2/M-3** Wire hunger/appetite + suggestion history (anti-repetition);
   optionally burn calories; or mark uncaptured fields honestly.
9. **M-4** Idempotency guard on plan generation against double-tap.
10. **L-1..L-3** Logging level hygiene; dead-code cleanup; verify no token in
    admin error reprs.

After 1–6, re-run: `compileall`, `ruff`, `pytest` (must be fully green),
`run_evaluations`, `preflight`, plus Telegram failure-injection tests and a
startup/shutdown smoke.
