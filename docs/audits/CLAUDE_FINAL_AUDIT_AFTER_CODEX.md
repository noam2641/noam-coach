# Final Engineering Audit — Noam Coach — After Codex (2026-06-27)

Independent, evidence-based audit of the current state of the project after
Codex's REC-PROGRAM-04 round. **No code was modified during this audit.**
All findings are derived from the actual code and test runs, not from Codex's
summary documents.

Baseline audited:
- HEAD commit `65fd5f0` ("complete REC-PROGRAM-04 integration and verification", author Codex)
- **plus the uncommitted working tree and untracked files** (where almost all of
  the real fixes actually live — see Finding C-NEW-1).

---

## 1. Executive Summary

- **Ready for use? PARTIAL.** The *code in the working tree* is in good shape:
  the functional fixes from the previous audit (C-1, C-2, C-3, H-1, H-2, H-3,
  H-4) are genuinely implemented, wired into production paths, and backed by real
  tests. The full quality gate is green. **But the project is NOT in a
  releasable/shippable state because the entire fix set is uncommitted and the
  committed HEAD is non-importable** (it imports modules that exist only as
  untracked files).
- **Risk level: HIGH** — driven almost entirely by release/repository integrity,
  not by the functional code. If anyone builds, deploys, or clones from the
  committed history, the bot fails to import. The functional risk on the working
  tree alone is MEDIUM.
- **Tests:** `609 passed, 0 failed, 0 skipped, 3 warnings` (full suite, maxfail
  disabled). Previous audit was `1 failed, 572 passed`. The previously-failing
  test now passes; ~37 net tests added; **no skip/xfail introduced**; only one
  assertion changed (a legitimate migration-count bump 8→9).
- **Top 3 findings:**
  1. **C-NEW-1 (Critical):** 4 production service files and 8 test files are
     **untracked**, and HEAD imports them → commit `65fd5f0` is non-importable.
     A 59 MB `קוד עדכני.zip` source-dump is untracked and *not* gitignored.
  2. **H-NEW-1 (High):** Per-day distinct workout times are not parsed. For the
     spec's exact input, שלישי is stored as 19:00 instead of 18:30 (single
     global time applied to all days). The Monday-disappears / system-time bugs
     *are* fixed.
  3. **M-NEW-1 (Medium):** `NutritionContext` still advertises several input
     fields (prep-time, location, available ingredients, rejection history) that
     no production flow ever writes — empty placeholders sent to the AI as if
     supported. Provisional-goal approval does not auto-continue a blocked plan.
- **Did Codex do good work overall? Yes, on the code.** The implementations are
  real, connected to production, and well-tested — not scaffolding. **But the
  delivery is not trustworthy as committed:** the summary docs claim "completed"
  and a green `git diff --check` / full suite, yet the deliverable (the commit)
  does not contain the files it needs to run.
- **Do the summary docs match the code?** *Functionally yes, structurally no.*
  `UNIFIED_RE6_TRACKING.md` accurately describes the working-tree implementation,
  but the claim that work is "Completed" is contradicted by the fact that the
  files are not in version control.

---

## 2. Git State

- **Branch:** `codex/complete-rec-program-04`
- **Last commit:** `65fd5f0` — Codex — Sat Jun 27 14:15:04 2026 — "complete
  REC-PROGRAM-04 integration and verification"
- **Modified (tracked, unstaged):** 33 files (assistant.py, config.py, db.py,
  mini_api.py, planning.py, recommendations.py, data_quality.py, the whole
  `noam_coach/bot/*` + `noam_coach/services/{availability,health_jobs,profile}.py`
  + `noam_coach/app/runtime.py` + `noam_coach/jobs/proactive.py`, 8 tracked test
  files, and 3 docs).
- **Staged:** none.
- **Untracked (critical):**
  - Production code: `noam_coach/services/food_preferences.py`,
    `noam_coach/services/next_meal.py`,
    `noam_coach/services/nutrition_context.py`,
    `noam_coach/services/telegram_errors.py`
  - Tests: `tests/acceptance/test_rec_next_meal_05.py`,
    `tests/test_availability_parser.py`, `tests/test_food_preferences.py`,
    `tests/test_menu_product_behavior.py`, `tests/test_next_meal_callbacks.py`,
    `tests/test_nutrition_context.py`, `tests/test_plan_completion_flow.py`,
    `tests/test_telegram_lifecycle.py`
  - Docs: `docs/CODEX_AUDIT_INDEPENDENT_20260627.md`,
    `docs/CODEX_AUDIT_REC_NEXT_MEAL_05.md`,
    `docs/FUNCTIONAL_UX_TRACEABILITY.md`, `docs/UNIFIED_RE6_TRACKING.md`
