# Claude `/remote` Implementation Prompt — Master Correction Backlog Addendum

You are continuing implementation work in the `noam2641/noam-coach` repository.

Work from branch:

```text
codex/complete-rec-program-04
```

The audit anchor for the new findings is:

```text
bfb56d7f629ac96ac6909f63f140cbfa67af7ca1
```

Your goal is to implement the correction backlog as a coherent architecture, in dependency order, without repeating completed work or treating isolated passing tests as proof that a cross-system finding is closed.

## Authoritative sources and precedence

Read these before changing code:

1. `docs/MASTER_CORRECTION_BACKLOG_ADDENDUM_38_57.md`
   * This is authoritative for FIX 38–57, expansions to earlier FIX items, dependency maps, system-wide state maps, and execution batches.
2. The original full MASTER CORRECTION BACKLOG FIX 1–37, if it is provided separately.
   * The original definitions of FIX 1–37 are not reproduced in this repository at the audit anchor.
   * Do not reconstruct or reinterpret a missing FIX 1–37 from its number or from a batch name.
   * The addendum’s “Existing FIX items requiring expansion” section supplements FIX 1–37; it does not replace their original acceptance criteria.
3. Current production code, migrations, tests, and Git history.
   * Code evidence determines whether a finding is still open, partially covered, or already closed.
4. `docs/MASTER_TASKS.md`, `docs/FUNCTIONAL_UX_TRACEABILITY.md`, `docs/CONTINUATION_STATE.md`, `docs/WHAT_REMAINS.md`, and archived audit/reports.
   * Treat these as historical evidence, not as authority that the new correction backlog is closed.
   * In particular, `docs/MASTER_TASKS.md` says the earlier 22 tasks are complete. That does not close FIX 38–57 or their cross-system dependencies.

If the original FIX 1–37 document is still unavailable, proceed only with work whose complete required behavior is defined by FIX 38–57. Do not silently invent the missing base requirements. Record any blocked dependency precisely and continue with independent work.

## Meaning of the audit wording

The addendum says that no implementation was authorized “in this phase.” That statement records the completed audit phase. This prompt begins a separate implementation phase and authorizes repository code, test, documentation, and necessary backward-compatible schema/migration changes within the listed FIX scope.

Do not open a PR unless the user explicitly requests one.

## Mandatory startup inspection

Before editing anything:

1. Run `git status --short --branch`.
2. Confirm the active branch and current HEAD.
3. Inspect all commits after the audit anchor:

   ```bash
   git log --oneline --decorate bfb56d7f629ac96ac6909f63f140cbfa67af7ca1..HEAD
   ```

4. Inspect every existing staged and unstaged diff.
5. Preserve all pre-existing local work. Do not reset, discard, overwrite, stage, or commit unrelated changes.
6. If the worktree is dirty, identify ownership/scope per file. Work around unrelated changes and stage only explicit files.
7. Read repository instructions such as `AGENTS.md` if present.

There may already be local work in `noam_coach/bot/ui.py`. Treat any such diff as pre-existing unless you can prove it belongs to the current FIX. Never include it accidentally in a batch commit.

## First deliverable: verified implementation map

Before implementing, produce a concise table for FIX 38–57 with one of these statuses:

* `OPEN`
* `PARTIALLY COVERED`
* `ALREADY COVERED`
* `NEEDS PRODUCT DECISION`
* `BLOCKED BY MISSING FIX 1–37 DEFINITION`

For every item include:

* Exact files/functions inspected.
* Relevant commits already present.
* Tests that prove the behavior, if any.
* The remaining production gap.
* Dependencies that must be implemented first.

Do not mark a FIX covered because:

* a similarly named commit exists;
* a helper or data class exists;
* a unit test passes;
* one channel uses the new service while other consumers still bypass it;
* documentation says “COMPLETE.”

A FIX is covered only when the complete user action → handler/route → service → persistence → derived state → AI context → UI → old action → background job → restart/next-interaction path satisfies its required behavior.

