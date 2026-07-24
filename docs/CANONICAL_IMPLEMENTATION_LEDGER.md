# Canonical Implementation Ledger — Noam Coach

**Single tracked source of truth** for the full-bot repair program: what is
implemented and verified, what is planned, and the evidence behind every claim.
This ledger supersedes scattered status across `MASTER_TASKS.md`, the
`FINAL_TASKS_58_65_IMPLEMENTATION_REPORT.md` (a historical *partial* report, not
the master plan), the FIX 38–57 addendum, and local-only audit documents.

**Update rule:** amend this ledger after every batch/commit; commit it *with* the
implementation work. Never mark an item complete without verifying it against the
actual code **and** a passing test.

**Verification baseline:** branch `develop` @ `608f60d`; full suite
**2377 tests, 0 failures, 0 errors, 1 skipped** (the pre-existing conditional
`.claude/` acceptance skip). Every "COMPLETE" below has its module/function
present in code and its named test in this green suite. Verified 2026-07-23.

---

## 0. Repository & branch state (verified 2026-07-23)

| Fact | Value |
|---|---|
| Canonical repo | `C:\coach_bot\noam-coach` (standalone `.git`) |
| Canonical branch | `consolidation/unified-noam-coach` @ `608f60d` (pushed) |
| Integration branch | `develop` @ `608f60d` (**exists** on origin) |
| `main` | **does NOT exist** on origin |
| `master` | **does NOT exist** on origin |
| **Actual GitHub default branch** | **`codex/complete-rec-program-04`** (the `main` default-branch change was authorized but interrupted — not applied) |
| Current working branch | `feature/dayplan-phase1` (from `origin/develop`) |
| Protected worktree | `C:\coach_bot\noam_coach_complete_release` @ `6d57c04` — **do not touch** |

**Legacy branches (classified — not deleted, do not base new work on):**