- **Artifacts / files that should NOT be committed:**
  - `קוד עדכני.zip` (59 MB) — untracked **and not gitignored** → a `git add .`
    would commit a 59 MB binary into the repo. (`HealthKit.zip`, `.env`,
    `noam_coach.db` are correctly gitignored.)
- **Verified:** `git cat-file -e HEAD:noam_coach/services/next_meal.py` →
  *"exists on disk, but not in HEAD"*; and `git show HEAD:.../callback_menu.py`
  imports `next_meal`. **Therefore HEAD does not import.**

---

## 3. Test / Check Results

| Check | Command | Result | Notes |
|---|---|---|---|
| compileall | `python -m compileall -q .` | **PASS** (exit 0) | |
| ruff | `python -m ruff check .` | **PASS** ("All checks passed!") | |
| pytest (full) | `python -m pytest -o addopts="" -q -p no:cacheprovider` | **PASS** | **609 passed, 0 failed, 0 skipped, 3 warnings in ~34s** |
| skip/xfail audit | grep for markers in `tests/` | **NONE** | no skip/xfail anywhere |
| evaluations | `python scripts/run_evaluations.py` | **PASS** | `total 33, passed 33, failed 0, pass_rate 1.0` |
| preflight | `python scripts/preflight.py --skip-runtime-secrets` | **PASS** | "Preflight passed" |
| import smoke | import coach_bot + runtime + all new services | **PASS** (working tree) | **would FAIL on HEAD** (C-NEW-1) |
| new-area subset | the 10 new/changed test files | **PASS** | 54 passed |
| http smoke | `scripts/smoke_test.py` | n/a | requires a running server (base_url); not run offline |
| mypy | — | not run | not part of project quality gate (no config, not in Makefile) |

The 3 warnings are pre-existing Starlette `DeprecationWarning`s (test-client
cookie API), unrelated to Codex's work.

**Exact pytest summary line:** `609 passed, 3 warnings in 34.13s`

---

## 4. Verification of Previous-Audit Findings

