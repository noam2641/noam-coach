# Implementation Report — Review 2026-07-18_1

Branch: `review/2026-07-18_1` (pushed, **never merged** — merging is the
operator's manual decision).
Base: `99cf889` (the review's recorded `source_commit` — verified identical
to the checkout before any edit).
Approvals: recorded in `findings.json` (approver: noam, per-finding scopes).

## Per-finding outcomes

### F-01 · HIGH · CONFIRMED · defect → **implemented** (`54d34e4`)
**Confirmed root cause (generic, not the sleep item):** wizard deferral had
no terminal state. `skip_health_wizard_item` appended the step to
`deferred`, and `_next_wizard_step` returned the first deferred item
whenever nothing else remained — with one item left this re-asked the
identical question forever (production events 66/75/84 → flow restarts
71/80/89). A skip pressed on a stale card after the wizard finished even
restarted the wizard from scratch (bookkeeping already cleared).
**Fix (progression layer, all step kinds):** first skip defers and its
return pass is announced ("🔁 חוזר לפריט שדחית…" — never a byte-identical
screen); a second skip of the same item is terminal for the run (new
`skipped` list; the fact stays pending, nothing applied or invalidated);
`_wizard_payload()` is the single payload shape all three state writers
persist, eliminating the forgot-a-list bug class; skip on a finished
wizard refuses with a visible toast instead of restarting.
**Tests:** exact incident sequence; "no identical re-render" invariant;
generic defer-then-advance ordering; whole-wizard termination under
repeated skips. Existing RE10-04/RE13 wizard suites untouched and green.

### F-02 · MEDIUM · PROBABLE → reproduced → **implemented** (`30fbf9f`)
**Reproduction first (conditional approval honored):** driving the real
`handle_callback` with a consumed approval produced zero visible output —
the new regression failed against the pre-fix code exactly as production
events 590/601, then passed after the fix (`synthetically_reproduced`).
**Confirmed root causes (two silent paths):** `persist_meal` → None hit a
bare `return True`; and the duplicate-tap gate ran after the query had
been empty-answered, so no toast could ever display (Telegram shows only
the first answer to a callback query).
**Fix:** duplicate-tap check moved before the pre-answer and answers with
a visible toast; a press on a consumed approval edits the card to an
explicit terminal state ("הארוחה הזו כבר טופלה ✅"). Rejection and
idempotency preserved (regression asserts zero meals written).

### F-03 · MEDIUM · CONFIRMED · observability_gap → **implemented** (`30fbf9f`)
**Fix:** `services/control_refusal.py` — every intentional refusal emits
canonical `decision.finalized(entity=ui_control, outcome=refused, reason)`
(duplicate_tap / stale_version / approval_already_handled /
wizard_already_finished; callback payloads as digests). The router's
stale-version gate emits it alongside the preserved legacy
`stale_callback_recovered`. `ui.safe_answer_callback` records every
TEXTUAL toast as a delivery event (`entity=callback_ack`,
`operation=callback_ack`, correlated); empty spinner-stop acks are
deliberately not evented. A refusal, its acknowledgement, a hang and a
delivery failure are now four distinguishable things in the trace.

### F-06 · MEDIUM · PROBABLE → **needs_investigation** (not implemented, per scope) (`21bbc41`)
The approved scope said implement only if the parallel-card/stale-version
interaction is confirmed. It is **not**: a wizard control carrying an
outdated version takes the router's explicit stale-recovery path (visible
text + legacy event + canonical refusal) — locked in as an executable
investigation record (`test_f06_stale_wizard_version_press_is_visibly_recovered`).
The production silence at event 275 is attributed to an unhandled handler
exception, which was invisible before R1 error capture; if it recurs,
`error.captured` will pinpoint it. No navigation redesign attempted.

### F-04 / F-05 / F-08 / F-09 · LOW · CONFIRMED → **implemented** (`6991aa3`)
One commit for the four independently-testable presentation fixes (single
approval scope, shared regression artifact — documented in the commit):
- **F-04** goal-weight guard example derives from the user's current
  weight and direction (0.9×/1.1×) instead of the hardcoded "83".
- **F-05** `goal_status_line()` extracted; the "נדרש עוד:" header renders
  only when the soft-precision list is non-empty.
- **F-08** `diet_restrictions` label is the level-neutral
  "העדפות והגבלות תזונה" (registry + display map + edit menu), and the
  chosen classification level is persisted per item
  (`diet_restriction_levels` fact) instead of surviving only in ack text.
- **F-09** `format_remaining_budget_line()` phrases overshoot as
  "חריגה של X" (the status screen's convention), never a raw negative.

### F-07 · LOW · PROBABLE → **needs_investigation** (operator decision)
Untouched, as instructed: workout source/date/coaching-day boundary and
attribution to be determined before any fix.

## Workflow fix discovered during this implementation
`scripts/review_findings.py` / `review_session.py` crashed printing "→"
on a cp1255 Windows console (found on first real approval). Both CLIs now
force UTF-8 stdout with replacement. Known gap left open: `set-status`
has no `--resolution` flag, so `implemented` transitions (which the
validator correctly refuses without a `final_resolution`) require editing
findings.json directly; candidate improvement for the next workflow batch.

## Verification
- Focused suites per finding: review regressions (14 tests) + RE10-04 +
  RE13 wizard suites + callback grammar/delivery suites + goal/post-meal
  contract suites — all green (run per batch before each commit).
- Full pipeline on the final tree (with the unrelated pre-existing WIP
  restored): see the summary appended below.

## Preserved behavior (explicit checks)
- Wizard confirm/edit paths, RE10-04/RE13 suites: unchanged, green.
- Duplicate-tap suppression semantics (families, 1.2 s window): unchanged;
  only visibility added. False-positive regression proves distinct
  presses are never refused.
- Stale-version recovery UX and its legacy event: byte-for-byte behavior
  preserved; only the canonical refusal event added.
- Meal save idempotency: regression asserts a refused press writes zero
  meal rows.
- The unrelated uncommitted WIP (16 files) was stashed during the work,
  never committed, and restored unchanged afterward.

## Verification summary (final tree, unrelated WIP restored)

- Compilation: pass · Ruff: pass ("All checks passed!")
- Full pytest (`-o addopts="" --maxfail=0`): first run 1814 passed /
  **2 failed** — both were architectural contracts my F-02 change had
  violated (dispatcher-size ≤110 lines; TASK-14 exact button vocabulary).
  Fixed properly in `efb1fb5` (extraction + the established terminal
  keyboard pattern; no test weakened). Final run: **1816 passed, 0
  failed, 0 deselected**.
- Evaluations: 33/33 passed · Preflight: passed · Release build: pass.

## Commits on review/2026-07-18_1

| Commit | Content |
|---|---|
| `54d34e4` | F-01 — wizard skip terminal state |
| `30fbf9f` | F-02+F-03 — visible, trace-evident refusals + callback-ack instrumentation |
| `21bbc41` | F-06 — investigation record (not confirmed; not implemented) |
| `6991aa3` | F-04+F-05+F-08+F-09 — presentation/classification fixes |
| `efb1fb5` | F-02 follow-up — dispatcher-size + TASK-14 contract compliance |
| (final)   | findings/report/implementation-report record |