| Branch | SHA | Contained in canonical `608f60d`? |
|---|---|---|
| `review/2026-07-18_1` | `b5b32e8` | YES (canonical baseline) |
| `review/meal-observability-batch7` | `f31f047` | YES (ancestor) |
| `review/workout-selection-architecture` | `7d256fa` | YES (ancestor) |
| `codex/post-observability-architecture` | `bf12455` | NO — 1 commit (`.claude/` CI ignore fix) |
| `codex/complete-rec-program-04` | `10a97dc` | NO — 2 commits (PR#1 merge + `.claude/` fix) |

The two un-contained `codex` commits are `.gitignore`/CI hygiene, not product
work. Canonical already ignores `.claude/`.

---

## Part A — Implemented & verified (past work)

Legend: **COMPLETE** = module + test present and in the green suite, with commit
provenance. **PARTIAL** = code present but scope-limited or audit-only.

### A.1 Product Tasks 1–22 — COMPLETE

All 22 re-verified against the repository at the 2026-07-11 audit (suite then
`1093 passed`) and again in the current green suite. Full per-task commit + test
mapping lives in `docs/MASTER_TASKS.md` (authoritative for 1–22). Owning modules:
`routine.py`, `health_service.py`, `planning.py`, `meal_intelligence.py`,
`next_meal.py`, `targets.py`, `daily_menu_state.py`, `nutrition_context.py`.

### A.2 Product Tasks 58–65 — COMPLETE (61 PARTIAL) — **corrects a stale label**

`MASTER_TASKS.md` still lists 58–65 under "Open Backlog", but code + tests exist
for all eight and pass. **Authoritative status is here; MASTER_TASKS is stale
(GAP-D-H1).**

| Task | Owning module (`noam_coach/services/`) | Test | Commit | Status |
|---|---|---|---|---|
| 58 meal-image identity correction | `meal_identity.py` | `test_task58_meal_identity.py` | `a823196` | COMPLETE |
| 59 remaining-day chronological timeline | `day_timeline.py` | `test_task59_day_timeline.py` | `35fe5fc` | COMPLETE |
| 60 workout-time averages + outlier-day approval | `workout_hours.py` | `test_task60_workout_hours.py` | `99d1e47` | COMPLETE |
| 61 pain-aware exercise substitution | `dietary_restrictions.py` | `test_task61_pain_adaptation.py` | audit-only | **PARTIAL** — audited, not authorized for full impl |
| 62 proactive morning briefing + daily menu | `morning_policy.py` | `test_task62_morning_policy.py` | `72d2db1` | COMPLETE |
| 63 plan-completion question dedup + continuation | `question_dedup.py` | `test_task63_question_dedup.py` | `7d2a3c7` | COMPLETE |
| 64 multi-fact free-text profile update | `multi_fact.py` | `test_task64_multi_fact.py` | `31df487` | COMPLETE |
| 65 focused "what to eat now" | `next_meal.py` | `test_task65_focused_next_meal.py` | `1c07ae1` | COMPLETE |

### A.3 Correction backlog FIX 38–57 (B-series) — COMPLETE

Live descendants of FIX 1–37. Owning services: `daily_flags_cas.py`,
`day_state_invalidation.py`, `recommendation_identity.py`, `precedence.py`,
`turn_context.py`, `flow_convergence.py`, `coaching_memory.py`. Tests:
`test_b6…b13`, `test_daily_flags_cas_b1`, `test_callback_grammar_b2`,
`test_coaching_day_b3`, `test_midnight_adversarial_b4`,
`test_recommendation_identity_b5`, and 15 `test_batch_[a-i]_*`. All in the green
suite. (FIX 1–37 are SUPERSEDED by Tasks 1–22 + 58–65, per the addendum.)

### A.4 Meal-interaction correction batches — COMPLETE / SUPERSEDES a local plan

The local `FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md` (see Part C) proposed
ordered batches for Hebrew correction parsing + a count/portion quantity domain.
**These are implemented and merged** — the code carries the batch labels:

| Batch | Evidence | Commit |
|---|---|---|
| 2/3 remove-and-add & replacement parser (`תוריד פלאפל ותוסיף שניצל`) | `meal_intelligence.py:427-486` (root module) | `f1f26df`, `cce73be` |
| 4 count/portion quantity domain (`ParsedQuantity`, "carries NO grams") | `meal_intelligence.py:69-195` | `f1317bb` |
| 5 count→grams conversion | referenced `meal_intelligence.py:76` | (in chain) |

Tests: `test_meal_intelligence.py`, `test_task58_meal_identity.py` (green).
**The local plan is historical prior-art — do not re-implement.**

### A.5 Observability O1–O10 + Review R1–R6 — COMPLETE

10/10 observability + 6/6 review test files present and green (R6 carries the one
conditional skip). Backed by 22 modules under `noam_coach/observability/`. This is
the substrate the forward **AI Gateway** and **Event Bus** build on (see Part B).

### A.6 Consolidation gates G0–G5 (this session) — COMPLETE

| Gate | What | Commit |
|---|---|---|
| G2.0–G2.3B | recover 5 local clusters + durable daily-menu idempotency (D1–D6) | `3a5a00c` … `28ba168` |
| G3 | full regression green; CLI validation-order fix | `ebb63d4` |
| G3B-2B | workout test-clock fix | `95b3ae4` |
| G4 | corrected source-coverage & open-gap registry | `608f60d` |
| G5 | canonical `.venv` (Py 3.12.8) + startup DB-path guard + fresh migration-14 DB | `aabd194` |

Daily-menu idempotency (D1–D6) owning modules: `daily_menu_operations.py`,
`daily_menu_delivery.py`, `daily_menu_refresh.py` (commits `cba7585`, `223dd13`,
`ebf1aa4`, `28ba168`).

### A.7 Consumed-totals consolidation — COMPLETE (verified against the local source-of-truth audit)

The `SYSTEM_DATA_SOURCE_OF_TRUTH` audit's "7 independent consumed-totals
implementations" claim is **disproved on current canonical**: `proactive.py:577`,
`user_state.py:704`, `explainability.py:52`, and `workout.py` formatting all
delegate to (or format) the canonical `daily_state` accessors. Only
`nutrition_context._reported_meals:211` still duplicates the SQL with identical
bounds/filter (consistent-by-copy — a maintenance risk, not a live divergence).
Daily-menu state is properly versioned (`revision` + `menu_id` + `schema_version`),
matching the D1–D6 work. **Consequence:** the DayPlan phase does not need to unify
remaining/consumed totals — it reads them; it unifies only **meal count** (see D-F).

---

## Part B — Forward repair program (planned)

Target direction only — **not a rigid spec**. Sequencing and boundaries adapt to
repository evidence (see Part D deviations). No production code in the forward
program has been written yet. Batch numbers here are **fresh** (`P1.x`) to avoid
collision with the already-complete meal-interaction "Batch 0–15".

**Root cause (verified, corroborated by A.5 + the FIX addendum's "10 separately
assembled representations of TODAY"):** data is gathered once and shared
(`DailyContext`), but decisions are re-derived independently per surface. The
program consolidates decision logic onto canonical owners.

| ID | Component / batch | Fixes findings | Depends on | Status | Acceptance (summary) | Tests | Owning module (proposed) | Legacy to retire (behind compat) | Flag |
|---|---|---|---|---|---|---|---|---|---|
| **P1.1** | **DayPlan** — single nutrition/day owner (count from `typical_meals_per_day`; slots; timeline; **canonical sleep/wake read → fixes R-2b**; remaining/consumed **read** from already-canonical `daily_state`/`nutrition_context`; read-only `workout_window` adapter) | F1 (meal-count part only — consumed-totals already consolidated, D-F), F3, F9, **R-2b** | — | **IN PROGRESS** (this branch) | 3 screens return identical today meal count; DayPlan carries the 18 contract fields; weekly-plan render performs no writes; coaching day correct for BOTH sleep-fact shapes | characterization (3 screens) + parity + no-write regression + **sleep-shape regression** | `noam_coach/services/day_plan.py` (new) | `next_meal._meals_remaining` cap, `planning._meal_slots` count | — |
| **P1.2** | AI Gateway — promote `ObservedOpenAIClient` to own calls + prompt registry + model/retry/timeout policy | R3 timeout; F2 prompt reuse | A.5 obs | PLANNED | one module imports OpenAI SDK; per-purpose model/timeout/retry; typed structured output | gateway unit + purpose-registry | `noam_coach/observability/ai_invocation.py` → promote | 27 direct client imports | — |
| **P1.3** | Event Bus + `Decision<T>` envelope | enables cache invalidation, explainability | A.5 `event_log`/`emit` | PLANNED | `MealLogged` cascades; every model returns `Decision` with confidence+evidence+reason | bus unit + envelope | `noam_coach/services/events.py` (new) | direct cross-service calls | — |
| **P1.4** | UserMemory — typed provenance model over existing `user_facts` columns (`source`, `confidence`, `confirmed`, `valid`, `updated_at`) | precedence honesty | user_facts schema (exists) | PLANNED | explicit>inferred precedence; freshness lowers confidence; corrections raise it | memory unit | `noam_coach/services/user_memory.py` (new) | scattered `get_value` reads | — |
| **P1.5** | Policy layer — collect scattered rules (protein-gap, pre-workout spacing, night floor) | F9 constraints | DayPlan | PLANNED | declared named policies; veto/annotate; feed confidence | policy unit | `noam_coach/services/policies.py` (new) | inline rules in `meal_timing`/`next_meal` | — |
| **P1.6** | MealRecommendationEngine — rank for the current DayPlan slot (context-aware) | F2, F11-clarify | DayPlan, Gateway | PLANNED | no chicken@08:00; consumes slot time/budget; low-conf → clarify | rec parity | `noam_coach/services/meal_recommendation.py` (new) | `next_meal._candidate_templates` slot-blindness | — |
| **P1.7** | WorkoutPlan — one active-plan resolver + sole writer | F5, F6, F7 | — | PLANNED | `workout_catalog` sole read; `activate_plan` sole fact writer; per-weekday times; pre-activation day reassignment | activation regression | `noam_coach/services/workout_catalog.py` → promote | `profile.get_user_plan`, `ui.resolve_todays_workout` bypass | — |
| **P1.8** | DecisionEngine — orchestration pipeline (facts→policies→models→AI→confidence→explain) | F12 | P1.1–P1.7 | PLANNED | single `resolve(intent)`; composes; returns explained Decision | orchestration integration | `noam_coach/services/decision_engine.py` (new) | scattered `build_*_text` builders | — |
| **P1.9** | NavigationGraph — one tree, one Home, deterministic Back | F8 | — | PLANNED | declared graph; every screen routes Home; no `None`-keyboard dead ends | nav unit | `noam_coach/bot/navigation.py` (new) | ~100 back-literals; `menu:more`/`menu:today` aliases; legacy `wk:` vs old | — |
| **P1.10** | Compute-once Cache (per-request, event-invalidated) | perf | P1.1, P1.3 | PLANNED | DayPlan built once per turn; `MealLogged` invalidates | cache unit | (in DecisionEngine) | informal `DailyContext` passing | — |
| **P1.11** | Weight display `per_side` + regional food vocab | F10, F11 | — | PLANNED | dumbbell shows "each hand (total)"; Argaliot not→Alfajores; high-conf clarify for confusables | unit | `exercise_plans.py`, `meal_prompts.py` | — | — |

### P1.1 DayPlan — implementation progress (this branch)

| Piece | Commit | Status |
|---|---|---|
| R-2b coaching-day sleep-schema fix + 8 tests | `10ac87a` | DONE |
| DayPlan contract + meal-count owner + 13 tests | `7e9ebc6` | DONE |
| `build_day_plan` builder + 5 read-only-verified tests | `90d7c07` | DONE |
| CI cross-platform fix (separate) | `8259c60` | DONE |
| Consumer migration + onboarding writer + 9 parity tests | (pending commit) | IN PROGRESS |

**Migration approach (consolidation, not rewrite):** the single legacy count
carrier is `WorkoutNutritionContext.meals_remaining_estimate`, read by BOTH
Today's Status and Today's Menu. A compat bridge
`day_plan.resolve_remaining_meals_estimate` makes that carrier DayPlan-owned
**only when an explicit confirmed `preferred_meal_count` exists** (honors "5–6"),
and returns the exact legacy `_meals_remaining` value otherwise — so existing
users are unchanged and the legacy path is **wrapped, not deleted**.

**Consumers migrated:**
- **Today's Status** (`workout.build_daily_status`) — via the shared carrier. ✓
- **Today's Menu** (`morning_menu_pipeline` → `build_meal_intents`) — via the same
  carrier. ✓
- **Weekly Plan → today** (`onboarding.render_unified_plan`) — live DayPlan
  overlay on today's row only; stored payload NOT mutated; controlled fallback to
  the stored row on any projection failure. ✓

**Writer:** `profile.save_routine_extraction` now persists the stated
`typical_meals_per_day` (+ new `typical_meals_per_day_max` for ranges) as a
confirmed `preferred_meal_count` fact — the **single writer** of that key.

**Remaining legacy callers of `_meals_remaining` (kept, wrapped — retire in a
later batch once all count consumers read the carrier/DayPlan):**
`next_meal._meals_remaining` is still the fallback inside
`build_workout_nutrition_context`; `planning._meal_slots` (weekly-plan STORED
count) is unchanged in Phase 1 (only today's row is re-projected — stored-plan
regeneration is Phase 2, ledger U-2). `meal_intent._slot_plan` still shapes slot
*roles/timing* but its count now flows from the carrier.

---

## Part C — Local-only artifacts (workspace audit)

Read-only inventory. **No local artifact was copied, moved, reset or committed.**
Each doc was fully read and verified against current canonical code (not trusted
as a handoff). Audits were written against `674a1ee` (an **ancestor** of canonical),
so several of their "current defects" are already fixed on canonical.

| Path | Class | Content type | Relevance / verified verdict | Recommendation |
|---|---|---|---|---|
| `noam-coach/` | canonical repo | code | — | active |
| `noam_coach_complete_release/` (+2 worktrees) | protected worktree | code+DB | historical | do-not-touch; retire in a later archive gate |
| `C:\coach_bot\FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md` | untracked local | proposed plan (batches 1–5) | **SUPERSEDED-COMPLETE** — batches merged (`f1f26df`,`cce73be`,`f1317bb`), verified in green suite (A.4) | archive to `docs/archive/` later; do NOT re-implement |
| `…\MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT.md` | untracked local | audit evidence (event-sourced, M1–M8) | **SUPERSEDED for its 2 "current defects"** (remove-and-add + count now parse on canonical — probe-verified); architectural items M5–M8 **merge** into forward Event Bus / Decision envelope | preserve; cite; do not duplicate M-items |
| `…\MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT.md` | untracked local | audit evidence (narrative) | earlier narrative feeding the above; historical | preserve |
| `…\SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT.md` | untracked local | audit evidence (F-01…F-07, evidence-graded) | **HIGH — partly LIVE, partly superseded.** LIVE: **F-02 sleep-schema drift** (verified live bug, see R-2b), F-01 goals split-brain, F-05 dual flow stores. Superseded: "7 consumed-totals impls" **disproved** (now delegate to canonical `daily_state`) — narrows F1 | preserve; cite; drives R-2b + narrows F1 |
| `…\UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN.md` | untracked local | plan+audit (G0–G5) | source for Part A.6 | preserve |
| `…\UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT.md` | untracked local | gate ledger (G0–G5, D1–D6) | source for Part A | preserve |
| `C:\coach_bot\noam_coach.db` | **A-2 empty-DB decoy** (0 bytes) | generated stray | none | leave; delete only in the archive gate |
| `C:\coach_bot\.git` | **stray/malformed** (not a git repo) | — | A-7 gap | leave/ignore |
| `C:\coach_bot\.conda` | Python 3.14 env | — | C-I1 (wrong python) | superseded by `noam-coach/.venv` 3.12.8; leave |
| `noam-coach-private-audit/session_20260718T0621Z/` | **PROTECTED — contains PII** | real captured session: `private_trace_full.json` (2.1 MB), timeline, **meal images**, manifest | source evidence for the meal audits (events 633..1537, head `78770778`) | preserve locally; **NEVER commit** (user PII) — keep out of the repo |
| `C:\coach_bot_BACKUP_20260721_150908\*` | external evidence store | audit evidence (G0–G5) | designated store | preserve |

---

## Part D — Architecture deviations (recorded per instruction)

Repository evidence justified changing the prior plan. Each deviation:

- **D-A — F4 (meal correction) narrowed.** The deterministic parser + quantity
  layer are **already complete** (A.4). The real remaining gap is *only* the
  AI-fallback path: an un-parseable identity correction (e.g. bare "זה חציל
  במיונז", no negation) triggers a full re-analysis that omits the prior meal
  JSON, so portions drift and the item isn't replaced. Forward scope = "send
  prior JSON + image + correction as a targeted patch," **not** rebuilding the
  parser.
- **D-B — F6 (Friday schedule) is PARTIAL, not missing.** `workout_hours.outlier_days`
  + the `hrout:*` confirmation flow already exist; they're wired only to the
  HealthKit wizard. Forward scope = wire the existing flow into onboarding,
  **not** build new outlier logic.
- **D-C — "Batch 0–15" numbering retired.** Those belong to the completed
  meal-interaction plan (A.4). The forward program uses fresh `P1.x` IDs (Part B)
  to prevent collision.
- **D-D — Task 61 kept PARTIAL.** Module + test exist but it was audit-only;
  not promoted to COMPLETE without an authorized implementation.
- **D-E — No `main`/`master`.** The target branch model assumed `main`; the repo
  has neither. Recommendation carried in Part E; not acted on without approval.
- **D-F — F1 (meal-planning divergence) narrowed by the source-of-truth audit.**
  The audit **disproves** the "7 independent consumed-totals implementations"
  claim: on current canonical, `proactive`, `user_state`, `explainability`, and
  `workout` all delegate to the canonical `daily_state` accessors; only
  `nutrition_context._reported_meals:211` still duplicates the SQL
  (consistent-by-copy — a maintenance risk, not a live divergence). So F1's live
  divergence is **meal count** (3 deciders + the `_meals_remaining` cap of 3) and
  the **weekly-plan stored count** — NOT remaining/consumed totals, which are
  already consolidated. DayPlan (P1.1) owns meal count + slots + timeline and
  *reads* the already-canonical remaining/consumed rather than reinventing them.
- **D-G — New live bug found via the local audit (F-02), verified empirically.**
  See R-2b. Recorded as a candidate fix for the DayPlan phase because it corrupts
  `coaching_date`/`wake_time`/`sleep_time` — the very fields DayPlan owns.
- **D-H — Meal-count semantics (product decision, approved + refined).** The stated
  `typical_meals_per_day` (models.py:220) is extracted at onboarding but **never
  persisted** to a readable fact, and `next_meal._meals_remaining` hard-caps at 3.
  Approved resolution:
  - **Persist** the explicitly stated preference as a **canonical *confirmed* user
    preference**, represented as a **range** when the user says e.g. "5–6"
    (fact `preferred_meal_count` = `{min, max}`).
  - **Precedence** for the preferred full-day count: explicit confirmed preference
    **>** learned meal-pattern inference **>** default. Explicit is a **soft
    planning band**, never an invariant. Learned `typical_meal_hours` informs
    **slot timing** and selecting a **feasible count within** the preferred range;
    it must **not override** an explicit preference.
  - DayPlan models **four distinct quantities**: `preferred_meal_count` (full-day
    band) · `selected_planned_meals` (this specific day) · `consumed_meals`
    (completed) · `remaining_meals` (slots left). The remaining count may
    legitimately fall below the preferred minimum late in the day or after meals
    are consumed — **any deviation from the band is stated explicitly in
    `DayPlan.assumptions`/provenance, never a silent fallback to 3.**
  - Legacy `_meals_remaining` cap is wrapped (compat), not deleted; it becomes a
    "remaining within band" adapter.
  - **Regression coverage:** explicit single value; explicit range (5–6); learned
    pattern with no explicit preference; default when neither exists; late-day
    feasibility; completed meals reducing remaining slots; explicit preference
    surviving restart and readable by DayPlan.

---

## Part E — Unresolved decisions & risks

| ID | Item | Recommendation / needed input |
|---|---|---|
| **U-1** | Default branch is `codex/complete-rec-program-04`, not `main` | Create `main` from `608f60d` + set default (option b) was authorized then interrupted — awaiting your go-ahead to resume |
| **U-2** | Weekly-plan future days still render stale stored count | Phase-2 regeneration (out of P1.1 scope); P1.1 fixes only *today*'s row live |
| **U-3** | Task 61 pain-aware substitution audit-only | Decide whether to authorize full implementation |
| **U-4** | Behavioral drift when unifying meal count | Land P1.1 behind characterization tests; the Weekly-Plan count *will* change to honor 5–6 (intended) |
| **U-5** | Legacy branch / worktree / A-2 decoy cleanup | Deferred to an explicit archive gate; nothing deleted now |
| **R-1** | WorkoutPlan write-path consolidation (2 fact-writers, precedence) | Highest-risk forward batch; gate behind a test reproducing "no active program" first |
| **R-2b** | **FIXED (DayPlan Phase 1, sub-batch 1).** Live bug: the coaching-day resolver read only `sleep_schedule.bedtime` (`coaching_day.py`); the onboarding text-edit writer stores `typical_bedtime`/`typical_wake_time` (`onboarding.py:2670`), so onboarding-configured users silently fell back to calendar midnight (empirically verified `_parse_bedtime(onboarding_shape) → None`). **Fix:** `_parse_bedtime` now reads **both** keys with explicit precedence (`bedtime` > `typical_bedtime`) — additive, backward-compatible, no destructive migration. Onboarding-written and Health-imported schedules now yield identical coaching-day semantics (parity test), incl. after-midnight bedtime and non-Israel-timezone regression cases. Commit `cdb8585`; test `tests/test_coaching_day_sleep_schema_r2b.py` (8 tests). **Residual limitation:** future writes are NOT yet normalized to a single schema — the onboarding writer still emits `typical_bedtime`, and ~6 readers (`proactive.py:616`, `onboarding.py:138`, `recommendations.py:160`, `multi_fact.py:180`, display paths) still read `typical_bedtime`-only. Read-side is fully robust now; write-side schema unification is deferred to a later consolidation batch (needs those readers migrated first) to avoid a 6-reader blast radius. Tracked as **R-2b-follow**. |
| **R-7** | F-01 goals split-brain — legacy `goals` table currently divergent on disk, no reader yet (future-code trap) | Watch during WorkoutPlan/goal work; no live impact today |
| **R-8** | `noam-coach-private-audit` session contains user PII (meal images, trace) | Never commit; keep local; if archived, redact |

---

*Ledger created 2026-07-23 on `feature/dayplan-phase1`. Verification: green suite
2377/0-fail/1-skip @ `608f60d`. Update after every batch and commit with the work.*
