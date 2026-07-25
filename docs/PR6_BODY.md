# PR #6 body — repository consolidation audit & Work Manager governance

> Paste this as the PR #6 description on GitHub (the body is currently empty; it
> cannot be set from this environment — no `gh`/token). This file is tracked so
> the content is durable.

## What this PR is
Establishes the persistent **Work Manager** governance and the **read-only
repository consolidation audit** for `noam-coach`. **Documentation only — no code
changes, and NO cleanup has been executed.** Cleanup runs only after the human
replies `APPROVE REPOSITORY CLEANUP EXECUTION`.

Base: `develop` · Head: `chore/repository-consolidation-audit`.

## Governance & ledger changes (tracked)
- **`CLAUDE.md`** — authoritative operating rules (develop is canonical; feature
  branches + PRs; never merge/force-push; protected-data & PII prohibitions;
  `.claude/` stays untracked per the CI forbidden-files gate; cleanup/retirement
  safety; blocked scopes: AI Gateway, weekly-plan regen, destructive sleep
  migration, multi-user, live Health).
- **`docs/WORK_MANAGER_STATE.md`** — compact operational checkpoint.
- **`docs/WORK_MANAGER_AGENTS.md`** — durable record of the agent roster
  (project-manager, repository-auditor, architecture-reviewer, test-verifier);
  the agent files live under gitignored `.claude/agents/`.
- **`docs/REPOSITORY_CLEANUP_LEDGER.md`** — full classification of every
  repo/worktree/local artifact + the executable A/B/C execution plan.
- **`docs/CANONICAL_IMPLEMENTATION_LEDGER.md`** — adds the **P2** consolidation-
  audit backlog (8 confirmed findings with file:line evidence + dependency order).

## Planned cleanup — three reversible batches (NOT executed)
Backup destination: `C:\coach_bot_BACKUP_20260721_150908\cleanup_20260725\`.
- **Batch A** (non-destructive): back up the six local-only audit docs. The
  extended PII scan found the real production user id + meal-image references in
  **all six**, so per the guard they were backed up **externally only and NOT
  added to Git** (the in-repo `docs/archive/` copy was skipped). Originals untouched.
- **Batch B**: remove ONLY the verified strays outside the repo — the 0-byte
  `noam_coach.db`, the malformed `.git\` (fail-closed: exact inventory +
  recorded sha256), the empty `.agents\`.
- **Batch C**: reassign stale local `origin/HEAD`→`develop`; prune ONLY branches
  proven merged with **0 unique commits** (`consolidation/unified-noam-coach`,
  `review/meal-observability-batch7`, `review/workout-selection-architecture`),
  via a guarded loop using `git branch -d`.

## Protected scopes (untouched)
Protected worktree `noam_coach_complete_release` @ `6d57c04` (+ its 2 worktrees);
historical DB (sha `5bd8ac1b…`); PII under `noam-coach-private-audit\`; runtime
`data/logs/storage`; `.conda`; `.claude/`.

## Exclusions (must NOT delete)
`feature/dayplan-phase1` & `feature/dayplan-residuals` (merged, intentionally
retained); the three `codex/*` + `audit/latest-manual-session-2026-07-18` branches
(**unique unmerged commits**; the audit branch has a unique doc `2cf3de5`).
**Source retirement (P2.7/P2.8 `coach_bot`/`runtime_bound` decoupling) is OUT of
scope** and remains future implementation work.

## Verification & rollback
Every batch in `REPOSITORY_CLEANUP_LEDGER.md §G` has exact commands, preconditions,
byte-equality/reachability verification, and rollback (including full branch
rollback SHAs `608f60d`/`f31f047`/`7d256fa`). After each executed batch: separate
logical commit on this PR, update ledgers + `WORK_MANAGER_STATE`, push, wait for CI.

## Status
**Cleanup has NOT executed.** This PR carries only governance + audit documentation.
Do not merge automatically.