| # | Finding | Status | Evidence |
|---|---|---|---|
| C-1 | `menu:smartplan` must clear stale `plan_completion` flow | **CLOSED** | `callback_plans.py:456-470` imports `PLAN_COMPLETION_FLOW`/`clear_flow_state`, clears the flow and (guardedly) `clear_pending` before `render_smart_plan_hub`. Uses the constant. `tests/test_plan_completion_flow.py` (6 tests) passes. The previously-failing test now passes. |
| C-2 | `PlanningBlockedError` as UX flow, not crash, with auto-continue | **CLOSED** | `callback_plans.py:527` catches `PlanningBlockedError` **before** the generic `except Exception` at line 529; routes to `_render_planning_blocked` (offers "אשר יעד ואז נמשיך"→`menu:goal`), persists pending via `_set_pending_plan_action`. `callback_menu.py:189-192` calls `resume_pending_plan_action` after full goal activation → auto-continues. No `LOGGER.exception` on the blocked branch. Regression tests present and green. **Gap:** provisional-goal approval (callback_menu.py:136-157) does NOT call `resume_pending_plan_action` (see M-NEW-2). |
| C-3 | Telegram transient classifier, dedup, no admin storm, dedicated polling client | **CLOSED** | `services/telegram_errors.py` (classifier + fingerprint + 600s admin dedup + token/path redaction); `callback_router.on_error:249-297` uses it (debug on shutdown / warning transient / error fatal; admin only when `notify_admin`). `runtime.build_telegram_app` sets dedicated `get_updates_*` timeouts + pool. `config.py` adds the timeouts with validation. `tests/test_telegram_lifecycle.py` (5 tests) green with real assertions. **Note:** no `bootstrap_retries` added (was a suggestion, not blocking). |
| H-1 | Ordered shutdown in `finally`, no orphan tasks | **CLOSED (unit-level)** | `runtime.run:287-309` — `shutting_down` set first; `_stop_api_server`→`_stop_telegram_application`(updater then app, guarded by `running`)→`_cancel_task(cleaner)` all in nested `finally`. `test_stop_telegram_application_stops_updater_before_application` asserts order. **Not** verified end-to-end under a `server.serve()` exception (no full-run failure-injection test) — Codex itself flags "live Telegram verification still required." |
| H-2 | DB constraint for single active goal | **CLOSED** | `db.py` migration 9 dedupes existing duplicates (keeps `active` over `active_provisional`, sets losers to `superseded`) then `CREATE UNIQUE INDEX IF NOT EXISTS ux_goal_versions_single_current ON goal_versions(user_id) WHERE status IN ('active','active_provisional')`. Idempotent. `test_single_active_goal_migration_cleans_duplicates_and_adds_constraint` green. |
| H-3 | `planning.activate_goal` supersede `active_provisional` too | **CLOSED** | `planning.py:248` now `WHERE ... status IN ('active','active_provisional')`. `test_activate_goal_supersedes_active_provisional_goal` green. |
| H-4 | Dislike/preference write path + classifier; fed into recommendations | **CLOSED** | `services/food_preferences.py` `record_food_preference_from_slots` writes `disliked_foods`/`preferred_foods`, with opposite-cleanup ("בעצם כן אוהב" cancels dislike). Production wiring: `assistant.keyword_fallback`→`set_dietary_pref`→`record_dietary_preference`→service. `next_meal._restrictions` merges preference restrictions; `_filter_options` + `_matches_free_text_preference` filter disliked items. Tests green (47 in the prefs subset). |
| M-1 | NutritionContext input fields are empty placeholders | **PARTIALLY CLOSED** | hunger/energy/sleep/fasting **are** now written via the flags menu (`callback_menu.handle_flags_callback`). But `available_prep_minutes`, `eating_location`, `available_ingredients`, `recently_rejected_meals` are still read-only with no writer (grep: only `nutrition_context.py` references them). See M-NEW-1. |
| M-2 | next_meal anti-repetition | **CLOSED** | `record_next_meal_served` stores `next_meal_recent_titles`; `_prioritize_fresh_options` deprioritizes recent; "לא מתאים לי N" button → `save_next_meal_option_feedback` regenerates. `test_next_meal_history_prioritizes_fresh_options` green. |
| M-3 | next_meal ignores activity burn | **NOT CLOSED** | `WorkoutNutritionContext` / budget still use target − consumed only; no active-energy field. Low impact; not regressed. |
| L-3 | Dead code after `return` in `planv2:complete_missing` | **CLOSED** | The handler now delegates to `ask_next_plan_completion_question`; the dead block is gone. |

No regressions were introduced by the verified fixes.

---

## 5. New Findings by Severity

### CRITICAL

#### C-NEW-1 — Entire fix set is uncommitted; committed HEAD is non-importable; 59 MB zip not ignored
- **Severity:** Critical
- **Description:** All four new production services (`food_preferences.py`,
  `next_meal.py`, `nutrition_context.py`, `telegram_errors.py`) and all eight new
  test files are **untracked**. HEAD (`65fd5f0`) already imports these modules
  (e.g. `callback_menu.py` imports `next_meal`).
- **Existing behavior:** `git cat-file -e HEAD:noam_coach/services/next_meal.py`
  → "exists on disk, but not in HEAD". A fresh clone/checkout of `65fd5f0` raises
  `ModuleNotFoundError` on startup. Additionally `קוד עדכני.zip` (59 MB) is
  untracked and not matched by `.gitignore`.
- **Expected:** The commit named "complete … and verification" should contain all
  files required to import and run, and no large source-dump artifacts.
- **Repro:** `git stash -u && python -c "import coach_bot"` (would fail) — *not
  executed to avoid disturbing the audited tree*; equivalently confirmed via
  `git cat-file -e HEAD:...` + the import statement in the committed file.
- **User impact:** Build/deploy/clone from history is broken. A naive
  `git add . && git commit` would also bake a 59 MB binary into the repo.
- **Root cause:** Files were created and verified in the working tree but never
  `git add`-ed; the "verification" commit predates (or omitted) them.
- **Recommended fix:** `git add` the four services + eight tests + intended docs;
  add `קוד עדכני.zip` / `*.zip` source-dumps to `.gitignore` (or delete);
  re-commit; re-run the full gate against a clean checkout.
- **Regression test:** A CI job that runs `git clone` (or `git archive | tar`) of
  the commit into a clean dir and executes `python -c "import coach_bot"` +
  `pytest`.

