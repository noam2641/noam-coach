# Work Manager — Operational State

Compact operational checkpoint (NOT a historical report). Update whenever the
phase changes and before every pause.

| Field | Value |
|---|---|
| Last updated | 2026-07-25 (PR #7 merged; PR #6 synced to develop; log-audit intake recorded) |
| Last verified `origin/develop` | `3ec5296c5d4e9af67f2605ad2258e9b516087910` (merge of PR #7 — deterministic lease-loss test fix) |
| GitHub default branch | `develop` (verified — the old `codex/*` default is corrected) |
| Active phase | PHASE 4 — **Batch A + B DONE; PR #6 synced to develop; Batch C pending CI verification on the synced head.** The lease/CAS flake was root-caused as **test-only** wall-clock nondeterminism and fixed deterministically on `fix/deterministic-lease-loss-test` (`4697298`, +64/−5, test-only; 150/150 local stress). **PR #7 merged** into develop (`3ec5296`). PR #6 branch merged origin/develop via a normal no-ff merge (conflict-free; only `tests/test_daily_menu_refresh.py` came in). Batch C runs only after both CI contexts are green on the new PR #6 head. |
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

## Log-audit intake (PROPOSED — not approved, not started)

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

## Links
- Canonical implementation ledger: `docs/CANONICAL_IMPLEMENTATION_LEDGER.md`
- Cleanup ledger: `docs/REPOSITORY_CLEANUP_LEDGER.md`
- Agent roster & contracts (durable): `docs/WORK_MANAGER_AGENTS.md`
- Active PRs: PR #4 (merged), PR #5 (merged), PR #7 (merged), **PR #6 (open — this audit)**.
