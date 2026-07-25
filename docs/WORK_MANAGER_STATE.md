# Work Manager — Operational State

Compact operational checkpoint (NOT a historical report). Update whenever the
phase changes and before every pause.

| Field | Value |
|---|---|
| Last updated | 2026-07-25 (audit-gaps corrected) |
| Last verified `origin/develop` | `fbff6f106e4b923bcbd016d652c2c121e55ecec0` (merge of PR #5) |
| GitHub default branch | `develop` (verified — the old `codex/*` default is corrected) |
| Active phase | PHASE 4 execution — **Batch A DONE (external-backup-only; PII guard tripped)**; Batch B/C pending |
| Baseline @ approval | head `1dbd6c6`; push CI `30154848488` success; PR CI `30154850042` success; PR #6 open, `+591/−0` |
| Batch A result | **PII in all six docs** (real user id + meal-image refs) → external backup only at `…\cleanup_20260725\local_docs_PII\`; **NOT added to Git** (A2 skipped). No tracked change. |
| Active branch | `chore/repository-consolidation-audit` (head advances with each audit-correction commit; latest pushed head recorded in the CI table below) |
| Audit PR | **#6** (open, not merged) → base `develop` @ `fbff6f1`; head `chore/repository-consolidation-audit` |
| CI on head `e9ebeac` | push run `30154114434` **success**; pull_request run `30154115680` **success** (this correction commit re-runs both; see PR checks) |
| PR statistics | **+445 / −0** at `e9ebeac` (this correction commit adds doc lines; final counts per PR #6 after CI) |
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

## Links
- Canonical implementation ledger: `docs/CANONICAL_IMPLEMENTATION_LEDGER.md`
- Cleanup ledger: `docs/REPOSITORY_CLEANUP_LEDGER.md`
- Agent roster & contracts (durable): `docs/WORK_MANAGER_AGENTS.md`
- Active PRs: PR #4 (merged), PR #5 (merged), **PR #6 (open — this audit)**.