## Architecture rule: one coherent coach

For every state-changing action, explicitly trace:

```text
user action
→ Telegram/Mini entry point
→ router/flow
→ domain service
→ database write
→ derived state and invalidation
→ AI context
→ Telegram messages and old callbacks
→ Mini App freshness
→ background jobs
→ restart/date rollover
→ next interaction
```

The implementation must converge toward:

* One canonical coaching-day resolver.
* One versioned user/day reality model.
* Atomic domain mutations.
* Explicit fact-read policies.
* Revision-bound projections and callbacks.
* One workout event/state resolver used by all consumers.
* Durable meal/planned-meal lifecycle identity.
* Cross-channel freshness and stale-write rejection.

Do not add a new parallel source of truth to patch one screen.

## Execution order

Follow the dependency batches in the addendum, with the following controls.

### Batch A — Reality versions and atomic state changes

Scope: FIX 41, FIX 43, FIX 57 and relevant parts of FIX 15, FIX 22, FIX 35.

FIX 41 is `NEEDS PRODUCT DECISION`. Do not choose a coaching-day boundary silently. Present the two audited options and their exact downstream effects. Continue only with Batch A work that does not hard-code the unresolved boundary. Once decided, implement one `CoachingDayService` and migrate all day-key consumers; do not create an eleventh TODAY model.

Before later batches, establish:

* Canonical day/reality revision.
* Atomic mutation/compare-and-swap behavior.
* Projection dependency metadata.
* Central invalidation/change-set contract.

### Batch B — Fact and safety authority

Scope: FIX 47, FIX 48, FIX 55 and available requirements from FIX 1, FIX 21, FIX 26.

Replace ambiguous reads with explicit decision/display/raw policies. Add a complete pain/constraint lifecycle. Safety changes must invalidate old food projections, and every final save/plan action must revalidate the current safety revision.

### Batch C — Conversation and callback identity

Scope: FIX 38, FIX 45, FIX 50, FIX 51 and available requirements from FIX 2, FIX 3, FIX 5, FIX 16, FIX 35.

Finish the flow-state migration rather than wrapping both engines indefinitely. Give every mutating callback an object/revision/single-use identity. Make Home, cancel, expiry, restart, and stale-message behavior explicit and tested.

### Batch D — Workout-day domain

Scope: FIX 39, FIX 40, FIX 52 and available requirements from FIX 8, FIX 27, FIX 28.

Centralize workout event, status, evidence level, actual/rescheduled time, cancellation, completion, bot/HealthKit reconciliation, and proactive suppression. Audit every raw-session/plan/routine reader and migrate it or document why it asks a genuinely different question.

### Batch E — Meal and planned-meal lifecycle

Scope: FIX 42, FIX 43, FIX 46, FIX 49 and available requirements from FIX 15, FIX 17–20.

Unify manual/photo interpretation around a task-appropriate context. Add durable planned-meal identity and lifecycle transitions. Meal create/correct/undo must propagate through totals, routine, menu, recommendations, follow-ups, Mini App, summaries, and old actions.

### Batch F — Menu/recommendation competition

Scope: FIX 43, FIX 50, FIX 55 and available requirements from FIX 6–16, FIX 21–23.

Define which active coaching object owns a text correction or action. Bind menu and next-meal projections to day, goal, meal, workout, preference, and safety revisions. Reject or refresh stale actions instead of applying them to the newest object.

### Batch G — Telegram/Mini synchronization

Scope: FIX 44, FIX 56 and available requirements from FIX 32, FIX 33.

Add revision-bearing APIs, focus/visibility refresh, optimistic concurrency, idempotency, and one request per user action. Verify Telegram changes while Mini remains open.

### Batch H — AI continuity and summaries

Scope: FIX 46, FIX 53 and available requirements from FIX 23, FIX 34.

Build task-specific AI contexts from the canonical reality snapshot. Keep deterministic facts and business rules authoritative. AI may interpret evidence or language but must not override targets, safety, state precedence, revisions, or confirmation policy.

