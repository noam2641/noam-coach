# Work Manager — Operational State

Compact operational checkpoint (NOT a historical report). Update whenever the
phase changes and before every pause.

| Field | Value |
|---|---|
| Last updated | 2026-08-13 (B0 Track B reconciliation) — **Track A implementation COMPLETE**; Track B backlog reconciled against current `develop` |
| Last verified `origin/develop` | `2895559` — re-verified 2026-08-13 during B0. A13 implementation `a53d94e` proven reachable (`git merge-base --is-ancestor a53d94e origin/develop` exit 0, and merge `26cf83a` likewise); `ruff check .` exit 0; `compileall -q .` exit 0. The delta from the previously-recorded `26cf83a` is **docs-only** (`WORK_MANAGER_STATE.md`, `PERSONALIZED_WORKOUT_ARCHITECTURE_PLAN.md`; PR #81), so the Track A **code** baseline is unchanged. Earlier A13 evidence at `26cf83a`: focused 39, half 1 2815, half 2 411 (**3,226** total) all exit 0; **36/36 deliberate mutations killed** (25 blocker + 11 original), zero survivors; both CI contexts SUCCESS on `a53d94e` (runs 31631507979, 31631512972) |
| GitHub default branch | `develop` |
| Active phase | **Personalized-workout programme, Track A — IMPLEMENTATION COMPLETE.** A1–A7, A9, A10, A11a, A11b, A12 and A13 all merged with reachability proven (PRs #70–#80). A8 is COMPLETE / RETIRED through A2 + A11b. **No Track A implementation item remains.** *(The 2026-07-27 WAVE-1 audit wave is closed; its survivors are listed under Track B below.)* |
| Active task | **B0 — Track B documentation reconciliation (docs-only, this change).** Track A implementation is COMPLETE and no Track A item is active. B0 reconciles the Track B backlog against `2895559` and closes W1-17; **no Track B implementation has been authorized to start.** The reconciled first batch under consideration is W1-16, W1-20 and W1-23 (approval pending; briefs not yet issued). Track B survivors, **W1-10R** and **Track C** remain separate pending work, none of them started. The live runtime is deliberately NOT rolled forward as part of this closeout -- the bot is still serving the pre-A13 commit and a restart is an explicit, separate decision. |
| **PR #78 — stale status wording (recorded 2026-08-12)** | PR #78 registers Track C and is DESIGN-only, but its body still says **A13 is the highest-priority unfinished item**, which stopped being true when A13 merged (`26cf83a`). **PR #78 must be rebased or its wording updated before it is merged**, so nothing it lands claims A13 is unfinished. Not expanded and not merged as part of the A13 closeout. |
| **A12 COMPLETE** (2026-08-03) | **A12** (PR #76, head `ddd73f4`, merge `ed4c9b9`; `git merge-base --is-ancestor ddd73f4 origin/develop` exit 0). Promotes a repeated substitution to a preference after **two consecutive** occurrences, asked at the end of the workout, with a 56-day decline / 365-day approve cooldown. An independent review found the first submission defined a detector, a proposal and a consumer while **nothing in production called any of them** — every service-level test passed on an unreachable feature. Three genuine completion sites now reach `offer_after_workout` (`callback_session.py:235/447/788`); the reversible explicit-finish path is deliberately excluded. Consecutiveness reads what was actually PERFORMED (`sessions`+`sets`, completed only, split-secondary excluded, first-set-wins, slot identity), because an audit-only history cannot see the sessions where the user trained the programmed exercise and A,B,A would still have promoted. Evidence validates ownership and re-derives the split signature from the named plan version, so forged provenance fails closed. **Mutation results: 17/17 original killed; review set 12 killed, 1 measured-equivalent (RV-5 `LEFT JOIN`, provably inert), 0 unexplained.** Two guards were found broken by measurement rather than by reading: RV-7 (a duplicate proposal logged `ERROR substitution_proposal_failed` with a stack trace on the most common benign path) and **TR-1 — a product guard that had silently stopped guarding**, scanning a fixed 3000-character window that a later edit pushed the approve branch outside of. |
| **Track B residuals (reconciled 2026-08-13 against `2895559`)** | Per-item current state, verified in code. **W1-11** OPEN — `noam_coach/observability/obs_context.py:122-123` still resets `_span_id`/`_parent_span_id`; exactly two `span_scope()` sites (`telegram_ingress.py:367`, `ai_invocation.py:198`), never nested, so `parent_span_id` has no producer. Note `trace_reader.span_children()` (`:68`) has **zero consumers, not even a test** — so the honest outcome may be retiring the unused reader/claim. **Any such retirement is NON-DESTRUCTIVE: the `product_events` correlation envelope is a backward-compatible architecture contract (`emit.py:118-149`, `event_log.py:57-59`, migration 13) and its nullable fields are retained. No column removal or destructive migration is authorized.** **W1-13** residual only — product-wide logging rose **57 → 108** calls, so the original count target is MET and retired; what remains is that `noam_coach/bot/workout.py` has **0** and `noam_coach/bot/assistant.py` has **1** (the new calls landed in the service layer A12/A13 were already writing). **W1-14** OPEN, both halves — A11b/A12 enriched the *audit row* only (`callback_session.py:878-916` carries `reason`/`slot_id`; `onboarding.py:1230-1237` writes `safety_alert`), and neither site emits a product event. **Now also owns W1-17's transferred sub-concern.** **W1-15** OPEN — the split sub-flow mutates durable flow state (`workout_runtime.py:124-125`) with zero `state.mutated` across three handlers (`callback_session.py:331/347/370`), and the `reps:` leg is unemitted while `rir:`/`ready:` emit, all three registered identically (`ui.py:144/145/158`). **W1-16** OPEN — `before_state` is populated at only **2 of 16** `emit_event` sites in `state_trace.py` (`:130`, `:187`); the plumbing already exists end to end (`emit.py:116→170`), so this is call-site work, not plumbing work; `EVENT_VERSIONS` is an **empty dict** (`taxonomy.py:121`), so `event_version()` returns 1 by construction. **W1-20** OPEN, and deeper than recorded — no `first == last` guard exists anywhere (the only site touching both fields is a display helper, `health_jobs.py:564-566`, truthiness only), `recommendations.py:168-171` still feeds the raw window to the menu-generating AI, and **W1-5's attenuation provably cannot reach these consumers** because they read the `routine_profile` blob directly (`nutrition_context.py:200-208`, raw `SELECT profile`) rather than through `user_model`. **W1-21** OPEN, unstarted — zero Atwater/macro-consistency matches repo-wide; `meal_validation.py:95` is still protein-only; `meal_plausibility.check_item` has five rules and never reads carbs or fat. **W1-22** OPEN but **re-scoped to a wiring gap** — see the reuse row below. **W1-23** half 1 OPEN at **two** sites (`noam_coach/bot/onboarding.py:3455` and `:3550`, both `parsed_items[0]`); half 2 is **already done** (`:3643` splits on `[,\n]+|\s+ו\s+`), though it requires whitespace on both sides, so the plan's own example `"טורטייה ואגוזים"` (prefixed vav, normal orthography) still stays one item. **W1-10R** OPEN, unassigned — the history readers key on `exercise_id` (`noam_coach/services/training.py:716`, `:894`), so a slot whose implementation changes restarts progression; A2's `exercise_index` and A11b's `slot_id` make carry-forward expressible but neither performs it. |
| **W1-22 reuse finding (recorded 2026-08-13, B0)** | **The singularizer already exists — do not build a second one.** `meal_intelligence.py:115` (repo root) maps `יחידות → יחידה` in `_PORTION_UNITS`, but that map is consulted **only** at `:264-265`, on a different parsing path. The materialization path compares `unit` **raw** at `:1479`, `:1487` and `:1491`, so a plural label never reaches `_DISCRETE_UNIT_GRAMS` and `materialize_count_quantity` declines. W1-22 is therefore a **wiring gap, not a missing mechanism**, and its implementation must REUSE `_PORTION_UNITS` rather than add a parallel normalizer. Note also that the unit tables live in root `meal_intelligence.py:1406-1425`, **not** in `noam_coach/bot/meals.py` as `WAVE1_WORK_PLAN.md` states. |
| **Root-vs-package paths (recorded 2026-08-13, B0)** | This repository has a **root-level legacy layer parallel to the `noam_coach/` package**. `data_quality.py`, `db.py`, `event_log.py`, `recommendations.py`, `meal_intelligence.py`, `assistant.py`, `routine.py`, `retention.py`, `config.py` and `conversation.py` are at the **repository root**. Several backlog entries cite package-prefixed paths for these files, which do not exist — a grep at the documented path returns "no such file" and **reads as evidence the defect was fixed**. That is exactly what happened while reconciling W1-17: `noam_coach/observability/data_quality.py` does not exist, while root `data_quality.py:249` does. **Always resolve the real path before concluding an item is closed.** |
| **Process rule (binding, registered 2026-08-13)** | **A planning or status document is evidence of prior intent, not evidence that its technical mechanism is still true.** Any backlog item whose priority depends on a concrete code or runtime claim must be re-derived from current code and, when cheaply reproducible, **measured** before implementation priority is assigned. Registered because W1-17 was reopened on a stated mechanism (SQLite's case-sensitive `IN` defeating a cross-store match) that was carried forward and used to rank it first in a batch; a temp-DB reproduction of the real `track_event` → `product_events` path disproved it in minutes, and the correct disposition turned out to be OBSOLETE with no production work at all. |
| **Process rule (binding, registered 2026-08-12)** | **Mutation experiments run against a COMMITTED baseline.** Deliberate breakage must mutate from a committed tree in a disposable worktree, or from an explicit known restore point, and restore by rewriting the exact captured text — ideally mutate → run → restore inside ONE process so no interruption can leave the tree mutated. **Never restore ambiguous production work with `git checkout -- <file>`.** Registered because A13 lost uncommitted production work to that command three times; each loss was recovered (once by decompiling a `.pyc`, once because the tests failed loudly), but the recovery was luck, not design. A related trap: a stale mutation anchor reports `anchor=0`, which is an UNMEASURED mutation — never a kill — so anchors are re-validated against the current source before every run. |
| **Governance rule (binding)** | **REUSE BEFORE BUILD.** Before adding any new code path, helper, service, table, migration, state model, callback, audit event, guard, API, job or UI component, the assigned agent runs a repository-wide reuse check and records a **Reuse assessment** in the task plan and PR. Prefer extend/repair/generalise; an incomplete mechanism is a candidate for upgrade, not permission to duplicate. NEW requires recorded evidence that the existing architecture cannot support the requirement. Superseded paths are retired, not left beside the new one. The Work Manager verifies the assessment independently before implementation and before merge; a proposal without evidence **returns to planning**. Full text: §2a of `docs/PERSONALIZED_WORKOUT_ARCHITECTURE_PLAN.md`. |
| Approved work outstanding | **Track A**: **NONE — implementation complete.** Done and merged with reachability proven: A1–A7, A9, A10, A11a, A11b, A12, A13. **A8 is COMPLETE / RETIRED through A2 + A11b** — not reopened, not expanded. **W1-44 is closed by A9**; **W1-10 is split into A2 + A8**, with the progression-history residual carried separately as **W1-10R** (below). **Track B** (WAVE-1 survivors, reconciled against `2895559` on 2026-08-13): **W1-11, W1-13** (residual only), **W1-14, W1-15, W1-16, W1-18** (residual only), **W1-19** (read side only), **W1-20, W1-21, W1-22** (wiring only), **W1-23** (half 1 only). **W1-17 is CLOSED as OBSOLETE / SUPERSEDED** — see its row below; it is no longer a Track B survivor. **W1-24–W1-36** (UX/copy) remain open and non-parallelizable. **W1-37–W1-43** remain **BLOCKED by live-DB governance** — not obsolete. |
| W1-10R (registered 2026-08-03) | **Preserve progression history across a slot implementation change.** The residual left after W1-10 was split into A2 + A8: when the exercise occupying a slot changes (substitution, promotion, plan rebuild), the load/progression history keyed to the previous implementation is not carried forward, so progression restarts. A2 gave `sets` a stable `exercise_index` and A11b gave slots a stable `slot_id`, which together make the carry-forward *expressible* — neither performs it. Owner: unassigned. **Not scheduled**; recorded so it cannot vanish into "A8 is done". |
| **A13 COMPLETE** (2026-08-12) | **Persist the load decision to audit.** PR #80, head `a53d94e`, merge `26cf83a`; `git merge-base --is-ancestor a53d94e origin/develop` exit 0. Records the decision at the two surfaces that PRESENT a prescription the athlete acts on — the Telegram card and the Watch payload — and at neither of the other seven `recommend_load*` call sites, which are previews or recomputations (`ui.py` calls it per exercise in a render loop). Durable semantic: **one row per recommendation TRANSITION** per `(user, session, exercise_index, set_number, exercise_id, channel)`, compared against the LATEST row only. Uses the canonical allowlisted `write_audit`; **no migration**, no raw INSERT, no load-behaviour change. Six review rounds produced six correctness blockers and execution found nine more. The load-bearing ones: recording 'presented' for a card that never arrived (`safe_edit` swallows a failed stale-message fallback); an unowned `ensure_future` collectable mid-write; unbounded duplicate rows from Watch polling; a key missing **A2's `exercise_index` occurrence identity**, so two occurrences of one movement collided; history-wide dedupe that suppressed a return to an earlier value, leaving the LAST row contradicting what was last shown; and a three-facet cancellation edge — `await previous` killed the predecessor's in-flight write, `suppress(BaseException)` swallowed the follower's own cancellation so it recorded beside a running predecessor, and a cancelled tail popped the chain key. **36/36 mutations killed.** Owner: Observability. |
| W1-18 / W1-19 (partial — re-scoped 2026-08-13, B0) | Both remain **PARTIALLY COMPLETE**; neither is closed. **W1-18** — the previous claim that it "was partially fixed incidentally by W1-7 rather than by direct work" is **withdrawn as factually wrong**. Its headline defect ("meals-per-day has exactly one entrance, and it was never opened") is **CLOSED, by direct work**: a second entrance exists at `noam_coach/bot/assistant.py:710-712` (`set_meal_frequency` → `_handle_meal_frequency` → `persist_preferred_meal_count` at `:752`), and `resolve_remaining_meals_estimate` reads the preference (`noam_coach/services/day_plan.py:326`) and clamps at **10**, not 3 (`day_plan.py:43-44`). The real residual is narrower and lives elsewhere: (a) `MEAL_SPACING_HOURS = 2.5` truncates any stated band ≥5 from mid-afternoon (`day_plan.py:263-267`) while its own docstring claims it bites only late in the day; (b) **`noam_coach/services/nutrition_context.py:402` is an independent third estimator** hard-defaulting to 3 and ignoring the preference — the real surviving "cap of 3", in a different file than the plan named; (c) the bridge at `noam_coach/services/next_meal.py:648-649` is wrapped in a bare `except Exception` that silently degrades to the legacy 3-cap with no logging. **W1-19** — the `DONE (#40)` mark in `WAVE1_WORK_PLAN.md` is **unsupported and withdrawn**; the two-resolver framing is stale. The surviving half is the **read side**: `proposal_only` is written once (`noam_coach/services/goals.py:409`) and has **zero production readers**, so an unapproved 0.55-confidence proposal is consumable as if approved. The 2100 fallback also survives (`nutrition_context.py:441` → `:318-325`, `config.py:83`), with `next_meal.py:418` contributing a fourth number (2000). Not scheduled; not to be recorded as done without evidence. |
| W1-24 – W1-36 (UX/copy) | **OPEN, and still NOT parallelizable with any logic lane.** Re-verified 2026-08-13: there is **no central strings module** in the repository, so Hebrew literals remain inline in the handlers that own the logic. Two spot-checks now carry code evidence: **W1-33** — `noam_coach/bot/callback_menu.py` ships the home button as both `⬅️ תפריט` (`:173`, `:185`, `:307`) and `🏠 תפריט` (`:477`, `:504`, `:521`) from one file; **W1-29** — 12 ASCII `ק"ג` vs 31 gershayim `ק״ג` occurrences across `noam_coach/`. The remainder of the band is still UNVERIFIED in either direction. |
| W1-37 – W1-43 (data hygiene) | **BLOCKED BY GOVERNANCE — not obsolete, not unverified.** These are live-DB row corrections with **no code component**. Acting on them requires the live database, which the standing constraints forbid (the Bot/API is running and must not be touched or restarted). They stay registered and unschedulable until an explicit, separately-authorized maintenance window exists. Do **not** reclassify them as complete or obsolete on the basis of this row. |
| Evidence standard (unchanged) | Any status change for a Track B item requires a code or git observation, not a document claim — the same standard applied to W1-17 above, which is precisely how the W1-17 reopening mechanism was disproven. |
| Batch 1 evidence | A1 `0691da3` — pain mirror isolated from the committed safety write. A2 `09e06d9` — migration 16 adds nullable `sets.exercise_index`; undo rewinds to the performed occurrence. A3 `295bb9f`+`2bd99da` — effort CTA on the rest screen, offered only when RIR is unknown. Each verified to fail without its fix. |
| Corrected 2026-07-29 | This row previously named W1-7 as the active task and listed W1-7/W1-8 as outstanding. **Both are merged and live in code** — verified on `develop`. W1-10 is split into A2+A8; W1-44 is absorbed into A9. *(The "W1-17 (#42) … done" clause in this row is **withdrawn** — see the W1-17 row below. W1-19 (#40) is partially complete, see below.)* |
| **W1-17 — CLOSED: OBSOLETE / SUPERSEDED BY ARCHITECTURE (corrected 2026-08-13, B0)** | **No production implementation remains.** *(History preserved: this row previously read "DONE — #42", which was withdrawn on 2026-08-03 as a misattribution — PR #42 is "Anchor learning windows to the data, not to today (W1-3)", merge `0ac6a0d`, touching only `routine.py` and `tests/test_learning_window_anchoring.py`. That withdrawal was correct. The row was then REOPENED on a technical mechanism which is now itself **disproven** — see below.)* **The reopening mechanism was wrong.** It claimed `noam_coach/bot/callback_router.py:289` writes lowercase `"user_callback"` while `data_quality.py:249` queries uppercase, so SQLite's case-sensitive `IN` "cannot match a single callback row". That analysis read only the FIRST half of `track_event` (`noam_coach/services/core.py:118`), which performs a **deliberate dual write**: `:121-127` inserts into `analytics_events` verbatim, then `:128-139` calls `event_log.append_event(..., event.upper(), ...)` → `product_events` (root `event_log.py:105`). The name is **uppercased on the way into the table the query reads**, so the table+case pairing is internally consistent. **Measured on a temp DB** (migrated 1→17, real `track_event` + real `can_send_proactive`, live DB never opened): `track_event(user,'user_callback')` → `analytics_events=['user_callback']`, `product_events=['USER_CALLBACK']`, and `can_send_proactive` → `(False,'user_recently_active')`; the `USER_MESSAGE` path suppresses identically. **Formally withdrawn: "proactive messages are never suppressed for a recently-active user" and "the query reads the wrong table / wrong case".** The suppression path is CORRECT and must not be "fixed": `data_quality.py:249` is the one uppercase-dependent reader, and `tests/test_analytics_dsar_redaction.py:144/160-161/167` deliberately pin BOTH spellings in `analytics_events` to prove redaction is case-agnostic — normalizing either spelling breaks them. **The three-store premise is the intended architecture, not a defect:** `noam_coach/observability/__init__.py:17-19` declares `audit` (domain/business audit trail) and `analytics_events` (compatibility/product metrics) **"intentionally remain separate stores with separate purposes"**, layered under `product_events` as the canonical interaction trace (`event_log.py:8-16`, `config.py:94`). **No migration 18** — see the row below. The only surviving sub-concern (safety evidence reachable only via `audit`) is **transferred to W1-14**, whose own fix (emit a product event at the `safety_alert` and `approve_substitution` sites) closes it with no schema change. |
| **No migration 18 (decided 2026-08-13, B0)** | A migration adding `trace_id` to `analytics_events` / `audit` was considered for W1-17 and is **NOT JUSTIFIED**, on four independent grounds. **(1) Zero blocked consumers.** Every reader was enumerated: `audit` has four — `noam_coach/bot/checkins.py:221` (`user_id`+`action`), `noam_coach/services/substitution_patterns.py:224` (`user_id`+`action`+time, joining `sessions`/`sets`), `noam_coach/services/training.py:474` (A13's own reader: `user_id`+`action`+`entity`+`entity_id`+JSON details), `retention.py:141` (time); `analytics_events` has two (the DSAR per-table dump `scripts/export_user_data.py:45`, and `retention.py:137`). **None keys on trace; none joins across stores.** **(2) The capability already exists and is reusable.** `product_events` carries the full envelope (migration 13, `db.py:1355-1372`) with ambient auto-inheritance (`event_log.py:93-102`), and `write_audit` (`noam_coach/services/core.py:494-522`) already carries `entity`/`entity_id` — a sufficient join key, so W1-14's product event closes the gap without touching `audit`. A second mechanism for a join `entity_id` already expresses is exactly what REUSE BEFORE BUILD forbids. **(3) It would be unevenly NULL and unread.** `write_audit`'s call sites split: handler-originated rows could inherit a trace, but `health_jobs.py` ×3, `health_service.py`, `plan_readiness.py` ×2, `plan_mutations.py` and `training.py` run in job context and could not — a partially-NULL column in a governance store means a `trace_id IS NOT NULL` filter silently under-reports safety evidence. **(4) Retention makes it incoherent.** `config.py:90-100`: `analytics_events` = 180 days, `product_events` = 365 — a trace pointer on an analytics row expires while its target survives. **Reopen only on** a named, approved consumer that must reconstruct an interaction spanning `product_events` AND an `audit`/`analytics_events` row **whose `entity`/`entity_id` cannot express the link.** No such consumer exists at `2895559`. |
| Active worktree | `C:\coach_bot\noam-coach` (integration) — the single remaining worktree; every `C:\coach_bot\wt-*` task worktree is removed. A new one is created per parallel writer and removed after its merge. |
| Protected worktree | `C:\coach_bot\noam_coach_complete_release` @ `6d57c04` — do not touch |
| Protected/PII data | `noam_coach_complete_release\noam_coach.db` (sha `5bd8ac1b…`); `C:\coach_bot\noam-coach-private-audit\` (session trace + meal images) |

## Current objective
**WAVE-1 — defects found in the 2026-07-27 live session.** The plan is
`docs/WAVE1_WORK_PLAN.md` (43 items, five bands). WAVE-0 is closed and its
fixes are confirmed working in production; see §WAVE-1 below for the evidence.

Naming note: an earlier, unrelated batch was also called "WAVE-1" (B1 / R1 /
LOG-004 / UX-01, merged as PR #17). The current WAVE-1 is the 2026-07-27 audit
wave. Where the distinction matters, this document says "WAVE-1 (2026-07-27)".

## Completed batches
- **P1.1 DayPlan Phase 1** — merged (PR #4, `08f953a`).
- **P1.1b DayPlan residuals & data-contract closure** — merged (PR #5, `fbff6f1`).
  Sleep-schema shared accessor + reader-first migration + canonical-only writers;
  Today's Menu count unified with Status/DayPlan; architecture guard; legacy-
  caller reclassification.
- **Repository consolidation + Batch A/B/C cleanup** — merged (PR #6, #7).
- **FS-CLEANUP-1 physical filesystem cleanup** — merged (PR #8). See §FS-CLEANUP-1.
- **WAVE-1** (B1 / R1 / LOG-004 / UX-01) and **WAVE-2** (weight text, rest next
  action) — merged (PR #17, #23, #24).
- **WAVE-0** — **COMPLETE.** All seven PRs merged: #25–#29 (the five lanes),
  #30 (docs), #31 (the migration-15 startup fix found post-merge). See below.

## Active task
WAVE-1 (2026-07-27). **14 PRs merged (#33–#46)**: four P0s, two docs, and eight
WAVE-1 items (W1-1, W1-2, W1-3, W1-4, W1-5, W1-6, W1-9, W1-12). `develop` @
`3d5dd18`, zero open PRs, single worktree.

Next: W1-7 (`build_plan` mutates with no confirmation) and W1-8 (a schedule
correction was discarded while the bot said it agreed) — Lane E, which shares
reach with Lane A and must therefore be serialized rather than run in parallel
with nutrition work.

### Parallel-writer protocol (learned this wave, now standard)
Disjoint file ownership is **necessary and not sufficient**. The first parallel
batch shared one git working tree and the branches stacked on each other —
one agent's commit landed on another's branch, and one had uncommitted work
discarded mid-task. The second batch gave each agent its own `git worktree` and
produced zero collisions.

**Additionally: cross-agent composition must be instructed.** W1-6 was launched
while W1-5 was still changing `set_fact`; its brief told it explicitly not to
defend its confidence value against the other agent's attenuation. It complied,
and the merged result is correct. Left to infer, it would reasonably have
fought the change and the two fixes would have cancelled out.

The database was reset at the owner's request (2026-07-27): both running bot
processes stopped first, a hash-verified backup taken outside the repo, then
rebuilt fresh. The bot runs on it with zero errors.

## Verification discipline for agent findings (added 2026-07-27)

Agent output is a **proposal with evidence, never a finding**. Across two audit
waves, roughly a quarter of agent claims did not survive verification — nine in
WAVE-0 and several more in WAVE-1 — and acting on them would have meant
"fixing" correct code.

Before any claim enters a work plan the Work Manager must:

1. **Reproduce it** against the live database or a copy, not against the
   agent's narrative. Every WAVE-1 critical item was re-run independently
   (restriction matching, the 0.2/4.0 store split, `parent_span_id` nullity,
   the plan-render diff, logging density).
2. **Quantify the user impact.** "The stores disagree" became actionable only
   once measured as 130 kcal/day.
3. **Cross-check against sibling claims.** Several agents reported the same
   root cause from different angles; several others contradicted each other.
   The cross-cutting read is what separates one defect with many symptoms from
   many defects.
4. **Record what was disproven**, in the plan, with the reason — otherwise the
   next session re-finds the symptom and re-fixes working code.

Examples of claims **rejected** at verification in WAVE-1:
- "The approved meal bypassed plausibility." It did not — the check runs on
  every card render; beverages simply disable two of the rules. That is a
  measurement gap, not an enforcement gap, and is recorded as such.
- "The knee constraint was never detected." It was detected correctly, and
  `squat`/`leg_press` both declare `knee` in `joint_load`. The real defect was
  narrower: the weekly-plan renderer never called the warning function.
- "`audit` rows have no corresponding product events." Partly wrong — the
  windows are not empty. The claim survives only for `safety_alert` and
  `approve_substitution`, which have boilerplate but no domain event.

## Blocking dependencies / pending human approvals
None. The owner granted standing autonomy through verified integration and
cleanup, and on 2026-07-27 extended it to `git push`, `gh pr merge` and
`git merge` without per-operation approval.

Two safety constraints remain self-imposed regardless: **a red PR is never
merged**, and protected data (`noam_coach_complete_release`,
`noam-coach-private-audit`) is never touched.

## Migration safety gate (added 2026-07-27 — learned the hard way)

**Any PR that adds or changes a schema migration MUST upgrade a copy of a
real, previously-migrated database before merge.** Green CI and a green full
suite are not sufficient evidence and did not catch the defect below.

Migration 15 shipped in PR #25 recording BOTH version 15 and version 14,
because the `_record_migration` call belonging to migration 14 was inserted
into `_migration_dev_notes`. On a fresh database every migration runs in one
pass and nothing collides, so every test passed. On any database that already
had 14 — i.e. every existing installation — re-inserting 14 violates the
`schema_migrations` primary key, `Database.init()` raises `IntegrityError`,
and **the bot cannot boot**. It was found only by upgrading a copy of the live
database as a post-merge check, and fixed in PR #31.

The generalised guard is
`tests/test_migration_15_dev_notes.py::test_every_registered_migration_records_its_own_version`,
which asserts the recorded `{version: name}` mapping equals
`SCHEMA_MIGRATIONS`. It fails for whichever migration makes this mistake, not
just migration 15. The v14-shaped upgrade fixture in the same file is the
pattern to copy for future migrations.

## Last test evidence
`develop` @ `bb917f1`: both suite halves pass (exit 0, no failures), `ruff
check .` clean, `compileall` clean. CI green in **both** push and
pull_request contexts for PR #25–#31 before each merge.

WAVE-0 added 75 focused tests across seven PRs.

Runtime verification on the reset database: bot boots clean (zero errors in
`logs/session_20260727_075856.log`), migration level 15, 31 tables,
`dev_notes` present. Scheduled jobs carry `tzinfo=Asia/Jerusalem` and resolve
to the correct local times (evening 22:00 IDT, weekly 20:30 IDT, morning
08:00 IDT). The APScheduler lines in the log are python-telegram-bot's
internal JobQueue backend — the repo never constructs a scheduler itself.

The typed-weight coverage was validated by reverting the fix: exactly the
three typed-weight tests failed and the untyped one-tap path stayed green,
confirming the tests fail for the right reason.

## Next exact action
Awaiting a fresh owner-driven session against the reset database. WAVE-0
changed five things the owner will feel immediately (intent classification,
typed weight, `##` notes, proactive messages, the Mini App button), and the
profile is now empty, so a full onboarding pass is the highest-value way to
exercise them. The resulting `product_events` are a better basis for the next
wave than continuing from the plan alone.

Ready-to-run when authorised, from `NOAM_COACH_WORK_PLAN_EXECUTABLE.md`:
**Lane B** (infrastructure — `/help`, voice/video/sticker handlers, medical
disclaimer) and **Lane C** (technical debt — DSAR redaction, Mini App
calendar-vs-coaching-day) are file-disjoint and can run in parallel. The
workout / nutrition / UX lanes share `meal_text.py` and `workout.py` and must
be serialised.

## Prohibited scopes (this session)
AI Gateway · stored weekly-plan regeneration (`planning._meal_slots` Phase 2) ·
destructive sleep-fact migration · any protected-data mutation · any deletion
before approval.

## FS-CLEANUP-1 — Physical filesystem cleanup of `C:\coach_bot` (**COMPLETE**)

Executed 2026-07-25 after the repository-cleanup track completed. **Prove-then-remove**;
read-only inventory first, deletion only when every guard passed. **Status: COMPLETE.**
This task did NOT change any log-audit finding from PROPOSED. Protected-DB baseline
hashes recorded and re-verified unchanged after the task:
`noam-coach/data/noam_coach.db` sha256 `9077e35d…`; `noam_coach_complete_release/noam_coach.db`
sha256 `5bd8ac1b…`.

Top-level classification (C:\coach_bot):
- `noam-coach` — **CANONICAL repo** (develop @ `fc628d9`, clean). KEEP. Do not touch.
- `noam_coach_complete_release` — **PROTECTED** separate clone (branch `review/2026-07-18_1` @ `6d57c04`;
  HEAD NOT ancestor of develop; **18 uncommitted changes** incl. 2 untracked tests; contains `.env`,
  protected `noam_coach.db`, `HealthKit.zip`, session recordings). KEEP (unique uncommitted work + protected data).
- `noam_coach_complete_release.worktrees/{meal-clarification-batch6, workout-selection-architecture}` —
  **PROTECTED** registered linked worktrees (`f31f047`, `7d256fa`). KEEP.
- `noam-coach-private-audit` — **PROTECTED** captured session (zip + session dir; meal images/PII). KEEP.
- `.conda` — root Python environment (~32MB). KEEP (guard F: not conclusively unused; not the canonical `.venv` interpreter but may be an IDE/conda env).
- `.claude` — operator config (`settings.local.json`). KEEP (never tracked/deleted for tidiness).
- 6 root `.md` files (`FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md`, `MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT.md`,
  `MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT.md`, `SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT.md`,
  `UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT.md`, `UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN.md`) — unique audit/planning docs
  that **contain PII** (ledger §Batch-A). **DELETED** — human-approved for deletion; each file was verified
  **SHA-256 byte-identical to its external backup copy immediately before deletion** (all six OK at delete time),
  then all six were removed from the `C:\coach_bot` root. The external backup remains **intact (6/6)** in the
  designated cleanup backup location. Not moved into the repo (would require an approved redaction pass).
- `noam-coach.wt-lease-fix` — empty, git-deregistered lease-fix worktree remnant. **RETAINED** only because a
  persistent OS filesystem lock (`Device or resource busy`) blocks removal, with no attributable user process;
  not force-removed per guard A. Delete the empty directory once the lock clears.

Actions taken this task: **the 6 root PII `.md` files were deleted** (delete-time SHA-256 backup re-verification;
backup intact 6/6). No other item deleted. Generated caches: none at root level (all live inside
canonical/protected repos → out of scope). No protected data touched; protected-DB hashes/mtimes unchanged
before and after.

Remaining steps (all outside this task's completed scope):
- **Human-gated merge of PR #8** (`docs/filesystem-cleanup-record` → `develop`) — do NOT auto-merge.
- After PR #8 merges: remove the `noam-coach.wt-fscleanup-doc` worktree.
- Delete the empty `noam-coach.wt-lease-fix` directory once its OS lock clears.
- Product backlog stays gated: LOG-014 → LOG-015/Task 66 → LOG-016 → LOG-012 remain **PROPOSED and unstarted**.

## Authorized product queue + owner decisions (standing autonomy)

Under the standing Work Manager autonomy directive (2026-07-25), the queue
`LOG-014 → LOG-015/Task 66 → LOG-016 → LOG-012` is **AUTHORIZED for autonomous
execution** (planning → implementation → review/correction loops → CI → merge →
cleanup). Owner Hebrew interview resolved these durable Category-C product
decisions (apply in each task's implementation + acceptance tests):

- **LOG-014 — ambiguous identity correction:** when a free-text correction names a
  food absent from the current items (or targets an unclear/low-confidence item),
  **route automatically to AI reanalysis with identity enforcement** — do NOT ask a
  clarification question, and do NOT apply a deterministic scale/quantity change.
  Scale/quantity must also be **idempotent** (dedup against `locked_corrections`).
- **LOG-015 / Task 66 — interrupted mandatory safety question:** **block-and-auto-
  restore.** (1) Enforce the safety gate (`pending_safety_questions` /
  `check_plan_readiness`) on **every** plan-build/activation path, not only the
  assistant path; AND (2) **automatically restore** the suspended
  `training_limitations` question immediately when the health-import microflow +
  confirm wizard completes (deterministic parent-resume by flow identity).
- **LOG-012 — medication name in `audit`:** **code to a category** (`kind_code`),
  do NOT store the raw medication free-text (and do not keep it even redacted);
  free-text `explanation`/`correction_text`/daily-flag `text` dropped in favor of
  bounded structured fields; `write_audit` also routed through `redact` as a backstop.
- **LOG-016 — legacy `goals`:** freeze (drop the two writers + repoint the DSAR
  export to `goal_versions`); **no schema drop, no remove-on-write** (brief-settled).

Engineering (Category-B) decisions resolved autonomously and recorded per task
spec at implementation time (e.g. reuse of existing confidence thresholds, the
resume-unwind mechanism, the audit allowlist shape, LOG-012-before-LOG-016
serialization on `core.py`/`export_user_data.py`).

## Log-audit intake (PROPOSED — superseded by the authorized queue above for LOG-014/015/016/012)

Recorded 2026-07-25 by the read-only log-audit + 4-agent intake turn. All items
revalidated at HEAD `ad1da97` against live code + read-only DB. **State = PROPOSED.**
None is APPROVED or IN_PROGRESS. **All product implementation is GATED** behind
the repository-cleanup sequence (PR #7 merged → PR #6 sync+green → Batch C → PR #6
merge → develop sync). Do NOT start any of these before those gates clear. No real
user IDs / medical text / prompts / image ids / private paths are stored here.

- **PROPOSED-LOG-014** — P1 nutrition correctness, High, confirmed defect. Deterministic
  meal-correction parser can misroute an item-**identity** correction to a **quantity/scale**
  op; scale is **non-idempotent** so repeats compound (grams/kcal halve each time); original
  unclear name preserved. "False saved" element **REFUTED at HEAD** (card is pending/must-approve;
  persisted == approved preview). Root cause: `meal_intelligence.py` scale branch (bare `חצי`→0.5,
  ~lines 720/845/1200) shadows the identity/reanalysis path (`meal_text.py` ~428-442); no dedup vs
  `locked_corrections`. Mapping: closest live task = **Task 58** but this is a NEW facet → new task
  OR Task 58 §K; cross-ref **B-2** (OPEN coverage-registry row `SOURCE_COVERAGE_REGISTRY.md`, not a task).
- **PROPOSED-LOG-015 / Task 66** — P2 medical safety, High, confirmed safety defect. Safety-critical
  onboarding question (`training_limitations`) suspended by the health-import microflow + confirm
  wizard is **not auto-restored** (only a later manual "complete my plan" returns to it); AND the
  safety gate (`pending_safety_questions`/`check_plan_readiness`) is enforced at **only one**
  plan-build entry (`assistant.py` ~435) — deferred-plan continuation (`onboarding.py` ~686-692 →
  `build_weekly_plan`) and callback activation (`callback_plans.py` ~1663) can build past an
  unresolved medical field. Root cause: resume guard `== health_import` already false at the
  `finally` (`health_jobs.py` ~1823); wizard exit never resumes parent (~1499-1538); single-level
  `resume_suspended` vs 2-deep nesting (`conversation.py` ~326-342). Not covered by Tasks 63/64 →
  **new Task 66** covering BOTH restoration + universal plan-build safety gating.
- **PROPOSED-LOG-016** — P3 canonical data, Medium latent trap. Legacy `goals` disagrees with
  `goal_versions` for `active_provisional` users; `goals` seeded default (`core.py` ~296-311,
  ON CONFLICT DO NOTHING), mirrored only on full `activate_goal` (`planning.py` ~276-285).
  **Zero product readers of `goals`** — only migration-5 seed + DSAR export, so a stale wrong number
  leaks into the user's data export. Fix by **derivation** (drop the two legacy writers; repoint
  `scripts/export_user_data.py` to `goal_versions`); **NO schema drop** until cleanup lands.
- **PROPOSED-LOG-012** — P4 privacy (real near-term). `write_audit` (`core.py` ~253-273) is a direct
  INSERT that **bypasses the canonical `emit`/`redact`/observability-mode boundary**; stores raw
  user_id + free-text medical/goal/correction content; exported in the DSAR ZIP. Observability-mode
  changes cannot protect it. Fix: allowlisted structured `audit` schema (codes/ids/bounded metadata
  over free text; drop `explanation`; medical→`constraint_id`+`kind_code`), route through `redact` as
  a backstop, pseudonymous user ref. Preserve safety-investigation evidence + `checkins.py` consumer + TTL.

Deferred (do NOT schedule above correctness/safety): LOG-002 (default content mode), LOG-003 (unused
spans), LOG-004 (raw id in flow_id), LOG-005 (file logging), LOG-006 (miniapp HTTP obs),
LOG-013 (analytics dup), LOG-009 (AI latency baseline). No action: LOG-001 already REMEDIATED
(F-A9 closed; `meal_trace.py`); LOG-007/008/010/011 EXPECTED/working (LOG-011 clears the earlier
"daily-menu idempotency unverified" note). Full specs + regression matrices: intake report + the
scratchpad draft `WORK_MANAGER_STATE_INTAKE_DRAFT.md`.

## Complete work-plan inventory + progress (reconciled 2026-07-25, git-verified)

Reconciliation of the FULL canonical work plan against git reality (not document
claims). Verified by `git log` / merge-base / test-file presence.

**Verified-and-merged (in `develop @ d3da771`):** Tasks 1–22; Tasks 58–65 (the
MASTER_TASKS "Open Backlog 58–65" label is **STALE** — all merged per
CANONICAL_IMPLEMENTATION_LEDGER A.2; Task 61's "PARTIAL/U-3" label is stale, it
is merged at `a4eec07`); FIX 38–57 (B-series, 20 modules/tests); ARCH 1–13 except
ARCH-07B; Observability O1–O10 + Review R1–R6; DayPlan P1.1/P1.1b (PR #4/#5).

**Current LOG batch (this session) — implemented, independently verified, and
MERGED to develop (status: verified-complete-merged). PRs #9–#13 all merged with
both CI contexts green (LOG-015's earlier push-context stray-db flake did NOT
recur on the PR run):**

| Task | PR | Merge commit | Branch CI |
|---|---|---|---|
| Governance (autonomy + owner decisions) | #13 | `6d2e841` | both green |
| LOG-012 (audit allowlist/redaction; medication→kind_code) | #11 | `c317a4b` | both green |
| LOG-016 (freeze legacy goals; DSAR→goal_versions) | #12 | `c517dbe` | both green |
| LOG-014 (meal-correction routing/idempotency) | #9 | `b38d467` | both green |
| LOG-015 / Task 66 (safety-question resume + universal gate) | #10 | `25101d0` | both green (flake cleared on rerun) |

Inventory: PR #14 (this doc) — final merge. develop after the 5 merges: `25101d0`.

**Original per-branch verification record (pre-merge):**
| Task | Branch | Commit | Local verify | Branch CI |
|---|---|---|---|---|
| LOG-014 (P1 meal-correction routing/idempotency) | `fix/log-014-meal-correction-routing` | `100cf71` | 177+~470+447 green | push **success** |
| LOG-015 / Task 66 (P2 safety-question resume + universal gate) | `fix/log-015-safety-question-resume` | `c803d8a` | full split + CI-exact 951 green | push red = **stray-db flake** (rerun clears) |
| LOG-012 (P4 audit allowlist/redaction; medication→kind_code) | `fix/log-012-audit-redaction` | `3466be9` | 23+~380 + full split green | (running) |
| LOG-016 (P3 freeze legacy goals; DSAR→goal_versions) | `fix/log-016-goals-canonical` | `066e81e` | 42+~135 green | (running) |
| Governance (autonomy model + owner decisions) | `chore/work-manager-autonomy-config` | `9971e67` | docs-only | push **success** |

Owner Category-C decisions (resolved, Hebrew interview): LOG-014 auto-reanalysis
on ambiguous/foreign-token identity corrections (no clarification), idempotent
scale; LOG-015 block-and-auto-restore (gate every plan-build path + auto-restore
after wizard); LOG-012 medication→`kind_code` (no raw free-text); LOG-016 freeze
(no schema drop).

**Genuinely-remaining APPROVED tasks beyond the LOG batch: NONE.** Everything
else is complete-merged, BLOCKED scope, exploratory-direction-only, or superseded.

**BLOCKED / owner-decision (NOT in the autonomous queue):** AI Gateway (P1.2);
stored weekly-plan regen / `planning._meal_slots` Phase 2 (U-2); destructive
sleep-fact migration; multi-user support; live Apple Health integration;
external-resource items (Telegram token, live OpenAI, Google Calendar OAuth,
Sentry, prod infra, PostgreSQL/queue/object-store, live E2E Telegram). P2.1–P2.8
consolidation-audit backlog is revalidated-with-evidence but **not scheduled** —
requires owner approval to enter the queue (P2.4/P2.5 Mini-App governance are the
most material, both High).

**Superseded / exploratory — DO NOT implement:** FIX 1–37 (→ Tasks 1–22 + 58–65);
B-2 (→ incorporated in LOG-014); archived requirements (MASTER_TASKS:104-116, "do
not reopen"); P1.3–P1.11 forward program ("target direction only, no production
code" — a doc line ≠ approved task); addendum "batches A–I" (audit recommendation
only); the deleted root PII audit `.md` plans (FS-CLEANUP-1).

### Corrected progress (explicit denominators)
- **Current LOG batch:** 4/4 implemented + independently verified + **MERGED**
  (PRs #9/#10/#11/#12 + governance #13). Inventory PR #14 finalizing.
- **Complete approved work plan:** the entire *approved* scope = already-merged
  historical tasks + this LOG batch — now **all merged**. **Remaining
  approved-and-unstarted beyond this batch: 0.**
- **Blocked by owner decision / external access:** the BLOCKED list above (AI
  Gateway, Phase-2, sleep migration, multi-user, live Apple Health, P2.x
  scheduling, external-credential items) — not counted as "remaining approved".
- **Worktree cleanup:** performed after the inventory merge — the 6 merged task
  worktrees (`wt-log012/014/015/016`, `wt-config`, `wt-inventory`) are removed via
  `git worktree remove` + `prune` once each is proven to hold no unique/uncommitted
  work; `wt-lease-fix` (empty, OS-locked) retained. Final accounting in the
  session report.

## Execution queue — WAVE-1 (in progress) and WAVE-2 (owner-approved, queued)

### WAVE-1 — correctness + privacy + UX (owner-approved, executing)
| Task | Branch | PR | State |
|---|---|---|---|
| TASK-R1 — recursive fail-closed DSAR redaction of `analytics_events` | `fix/wave1-dsar-analytics-redaction` | #15 | **MERGED** (`0cfb062`) |
| TASK-B1 — fit the main session to `session_minutes` | `fix/wave1-planning-duration-fitting` | #16 | **MERGED** (`2674c98`) |
| TASK-LOG004 — fully opaque `flow_id` (no user-derived component) | `fix/wave1-opaque-flow-id` | #18 | **MERGED** (`95119bf`) |
| TASK-UX01 — duplicate-correction notice (idempotent skip) | `fix/wave1-duplicate-correction-notice` | #19 | **MERGED** (`a5bfc01`) |

**WAVE-1 is COMPLETE.** All four tasks merged with both CI contexts green on each
PR; every merge commit is contained in `develop`. Cumulative full-regression gate
on the combined state: **both halves EXIT 0, zero failures** (~2,487 tests).
Wave-only worktrees/branches removed after proving each held no unique or
uncommitted work.

**TASK-CI-DAILY-MENU-CONCURRENCY** (owner-authorized during closeout; PR #21,
merged as `78e9add`) — deterministic hardening of the three daily-menu refresh
concurrency tests. The closeout PR's CI failed **deterministically** (two runs)
on `test_concurrent_refresh_creates_one_delivery_attempt` with
`len(deliver.calls) == 2`, on a docs-only diff, while the push context and
`develop`'s own CI were green.

Root cause was in the TEST, not production: the three siblings started both
refreshes behind one `asyncio.Event`, which synchronizes only the task START. Under
slow scheduling the first refresh could complete claim → generation → persistence
→ delivery before the second reached its claim; the second then legitimately saw a
NEW menu identity and refreshed again — which **DECISION-R requires** for a later
deliberate refresh. A shared `_overlapping_refreshes` helper now blocks the winner
INSIDE its generator (generation runs strictly after `claim_operation`, so the
claim is held and the operation is provably incomplete), drives the challenger to
completion against that live claim, observes its suppression, and only then
releases the winner. No sleeps, timing assumptions or retry loops are used as the
proof of overlap. **Tests-only** — inspection found no production defect.
Verified: 300-iteration stress (100× each) zero failures, module 31, daily-menu
regression 127, ruff and compileall clean, both CI contexts green.

### WAVE-2 — Workout UX (owner-approved P1) — **COMPLETE**

| Task | PR | Merge commit | CI |
|---|---|---|---|
| TASK-WORKOUT-WEIGHT-TEXT | #22 | `198e6a5` | both contexts green |
| TASK-WORKOUT-REST-NEXT-ACTION | #23 | `dc300cc` | both contexts green |

Serialized as required: REST-NEXT-ACTION was branched from the **merged**
weight-text result, so the rest flow transitions into free-text weight entry.
Neither task changed the schema; `db.py` is absent from both diffs.

WEIGHT-TEXT: the weight-selection buttons are replaced by a deterministic
free-text parser (bare number, `קג`/`ק״ג`, decimal point AND comma, per-hand,
bodyweight, same-as-previous — the last refused without a previous value), bounded
0–500 kg. Ambiguous input re-asks in place and writes nothing and advances
nothing; duplicate delivery cannot double-record (the live row is re-read and the
step guarded). The flow is armed only while the question is on screen via
`FlowName.workout_session`, so meal/onboarding/general text is never captured —
full-match anchoring is an independent second protection. Per-hand/bodyweight are
represented WITHOUT a migration: `sets.weight` stores the same canonical number
the buttons produced, the load-type hint rides in the schemaless `sessions.plan`
JSON, and the interpretation is stated in the prompt and confirmation. The
`splitw` split-set sub-flow was deliberately left button-driven (a second state
machine with its own duplicate semantics) — the one remaining inconsistency.

REST-NEXT-ACTION: one **pure** resolver (`noam_coach/services/workout_next_action.py`)
is consulted by both the rest card and the transition that actually happens at
zero, so the displayed instruction and the real next state cannot diverge. It
reads the canonical `sessions` row (never `rest_timers`' denormalised last-set
data, never the last message), so changes during rest surface on the next edit.
Review caught and fixed a real defect: because the pointer is advanced by whoever
saved the set, a rest before a DIFFERENT exercise was rendered with continuation
framing and carried the previous exercise's weight across; `from_exercise_index`
(from the already-persisted `rest_timers.exercise_index`) now distinguishes them,
with regression tests pinning it. The existing throttle/dedupe are untouched, so
there is no message flooding, and the three inline advance chains were
deliberately not refactored (their atomic optimistic UPDATE is a real concurrency
guard) — a test instead asserts the resolver agrees with them at every step.

### WAVE-2 — original queue entry (historical)
*(original requirement text, kept for traceability)*

Both requirements and their acceptance criteria as supplied by the owner are
**binding**; they are already approved and must not be re-confirmed.

**TASK-WORKOUT-WEIGHT-TEXT (P1)** — during an active workout, replace the
predefined weight-selection buttons with validated **free-text** weight entry.
Deterministic Hebrew/numeric parsing (no AI for simple numerics): `80`, `80 קג`,
`80 ק״ג`, `17.5`, `17,5`, `12 בכל יד`, `משקל גוף`, `אותו משקל`. Ambiguous input →
short Hebrew clarification, **no write and no state advance**. Duplicate Telegram
update must not double-record or double-advance. Free-text is consumed **only**
while an active workout explicitly awaits a weight (must not steal meal,
onboarding or general messages). Confirmation states the interpretation actually
persisted. Non-scope: plan generation, exercise selection, RIR/progression, rest
timers, alternatives, split-set behavior, DB migration, historical records,
unrelated navigation/safety buttons.

**TASK-WORKOUT-REST-NEXT-ACTION (P1)** — every visible rest timer must show the
actual **next action**: another set of the current exercise, the exact next
exercise, or the correct completion step — derived from the **canonical persisted
state**, never from the last rendered message. Must be correct for skips,
alternatives, split sets, resume-after-interruption and final rest; must survive
timer-message edits without flooding the chat; existing timer controls preserved.
Prefer **one shared next-action resolver** used by both the rest display and the
transition that actually occurs when rest ends.

**Sequencing (inspection already performed, read-only):** the two tasks
**OVERLAP and must be SERIALIZED** — hard collisions in the `callback_session.py`
handler block, in `workout.py::save_set` advance branches, and potentially in
`db.py`. Order: **WEIGHT-TEXT first**, then REST-NEXT-ACTION **rebased on the
merged result**, because the rest flow must transition correctly into free-text
weight entry. A short re-inspection runs at wave start. Separate tracked tasks,
separate PRs.

**Engineering decision recorded (no migration, no owner input required):**
per-hand (`12 בכל יד`) and bodyweight have no representable column today
(`sets.weight` is a bare `REAL`). Resolution: store the same canonical number the
buttons would have produced — preserving historical meaning exactly — carry the
load-type hint in the schemaless `sessions.plan` JSON plus existing equipment
metadata, and make the interpretation explicit in the prompt and confirmation
copy; bodyweight → `0`. No schema change, no historical-data modification.

## Follow-up register (recorded, NOT pulled into any active wave)

| ID | Observation | Evidence | Disposition |
|---|---|---|---|
| FU-01 | `session_N_missing_time` quality issue when `resolved_preferred_time` is omitted and no availability fact exists | surfaced while building TASK-B1 tests; verified **pre-existing** against a stashed baseline (`_workout_candidate` emits sessions with no time on that path) | **DEFERRED** — real but pre-existing; out of TASK-B1 scope. Candidate for a future small planner-quality task. Not scheduled. |
| FU-02 | Empty equipment facts can leave a session with zero exercises (equipment adaptation strips to bodyweight) | surfaced while building TASK-B1 tests; **pre-existing** in `adapt_exercises`, not in the new fitting code | **DEFERRED** — pre-existing adaptation behavior, outside TASK-B1 scope. Not scheduled. |
| FU-03 | Remaining `DIRECT_TABLES` in the DSAR export (e.g. `conversation_state`, `user_facts`, `medical_constraints`) may carry comparable free-text exposure; only `audit` (LOG-012) and `analytics_events` (TASK-R1) are redacted today | `scripts/export_user_data.py` generic `SELECT *` loop | **DEFERRED** — follow-up DSAR audit candidate. Deliberately not expanded into WAVE-1 (scope discipline). Not scheduled. |
| FU-04 | `test_plan_completion_flow.py::test_complete_missing_callback_starts_continuous_completion_flow` fails with a `TypeError` in `noam_coach/observability/telegram_egress.py` **only inside a broad `-k` slice** (`conversation or flow or callback or resume or suspend or observability or grammar or trace`); it passes alone, as a whole file, and in CI | Reproduced on **clean `develop`** with the identical slice, i.e. **pre-existing test-ordering pollution**, not caused by TASK-LOG004 | **DEFERRED** — test-isolation hygiene, same family as the known stray-root-db ordering artifact. Does not affect CI (which passes) or product behavior. Not scheduled. |

These remain visible in the accounting and must not disappear silently; none is
authorized for implementation without an explicit owner decision.

## WAVE-0 — live-session correctness defects (2026-07-27)

Driven by 1,729 `product_events` from two live sessions on 2026-07-26 plus 21
owner comments. Five lanes were run in parallel against a **function-level**
conflict matrix produced by four read-only agents before any code was written;
all five merged into an integration branch with **zero conflicts**, which is
the evidence the matrix was correct.

| PR | Defect | Evidence |
|---|---|---|
| #25 | `##`-prefixed messages persisted as user data — one comment reached `diet_restrictions` and was echoed back as a declared dietary preference; another was stored as a meal | live session; profile screen |
| #26 | **Intent classification had never worked.** `Intent.slots` was `dict[str, Any]`, which is unrepresentable in strict mode, so every request was rejected before generating a token (9/9) while every other AI purpose succeeded | `ai.call.failed`, `BadRequestError` |
| #27 | The typed weight was discarded: the router seeded from `recommend_load` and `setok` wrote the plan default, while `show_session` displayed the typed value — **the card and the database disagreed**. Separately, `0.0` kg persisted on a barbell press | 3 typed weights, 0 stored correctly |
| #28 | Skip emitted no event at all; pain reached `medical_constraints`/`audit` but no domain event | 20:42:33, 20:41:49 |
| #29 | Proactive messages permanently blocked (`status='active'` never matched `active_provisional`); `product_events` had no retention; AI errors recorded no message; the Mini App button could never work | 0/8 sent; ~1,150 rows/hour |

### Verification that the plan itself was wrong (recorded so it is not repeated)

Nine items in the pre-implementation plan were **disproven** by the agents and
deliberately NOT implemented:

| Item | Claim | Reality |
|---|---|---|
| Israel timezone | Scheduler runs in UTC | **There is no APScheduler in this repo.** Jobs use PTB's `JobQueue`; every `run_daily` already passes `tzinfo=TZ` |
| DB backup | No backup exists | `scripts/backup.py` + `restore.py` + compose volume + documented cron all exist; only in-process scheduling is absent |
| AI schema guard | 6 models at risk | All six already convert to strict schemas; the new test is a guard, not a fix |
| `loadwhy` | Explains nothing | Fully wired; an empty explanation is a data problem in the `planned_load` path |
| `pain_location` | Stays `NULL` = lost write | `NULL` is the **success** state — a transactional claim token. Now pinned by a test |
| Rest-timer persistence | Not implemented | Already implemented, including startup restore |
| AI progress indicator | Missing | Exists at `meals.py:234` |
| Session-card limitation | Never surfaced | `_exercise_pain_warning_line` already renders it per exercise |
| Cosmetics lane | Parallelizable | **False.** No central strings module; every Hebrew literal is inline in the handler that owns the logic, so cosmetics collide with every logic lane |

The proactive-message failure also had a **second, non-code cause**: morning
(08:00), evening (22:00) and weekly (Sat 20:30) never fired because the bot
was not running at those times — it started at 22:14, eight minutes after the
evening slot. `run_daily` has no catch-up-on-startup behaviour. Making it
catch up is a product decision, not a bug fix, and is not scheduled.

## WAVE-1 — second live session (2026-07-27), audit + plan

Driven by 779 `product_events` from the 05:00–07:15 UTC session on a freshly
reset database (full onboarding from zero), plus `logs/session_20260727_075856.log`.
**Seven** read-only agents across disjoint domains; every critical finding was
re-verified independently before being recorded.

**The full plan is `docs/WAVE1_WORK_PLAN.md` — 43 items in five bands.** It is
the single source for this wave; do not maintain a copy outside the repository.

### WAVE-0 held in production
| Fix | Evidence this session |
|---|---|
| `Intent.slots` (#26) | 10/10 intent classifications succeeded (was 0/9); zero `ai.call.failed` all session |
| Typed weight (#27) | Typed 50 while the card offered 54 → `sets.weight = 50.0` |
| Mini App button (#29) | 93/93 deliveries succeeded; zero BadRequest |

### P0 fixed this session
| ID | Defect | Commit |
|---|---|---|
| P0-1 | `menu:goals` was emitted by the top-priority CTA and handled nowhere — 4 silent taps, and no goal could ever be created. Unhandled callbacks now answer the user and emit an event | `95a82f2` |
| P0-2 | The dev-note guard required `##` while every real note used `#...#` — 5 notes lost, one stored as a confirmed nutrition fact | `5d0e60e` |
| P0-3 | The weekly plan prescribed squats and leg press to a user with an active knee constraint, with no warning, while attaching the elbow caveat to a back exercise | `f8a5982` |
| P0-4 | `allergies="none"` was written `confirmed=1` from an unanswered question, while nuts sat unclassified | `e2b0c1a` |

### The three findings that most change priorities
1. **Two of three dietary restrictions are unenforceable.** Matching resolves
   only through an 85-entry alias table, so a restriction stored as raw Hebrew
   cannot match its own name. Verified: a tortilla wrap and a baked eggplant
   both return zero violations against the user's own stored restrictions.
   Compounding it, `avoidance` maps to `warn` and menu validation acts only on
   `block`, so no `diet_restrictions` entry can ever hard-block a menu.
2. **The calorie target is 130 kcal/day too low.** `routine_profile` says 0.2
   workouts/week and `user_facts` says 4.0; `goals.py:297` reads the former.
   Measured: 2290 vs 2420 kcal. The sync is one-way, so it re-diverges nightly.
3. **The observability layer cannot reconstruct causality.** `parent_span_id` is
   NULL on all 779 rows, no `interaction.*` terminal event exists, and the
   application wrote 2 log lines in 2h15m (`callback_session.py`, `workout.py`
   and `assistant.py` contain zero logging calls). This is *why* a dead button
   survived: `routing.decided` is written before dispatch and nothing records
   the outcome.

## Links
- **WAVE-1 work plan (current): `docs/WAVE1_WORK_PLAN.md`**
- Canonical implementation ledger: `docs/CANONICAL_IMPLEMENTATION_LEDGER.md`
- Cleanup ledger: `docs/REPOSITORY_CLEANUP_LEDGER.md`
- Agent roster & contracts (durable): `docs/WORK_MANAGER_AGENTS.md`
- PR history: #4/#5/#6/#7/#8 (earlier tracks), #9–#14 (LOG batch), #15–#21 (WAVE-1 + CI hardening), #22–#23 (WAVE-2) — all merged. No open PRs; no task branches outstanding. Superseded line:
  `fix/log-012/014/015/016-*`, `chore/work-manager-autonomy-config`.
