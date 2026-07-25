# Work Manager — Operational State

Compact operational checkpoint (NOT a historical report). Update whenever the
phase changes and before every pause.

| Field | Value |
|---|---|
| Last updated | 2026-07-25 |
| Last verified `origin/develop` | `fbff6f106e4b923bcbd016d652c2c121e55ecec0` (merge of PR #5) |
| GitHub default branch | `develop` (verified — the old `codex/*` default is corrected) |
| Active phase | PHASE 2 — repository & filesystem consolidation audit (read-only) |
| Active branch | `chore/repository-consolidation-audit` (off `develop` @ `fbff6f1`) |
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
PHASE 2: enumerate & classify every repo/worktree/local artifact into
`docs/REPOSITORY_CLEANUP_LEDGER.md` (read-only), then PHASE 3 revalidation, then
produce the 14-part audit output and open the PR.

## Prohibited scopes (this session)
AI Gateway · stored weekly-plan regeneration (`planning._meal_slots` Phase 2) ·
destructive sleep-fact migration · any protected-data mutation · any deletion
before approval.

## Links
- Canonical implementation ledger: `docs/CANONICAL_IMPLEMENTATION_LEDGER.md`
- Cleanup ledger: `docs/REPOSITORY_CLEANUP_LEDGER.md`
- Agent roster & contracts (durable): `docs/WORK_MANAGER_AGENTS.md`
- Active PRs: PR #4 (merged), PR #5 (merged). Next: consolidation-audit PR (pending).