### HIGH

#### H-NEW-1 — Per-day workout times collapse to a single global time
- **Severity:** High (spec §4.8 is explicit about the expected output)
- **Description:** `parse_hebrew_availability_answer` extracts exactly **one**
  `workout_window` via `_parse_hebrew_time` (first match) and applies that same
  `start` to every parsed day.
- **Existing behavior (verified by running the service):** Input
  `ראשון 19:00, שני 19, שלישי 18:30 45 דקות` →
  sun=19:00, mon=19:00, **tue(שלישי)=19:00**, minutes=45.
- **Expected:** sun=19:00, mon=19:00, **tue=18:30**, duration 45.
- **File/function:** `noam_coach/services/availability.py:192-243`
  (`parse_hebrew_availability_answer`, `_parse_hebrew_time`).
- **Repro:** run the function with the spec string (done; output above).
- **User impact:** A user who gives different times per day silently has the wrong
  time stored for some days; the workout plan schedules at the wrong hour.
- **Root cause:** Single-time model; no per-segment association of day↔time.
- **Recommended fix:** Tokenize the input into per-day segments and parse a time
  per segment, falling back to the global/most-recent time only when a segment
  has none.
- **Regression test:** Assert the spec example yields tue=18:30 (currently no test
  asserts distinct per-day times — `test_availability_parser.py` only checks the
  collapsed behavior). **The previous Monday-disappears and "system time
  injected" bugs ARE fixed** — those parts are correct.

### MEDIUM

#### M-NEW-1 — NutritionContext exposes unpopulated input fields as if supported
- **Severity:** Medium
- **Description:** `nutrition_context.py:330-345` reads `available_prep_minutes`,
  `eating_location`, `available_equipment`, `available_ingredients`,
  `recently_rejected_meals` from `daily_flags`, but no production flow writes
  these keys (grep: only the reader references them). They are always None/empty
  yet are serialized into the AI prompt.
- **Expected (spec §4.6):** every field claimed as supported must trace to a real
  source, or be explicitly marked "not yet captured."
- **Impact:** "What to eat now" cannot actually use prep-time/equipment/location/
  rejection-history; the structured payload looks complete but is hollow for
  these fields.
- **Fix:** Either wire a capture path (flags/questions) or omit/annotate the
  fields honestly.

#### M-NEW-2 — Provisional-goal approval does not auto-continue a blocked plan
- **Severity:** Medium
- **Description:** When a nutrition plan is blocked on `active_goal`, the user is
  routed to goal approval. Full activation (`callback_menu.py:189-192`) calls
  `resume_pending_plan_action`. The **provisional** approval branch
  (`callback_menu.py:136-157`) returns before that call, so a user who approves a
  *provisional* goal is left at the provisional confirmation screen instead of
  continuing to candidate generation.
- **Expected (previous audit C-2):** auto-continue after a full **or** provisional
  activation.
- **Impact:** Minor dead-end for the provisional path; user must re-tap "generate".
- **Fix:** Call `resume_pending_plan_action` after `activate_goal_version_provisional`
  as well (or clear the pending action deliberately and tell the user).

#### M-NEW-3 — Full-run shutdown / failure-injection not tested end-to-end
- **Severity:** Medium
- **Description:** H-1's ordering is tested only on the `_stop_telegram_application`
  helper. There is no test that drives `runtime.run` with an injected polling/
  serve error and asserts no orphan tasks / no "Task exception was never
  retrieved". Codex acknowledges live verification is outstanding.
- **Fix:** Add a fake-application/fake-server harness around `run()` that injects a
  `server.serve()` exception and asserts clean teardown.

### LOW

#### L-NEW-1 — Mojibake (corrupted Hebrew) in two runtime strings
- `noam_coach/app/runtime.py:294`: `raise RuntimeError("Telegram updater ?? ????")`.
- `config.py` runtime-validation messages: `"׳”׳’׳“׳¨׳•׳× timeout ׳©׳ Telegram …"`
  and `"׳’׳•׳“׳ connection pool …"` — encoding-damaged Hebrew the user would see if
  validation fails.
- **Impact:** garbled error text; cosmetic but user-visible on the failure path.
- **Fix:** restore correct Hebrew/English strings.

