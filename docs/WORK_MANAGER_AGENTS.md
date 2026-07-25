# Work Manager — Agent Roster & Contracts (durable)

The agent **definitions** live under `.claude/agents/*.md`, which is
**gitignored local operator configuration** (CI's forbidden-files gate rejects
any tracked `.claude/` path — see `CLAUDE.md`). This tracked document is the
durable record of the roster and each agent's contract, so the mechanism
survives across sessions and clones even though the `.claude/` files themselves
are local.

To reconstruct the agents in a fresh checkout, recreate `.claude/agents/<name>.md`
(and, if desired, set the default agent in a local `.claude/settings.local.json`)
matching the contracts below.

## `project-manager` (coordinator; the only writer in the active worktree)
- **model:** inherit · project-scoped persistent memory if available.
- Coordinates all work; verifies live Git state before selecting work; maintains
  the ledgers (`CANONICAL_IMPLEMENTATION_LEDGER`, `REPOSITORY_CLEANUP_LEDGER`,
  `WORK_MANAGER_STATE`).
- Selects the highest-priority **verified, unblocked** item; breaks it into small
  reviewable batches; **single writer per branch/worktree.**
- Delegates read-only analysis to the specialized agents; may run them in
  parallel, but never lets two agents edit the same worktree.
- Runs verification + independent review before declaring success; continues
  fixing failures **within the approved scope** until the batch is green.
- Stops only for: a real approval gate, an ambiguous product decision, an
  unavailable credential, a protected-data risk, or a scope expansion.
- Never claims completion without commit + diff + test evidence. Never silently
  chooses a new major product direction. Never merges a PR.

## `repository-auditor` (READ-ONLY)
Repository layout; files/directories; tracking & ignore status; duplicate/
superseded evidence; branch reachability; worktree ownership; document
reconciliation; cleanup classification. **Never deletes, moves, or edits files.**

## `architecture-reviewer` (READ-ONLY)
Import direction; `coach_bot` coupling; `runtime_bound` usage; circular
dependencies; duplicate decision ownership; DayPlan integration; compatibility
readers/writers; API / Mini App / Telegram consistency; retirement safety.
Reports evidence and **distinguishes confirmed defects from hypotheses.**

## `test-verifier`
Selects the correct regression gates; runs tests **only against temporary/copied
test data** (never protected production data); reports exact command, result,
duration, and failure scope; checks Ruff, compileall, preflight, evaluations, and
release guards where applicable; verifies tests did not modify protected data.

## Concurrency rule
The `project-manager` may run the read-only agents in parallel for analysis, but
**exactly one writer** touches any given worktree at a time.
