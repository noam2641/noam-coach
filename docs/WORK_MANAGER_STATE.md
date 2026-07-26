# Work Manager — Operational State

Compact operational checkpoint (NOT a historical report). Update whenever the
phase changes and before every pause.

| Field | Value |
|---|---|
| Last updated | 2026-07-26 (WAVE-1 complete + CI hardening #21; develop `78e9add`) |
| Last verified `origin/develop` | `78e9add` (merge of PR #21 — TASK-CI-DAILY-MENU-CONCURRENCY) |
| GitHub default branch | `develop` (verified — the old `codex/*` default is corrected) |
| Active phase | **PHASE 7 — WAVE-1 CLOSEOUT.** All four WAVE-1 tasks merged: TASK-R1 #15 (`0cfb062`), TASK-B1 #16 (`2674c98`), TASK-LOG004 #18 (`95119bf`), TASK-UX01 #19 (`a5bfc01`); queue/follow-up doc #17 (`91eeac1`). Cumulative full regression run on the combined state. **Next: WAVE-2 (owner-approved workout UX), serialized — TASK-WORKOUT-WEIGHT-TEXT then TASK-WORKOUT-REST-NEXT-ACTION.** |
| Active task | WAVE-1 closeout (regression + traceability + wave-only worktree cleanup), then WAVE-2 start. |
| Approved work outstanding | **YES — WAVE-2 is owner-approved and queued** (see the execution-queue section). This supersedes the earlier "no remaining approved tasks" statement, which was true only of the LOG batch on 2026-07-25 and is **no longer current**. |
| *(rows below are HISTORICAL)* | *The following rows record the completed 2026-07-21…25 repository-cleanup and LOG-batch work. They are kept for traceability and are **not** current operational state — the current state is the four rows above.* |
| Baseline @ approval | head `1dbd6c6`; push CI `30154848488` success; PR CI `30154850042` success; PR #6 open, `+591/−0` |
| Batch A result | **PII in all six docs** (real user id + meal-image refs) → external backup only at `…\cleanup_20260725\local_docs_PII\`; **NOT added to Git** (A2 skipped). No tracked change. |
| Batch A CI | `b6db987`: push run `30155534787` success; pull_request run `30155536081` **success on re-run attempt 2** (attempt 1 flaked on `test_daily_menu_refresh::test_lease_loss_during_generation_fences_persistence`). Flake-record commit `52d6a10` CI: push `30156817298` success + pull_request `30156817976` success. **The deferred CI-hardening is now DONE** — PR #7 / commit `4697298` (event-driven deterministic synchronization; 150/150 local stress), merged to develop as `3ec5296`. |
| Batch B result | **DONE @ `69a4450`** (both CI green first; `52d6a10` was the preceding Batch-A flake-record doc commit, NOT Batch B). Fail-closed removal of the 3 verified strays after creating+hash-verifying their backups: `C:\coach_bot\noam_coach.db` (0-byte), `C:\coach_bot\.git\` (only `info\exclude`, sha `584f2cca…06ef`), `C:\coach_bot\.agents\` (empty). All outside the repo → **no git diff**. Backups at `…\cleanup_20260725\{stray_root_db,stray_root_git}\`. Canonical repo, protected worktree/DB, PII, runtime data untouched. |
| Active branch | `chore/repository-consolidation-audit` (head advances with each audit-correction commit; latest pushed head recorded in the CI table below) |
| Audit PR | **#6** (open, not merged) → base `develop` @ `3ec5296`; head `chore/repository-consolidation-audit` (synced to develop) |
| CI on PR #6 head (pre-sync `ad1da97`) | pull_request run `30158084198` **success**; push run `30158082421` **startup_failure** — verified **0 jobs ran** (workflow-START failure, NOT a pytest failure). Superseded by the post-sync head; re-verify both contexts on the new head. |
| PR statistics | Counts per PR #6 checks at the live synced head (earlier `+445/−0 @ e9ebeac` is stale). |
| Active worktree | `C:\coach_bot\noam-coach` (single writer) |
| Protected worktree | `C:\coach_bot\noam_coach_complete_release` @ `6d57c04` — do not touch |
| Protected/PII data | `noam_coach_complete_release\noam_coach.db` (sha `5bd8ac1b…`); `C:\coach_bot\noam-coach-private-audit\` (session trace + meal images) |

## Current objective
Establish the persistent Work Manager and produce the non-destructive repository
consolidation audit + architecture-finding revalidation, ending in a
Ready-for-review PR against `develop`. **No deletions/moves/retirement** until
the human approves with `APPROVE REPOSITORY CLEANUP EXECUTION`.

## Completed batches
- **P1.1 DayPlan Phase 1** — merged (PR #4, `08f953a`).
- **P1.1b DayPlan residuals & data-contract closure** — merged (PR #5, `fbff6f1`).
  Sleep-schema shared accessor + reader-first migration + canonical-only writers;
  Today's Menu count unified with Status/DayPlan; architecture guard; legacy-
  caller reclassification.

## Active task
Work Manager bootstrap (PHASE 1) + consolidation audit (PHASE 2) +
architecture-finding revalidation (PHASE 3).

## Blocking dependencies / pending human approvals
- **`APPROVE REPOSITORY CLEANUP EXECUTION`** required before any deletion/move/
  archive/branch-removal/worktree-removal/source-retirement (PHASE 4).
- GitHub PR creation from this environment needs the operator (no `gh`/token
  here) — a prefilled URL is provided at each PR step.

## Last test evidence
P1.1b head `4f9c664`: local full suite **2428 passed, 0 failed, 1 skipped**;
CI (push + pull_request contexts) **green**. Verified in the PR #5 pre-merge review.

## Next exact action
Audit is complete and PR #6 is open with the corrected 14-part output + the
three-batch (A/B/C) execution plan in `REPOSITORY_CLEANUP_LEDGER.md` §G.
**PAUSED — awaiting the human token `APPROVE REPOSITORY CLEANUP EXECUTION`.**
On approval, execute Batch A (backups + archival copies) → verify → Batch B
(remove verified strays) → verify → Batch C (origin/HEAD + prune 0-unique merged
branches). Backup destination: `C:\coach_bot_BACKUP_20260721_150908\cleanup_20260725\`.
No source retirement (P2.7/P2.8 stay future work).

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

### WAVE-2 — Workout UX (owner-approved P1; queued, starts after WAVE-1 closeout)

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

## Links
- Canonical implementation ledger: `docs/CANONICAL_IMPLEMENTATION_LEDGER.md`
- Cleanup ledger: `docs/REPOSITORY_CLEANUP_LEDGER.md`
- Agent roster & contracts (durable): `docs/WORK_MANAGER_AGENTS.md`
- Active PRs: PR #4/#5/#7 (merged), PR #6/#8 (merged). Open task branches await PRs:
  `fix/log-012/014/015/016-*`, `chore/work-manager-autonomy-config`.