#### L-NEW-2 — `4.9` validator runs at activation, not at candidate display
- `planning.workout_quality_issues` is real and blocks bad payloads in
  `_validate_plan_for_activation`, and penalizes score at generation — but a
  broken candidate can still be **displayed** in the 3-candidate list before the
  user picks it (it is blocked only on activation). Spec §4.9 prefers
  repair/regenerate so a broken candidate is never shown. Low because activation
  is gated; consider regenerating instead of displaying-then-blocking.

---

## 6. Master-Document Requirements Table

| Task (master scope) | Status | Evidence | Remaining gap |
|---|---|---|---|
| 1. Single source of truth for goals + approval | **Done** | `activate_goal` supersedes both active statuses; migration 9 unique partial index; `fetch_goal` prefers `goal_versions` | — |
| 2. `PlanningBlockedError` friendly business block | **Done (full goal)** | C-2 evidence | provisional auto-continue (M-NEW-2) |
| 3. Telegram polling / pool / graceful shutdown | **Done (unit), unverified (live)** | C-3, H-1 | end-to-end shutdown test (M-NEW-3); live smoke |
| 4. Full regression tests for recorded scenarios | **Done** | 609 green; +37 tests; no skips | tests not in Git (C-NEW-1) |
| 5. Contextual food-recommendation feedback loop | **Done** | `save_next_meal_option_feedback` regenerates; dislike persisted | dislike stored as full option title, not the offending ingredient (works via substring match but coarse) |
| 6. Canonical prefs/allergies/intolerances/restrictions | **Done** | `food_preferences` service + classifier; opposite cleanup | "intolerance" vs "allergy" nuance ("חלב עושה כאב בטן") relies on absence of allergy keyword → falls to help, not stored as intolerance |
| 7. Clean/safe profile rendering | **Done** | `_format_structured_profile_item` strips internal keys/snake_case; regression tests for `missing`/`why_matters` | — |
| 8. Accurate Hebrew availability parsing | **Partial** | Monday-keep + no system-time confirmed | per-day distinct times (H-NEW-1) |
| 9. Workout candidate quality | **Done (gate at activation)** | `workout_quality_issues` + validator block | display-then-block vs regenerate (L-NEW-2) |
| 10. Central nutrition service for AI routes | **Done** | `nutrition_context` used by meals/meal_text/health_jobs/recommendations/profile/next_meal | empty placeholder fields (M-NEW-1) |
| 11. "Today menu" / "what to eat now" behavior | **Done** | signed balances, fasting, restrictions, anti-repetition, default disclaimers | burn calories (M-3) |
| 12. Consistent Active Flow/FSM, minimum typing | **Done** | flow cleared on hub open; router regressions green | — |
| 13. Mini App parity | **Skipped (user direction)** | per tracking doc | out of scope |
| 14. Useful proactive messages | **Done** | quality gates in `proactive.py`/`data_quality.py`; tests green | — |
| 15. Onboarding + Apple Health | **Done (not exhaustively re-verified)** | health_jobs freshness + import security tests green | partial/corrupt-ZIP injection not re-run here |
| 16. Observability / admin alerts / privacy | **Done** | redaction + dedup + events; privacy/retention tests green | — |
| 17. Architecture cleanup / dead code | **Mostly Done** | dead block removed; lint clean | mojibake strings (L-NEW-1) |

---

## 7. Trustworthiness of Codex's Work

- **Were tests weakened to hide failures?** **No.** 40 assertions added, exactly 1
  changed — a legitimate migration-count bump (`range(1,9)`→`range(1,10)`). No
  assertions deleted to pass.
- **Were skip/xfail added to bypass problems?** **No.** Zero skip/xfail markers in
  the entire test tree.
- **Are essential files untracked?** **Yes — critically.** 4 production services +
  8 tests are untracked while HEAD already imports them (C-NEW-1).
- **Are the continuation/summary docs accurate?** **Functionally yes, structurally
  misleading.** `UNIFIED_RE6_TRACKING.md` correctly describes the working-tree
  implementation and even lists "full suite passed". But marking items "Completed"
  is not justified when the files are not committed and HEAD cannot import.
- **Are there completion claims that are not true?** The doc's
  `git diff --check -> passed` and `python -m pytest -q -> full suite passed`
  refer to the working tree, which is honest *for the working tree* — but the
  deliverable artifact (the commit) does not reflect that state.
- **Is there code not connected to production?** **No** — all four services are
  imported and exercised by real Telegram/free-text/recommendation paths.
