---
name: implement-review
description: Implement the explicitly approved findings of a completed session review — root-cause fixes with regression tests, full verification, one commit per finding on a pushed review/<review-id> branch, never merged. Args; the review id (e.g. 2026-07-20_1).
---

# /implement-review <review-id> — approved-findings implementation

You are running the implementation half of the continuous-improvement
workflow. Only explicitly approved findings may produce code changes;
the validator — not conversation — is the gate.

## Preconditions (all must pass before any edit)

1. `python scripts/review_session.py validate <review-id>` — package
   intact, evidence hashes unchanged.
2. `python scripts/review_findings.py approved <review-id>` — the ONLY
   source of work items. It validates the whole findings file first; if
   it errors, stop and report. An empty list means nothing to implement.
3. Compare `manifest.json`'s `source_commit` with the current checkout
   (`git rev-parse HEAD`). If they differ, note it: every finding must be
   revalidated against the CURRENT code before fixing (step 3 below).
4. Report the current branch, HEAD and `git status`; never overwrite
   unrelated local changes. Create branch `review/<review-id>` from the
   current main line and work there.

## Per approved finding — in severity order

1. Honor `implementation_eligibility`:
   - `eligible` — proceed.
   - `eligible_with_reproduction` (PROBABLE defect) — reproduce FIRST
     (smallest harness journey through real handlers —
     `run_user_turn` / `run_failing_user_turn` — or a documented manual
     reproduction). If reproduction fails, set the finding to
     `needs_investigation` with notes and skip it; it is not yet a fact.
   - `eligible_as_experiment` (SPECULATIVE improvement) — implement only
     the explicitly approved experiment scope; never present it as a
     defect fix.
2. Re-inspect the affected CURRENT code (`show-finding` prints the cited
   evidence). If the code changed since the review and the issue is gone,
   set the finding to `already_resolved`; if the evidence no longer maps,
   set `stale`. Never blindly apply a stale recommendation.
3. Identify the architectural root cause — fix the cause, not the
   recorded example. If the approved scope turns out to be wrong (the
   real fix exceeds it), STOP that finding: set `blocked` or
   `needs_investigation` with notes, continue with independent findings,
   and report. Never expand scope silently.
4. Add regression coverage BEFORE the fix where technically meaningful
   (failing test first). Default vehicle: harness journey + trace
   assertions; the one-turn harness is the supported reproduction tool —
   do not build or claim a full-session replay engine.
5. Implement the fix, preserving unrelated behavior. Run the focused
   tests for this finding.
6. One logical commit per independent finding (reference the finding id
   in the commit message). If two findings are truly inseparable, one
   commit is acceptable — say why in its message.
7. Update the finding: `implementing` → `implemented` with a
   `final_resolution` (or `verification_failed` if its tests fail and
   you cannot fix them within the approved scope).

## After all findings

1. Full verification: `make validate` (compile, ruff, full pytest,
   evaluations, preflight, release build). Report every result honestly;
   on failure, fix or mark the responsible finding `verification_failed`
   — never weaken existing tests to pass.
2. Re-validate findings: `python scripts/review_findings.py validate
   <review-id>`, then `render-report`.
3. Commit the updated `findings.json`/`report.md` on the review branch.
4. Push `review/<review-id>`. **Never merge** — merging is the
   operator's manual decision.
5. Write `reviews/<review-id>/implementation_report.md`: per finding —
   confirmed root cause, fix, tests added, verification results, commit
   SHA; plus the full-suite/evaluation/validate outcomes and anything
   skipped or blocked. Commit it, and end by telling the user the branch
   name and that nothing was merged.