### Batch I — Cross-system regression suite

Scope: FIX 37 and the addendum’s 30 test-coverage gaps.

Tests must be written alongside each batch. Batch I is the final system-level verification pass, not the first time lifecycle behavior is tested.

## Per-batch implementation protocol

For each batch:

1. Reconfirm the relevant FIX definitions and dependencies.
2. Trace all production readers and writers before choosing the shared abstraction.
3. Write failing regression/integration tests for confirmed gaps.
4. Implement the smallest coherent architecture that closes the entire batch dependency boundary.
5. Migrate all relevant consumers; do not leave known competing readers without an explicit reason.
6. Run targeted tests and Ruff for touched paths.
7. Run the broader related suite.
8. Run the full suite before the batch is declared complete.
9. Inspect `git diff` for accidental or unrelated changes.
10. Update traceability/backlog status with code evidence and exact test commands.
11. Commit only that batch’s files with a clear commit message.
12. Push the current branch after the commit.
13. Continue automatically to the next unblocked batch. Do not stop merely to ask whether to continue.

Pause only when:

* a `NEEDS PRODUCT DECISION` choice materially changes the architecture;
* required FIX 1–37 wording is missing and cannot be implemented safely from FIX 38–57 alone;
* unrelated local changes overlap the same lines and cannot be preserved safely;
* credentials or a real external service are required;
* a destructive or production-data operation would be necessary.

## Testing requirements

At minimum, add the system scenarios listed in the addendum, including:

* Meal create/correct/undo invalidation.
* Planned meal consumption and undo/reopen semantics.
* Allergy change after recommendation/menu rendering.
* Old callback after newer recommendation/goal/date rollover/restart.
* Home/cancel/restart across every continuation type.
* Restart during workout rest.
* Reported/verified/cancelled/rescheduled workout consistency across every consumer.
* Bot/HealthKit workout reconciliation and deduplication.
* Pain resolution across Telegram, Mini, planning, AI, and jobs.
* Midnight before sleep.
* Mini open while Telegram changes meals/goals/allergies/pain/workout.
* Concurrent `daily_flags` updates and duplicate requests.
* Health import semantic invalidation.
* Manual/photo interpretation parity.
* General-assistant reference continuity.
* Evening summary after corrections and explicit workout reports.
* Proactive revalidation immediately before delivery.

Do not weaken or delete existing tests merely to make the suite pass. If an existing test encodes behavior contradicted by the authoritative backlog, update it with an explicit explanation and replacement coverage.

## Commit, push, and reporting policy

* Stay on `codex/complete-rec-program-04` unless the user instructs otherwise.
* Never use `git add -A` in a mixed worktree. Stage explicit paths only.
* Do not include pre-existing unrelated changes.
* Do not rewrite or squash earlier history.
* Make one coherent commit per completed batch or narrowly coupled dependency unit.
* Push completed commits to `origin/codex/complete-rec-program-04`.
* Do not create a PR.

After every batch report:

* FIX items closed/partially closed/blocked.
* Root cause addressed.
* Files changed.
* Migrations added, if any, and compatibility behavior.
* Tests added/updated.
* Exact commands and results.
* Commit hash and push status.
* Remaining dependency and next batch.

## Final completion standard

Do not declare the backlog complete until:

1. Every available FIX has a code-evidence status.
2. Every confirmed defect in FIX 38–57 is implemented or explicitly blocked.
3. Every `ARCHITECTURAL RISK` has either been removed or converted into a documented, tested product policy.
4. Every `NEEDS PRODUCT DECISION` has a recorded user decision before dependent behavior is finalized.
5. Telegram, Mini App, AI contexts, background jobs, old actions, restart, and date rollover all consume the same intended reality.
6. The full test suite and lint pass.
7. The final `git status` is reported exactly.
8. Unrelated pre-existing work remains untouched.

The standard is not “all tests pass.” The standard is that the product behaves like one continuous, reliable coach with one coherent understanding of the user and today.
