# Work Manager — Operational State

Compact operational checkpoint (NOT a historical report). Update whenever the
phase changes and before every pause.

| Field | Value |
|---|---|
| Last updated | 2026-07-27 (WAVE-1 batch 2: PRs #44/#45/#46/#47 merged; develop `25f7c81`) |
| Last verified `origin/develop` | `25f7c81` — full suite exit 0, no stray DB, verified at `3d5dd18` with only docs changed since |
| GitHub default branch | `develop` |
| Active phase | **WAVE-1 (2026-07-27 audit wave) — in progress.** 15 PRs merged (#33–#47): four P0s, two docs, and eight WAVE-1 items. Zero open PRs, single worktree. |
| Active task | **Batch 1 COMPLETE** (A1, A2, A3 — PRs #59, #60, #61, merged with reachability proven). Batch 2 is A4, A5, A6, A11a in **two serial chains** — `A4 → A6` and `A5 → A11a` — which are parallel to each other. Their file surfaces overlap even though their dependencies do not; ownership and merge order are fixed in §3.6 of `docs/PERSONALIZED_WORKOUT_ARCHITECTURE_PLAN.md`. |
| Approved work outstanding | **Track A**: A4, A5, A6, A7, A8, A9, A10, A11a, A11b, A12 (A1–A3 done). **Track B** (WAVE-1 survivors): W1-11, W1-13, W1-14, W1-15, W1-16, W1-18, W1-20, W1-21, W1-22, W1-23. |
| Batch 1 evidence | A1 `0691da3` — pain mirror isolated from the committed safety write. A2 `09e06d9` — migration 16 adds nullable `sets.exercise_index`; undo rewinds to the performed occurrence. A3 `295bb9f`+`2bd99da` — effort CTA on the rest screen, offered only when RIR is unknown. Each verified to fail without its fix. |
| Corrected 2026-07-29 | This row previously named W1-7 as the active task and listed W1-7/W1-8 as outstanding. **Both are merged and live in code** — verified on `develop`. W1-17 (#42) and W1-19 (#40) are also done. W1-10 is split into A2+A8; W1-44 is absorbed into A9. |
| Active worktree | `C:\coach_bot\noam-coach` (integration) + one per parallel writer under `C:\coach_bot\wt-*` |
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