- **Mocks faking success?** **No** — the new tests use focused fakes
  (FakeLogger/FakeUpdater) for I/O boundaries and assert real behavior; domain
  logic runs against real in-memory DB.
- **Are all migrations in Git?** The migration (db.py change) is in the **tracked
  diff** (unstaged) — present but uncommitted, same as everything else.
- **Are all regression tests in Git?** **No** — the 8 new test files are untracked.

**Net:** the engineering is genuine and good; the *release/version-control
discipline* is not. The biggest risk is not the code — it's that the code isn't in
the commit.

---

## 8. Release Decision

### NO-GO (as the committed `65fd5f0`)
### GO WITH RESTRICTIONS (for the working tree, after committing the untracked files)

**Why:** The functional fixes are real, wired, and tested green, but (a) the
committed history is non-importable, (b) the deliverable is entirely uncommitted,
and (c) a 59 MB zip is one `git add .` away from polluting the repo. None of these
are acceptable for a release tag, even though the working tree behaves well.

**Conditions that MUST hold before use:**
1. Commit the 4 untracked services + 8 untracked tests (+ intended docs).
2. Add `*.zip` source-dumps to `.gitignore` and remove `קוד עדכני.zip` from the
   tree; confirm `.env`/`*.db` stay ignored.
3. Re-run the full gate on a **clean checkout** of the new commit (clone →
   `import coach_bot` → compileall → ruff → pytest → evaluations → preflight).
4. Fix the two mojibake strings (cosmetic but user-visible on failure paths).

**Scenarios safe to run now (working tree):** goal approval + nutrition-plan
block→approve→continue (full goal); "what to eat now" with restrictions/dislikes
and anti-repetition; dislike feedback loop; profile rendering; single-active-goal
invariant; Telegram transient-error handling (unit-verified).

**Scenarios still risky / unverified:** live Telegram polling under real network
flaps and graceful shutdown under load (no end-to-end test); per-day workout times
(H-NEW-1); provisional-goal auto-continue (M-NEW-2); AI fields that are empty
placeholders (M-NEW-1); partial/corrupt Apple Health ZIP injection (not re-run).

---

## 9. Ordered Fix List for Codex

### P0 — Blocks any use/release
1. **C-NEW-1:** `git add` the 4 untracked services
   (`food_preferences/next_meal/nutrition_context/telegram_errors`) and the 8
   untracked tests; commit. **Acceptance:** a clean `git clone` of the new commit
   runs `python -c "import coach_bot"` and the full `pytest` green. **Test:** add a
   clean-checkout import+pytest CI gate.
2. **C-NEW-1 (repo hygiene):** add `*.zip` (or at least `קוד עדכני.zip`) to
   `.gitignore`; delete the 59 MB zip from the working tree. **Acceptance:**
   `git status` shows no large binary untracked; `git check-ignore` matches it.

### P1 — Required before beta
3. **H-NEW-1:** parse per-day workout times. **Acceptance:** the spec input yields
   tue(שלישי)=18:30 while sun/mon=19:00, duration 45. **Test:** add an explicit
   per-day-time assertion to `test_availability_parser.py`.
4. **M-NEW-3:** end-to-end shutdown/failure-injection test for `runtime.run`
   (inject a `serve()`/polling error → assert ordered stop, no orphan tasks).
   **Acceptance:** test fails if updater/app stop is skipped or a pending task
   warning would fire.
5. **M-NEW-2:** auto-continue (or explicitly clear + message) after provisional
   goal approval. **Acceptance:** block→approve provisional→candidates render.
   **Test:** integration test on the provisional path.

### P2 — Important improvements
6. **M-NEW-1:** wire or honestly annotate the empty NutritionContext fields
   (prep-time, location, ingredients, rejection history). **Acceptance:** every
   serialized field has a writer or a "not captured" marker.
7. **L-NEW-2:** regenerate/repair broken workout candidates instead of
   display-then-block. **Acceptance:** a candidate with a duplicate exercise is
   never shown in the 3-candidate list.
8. **M-3:** optionally factor activity/burn into the meal budget with a caveat.

### P3 — Cleanup
9. **L-NEW-1:** fix the mojibake strings in `runtime.py:294` and the `config.py`
   telegram-timeout validation messages.
10. Add `bootstrap_retries` to the Telegram builder for first-connect resilience
    (previous-audit suggestion; nice-to-have).

---

*End of report. No files were modified during this audit.*
