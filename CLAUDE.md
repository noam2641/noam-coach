# CLAUDE.md — Project operating rules for `noam-coach`

Authoritative, concise rules for any Claude Code session in this repository.
**Every session must begin by reading this file, the source-of-truth ledgers
below, and verifying live Git state before selecting or changing anything.**

## Sources of truth (read first, in this order)

1. `CLAUDE.md` (this file) — operating rules.
2. `docs/WORK_MANAGER_STATE.md` — the current operational checkpoint (phase,
   branch, active task, next action, approvals pending).
3. `docs/CANONICAL_IMPLEMENTATION_LEDGER.md` — the implementation source of truth
   (what is done/planned, verified against code + tests).
4. `docs/REPOSITORY_CLEANUP_LEDGER.md` — the cleanup/disposition source of truth.

**The repository and its executable behavior outrank any report, task document,
or commit message — including this prompt's own historical references.** Verify
before trusting.

## Branching & PRs

- `develop` is the **canonical integration branch** unless a ledger entry
  explicitly changes that decision. GitHub's default branch is `develop`.
- **Never implement directly on `develop`.** Use a feature/fix/chore branch and a
  PR into `develop`.
- **Never merge a PR automatically.** Merging is a human decision.
- **Never force-push or rewrite shared history.**
- A change is only "done" with **commit + diff + test evidence** — never claimed
  complete without it.

## Protected data & secrets — hard prohibitions

- **Never** modify, reset, clean, migrate, copy over, or run tests against
  protected production data. The protected worktree is
  `C:\coach_bot\noam_coach_complete_release` (+ its linked worktrees); its
  historical DB and the captured session under
  `C:\coach_bot\noam-coach-private-audit\` are off-limits.
- **Never commit** PII, production logs, meal images, Apple Health exports,
  tokens, secrets, `.env`, or historical databases.
- `.claude/` is **local operator configuration — never tracked.** The CI
  forbidden-files gate (`.github/workflows/ci.yml`) FAILS the build if any
  `.claude/`, `.env`, or `*.db` path is tracked. Keep agent definitions and
  local settings under `.claude/` (gitignored); put durable governance in
  tracked `docs/`.
- For sensitive files, work from metadata / tracking status / size / hashes —
  do not print or copy sensitive contents.

## Cleanup & retirement safety

- **Never remove a branch or worktree** without proven merge + reachability
  evidence (`git merge-base --is-ancestor`, `git branch --merged`).
- **Never retire source code** without runtime-caller analysis and regression
  coverage proving the canonical replacement behaves identically.
- Do not classify something removable merely because its name looks old.
- Cleanup executes only after an explicit human approval token
  (`APPROVE REPOSITORY CLEANUP EXECUTION`), in small reversible batches, never
  mixing document cleanup + protected-data handling + source retirement in one
  opaque batch.

## Scope guards (blocked until explicitly authorized)

- **AI Gateway** work is BLOCKED until explicitly authorized.
- **Stored weekly-plan regeneration / `planning._meal_slots` Phase-2 work** is a
  separate scope — do not pull it into an unrelated batch.
- **Multi-user support** and **live Apple Health integration** are product
  decisions, not automatic tasks.
- Historical legacy-shaped sleep facts must remain readable through the shared
  accessor (`coaching_day.sleep_bedtime/sleep_wake_time`); **no destructive data
  migration** is authorized.

## Session workflow

1. Read the four sources of truth above; run `git status` + verify branch/remote.
2. Pick the highest-priority **verified, unblocked** ledger item.
3. Break it into a small reviewable batch on a dedicated branch (single writer).
4. Implement → focused tests → full regression → independent review →
   documentation → commit → push → **green PR** (do not merge).
5. Update the relevant ledger(s) and `docs/WORK_MANAGER_STATE.md` before pausing.
6. Stop for: a real approval gate, an ambiguous product decision, an unavailable
   credential, a protected-data risk, or a scope expansion.

## Verification gates (this repo)

Full local suite runs in two halves (environment long-run limit):
`tests/` root, then `tests/regression tests/acceptance`. CI (Linux, Python 3.12)
runs: `compileall -q .`, `ruff check .`, `pytest --cov`, `run_evaluations.py`,
`build_release.py`, and the forbidden-files gate. A PR is not ready until **both**
the push- and pull-request-context CI runs are green.
