# Continuous Improvement Workflow — Operator Guide

Real usage becomes engineering input: use the bot, run one review
command, approve findings, run one implementation command. Nothing is
implemented without your explicit approval; nothing is ever merged
automatically.

Component map: selection/cursor — `noam_coach/observability/review_window.py`;
package builder — `noam_coach/observability/review_package.py` +
`scripts/review_session.py`; detectors —
`noam_coach/observability/session_review.py`; findings/report —
`scripts/review_findings.py`; judgment protocol — `docs/REVIEW_PROTOCOL.md`;
Claude commands — `.claude/skills/review-session`, `.claude/skills/implement-review`.

## 1. Daily use

Use Noam Coach normally (Telegram + Mini App) for hours or days. All
evidence accrues in the canonical `product_events` stream — nothing to
run during usage.

## 2. Review since the last successful review

In Claude Code:

    /review-session

Claude builds the package (`--since-last-review`), analyzes it under the
protocol, writes `reviews/<id>/findings.json` + `report.md`, advances the
cursor, and stops with **Engineering Review Required.** On the very first
review there is no cursor yet — Claude falls back to `--last-hours 48`
and says so.

Manual equivalent (no Claude analysis — package only):

    python scripts/review_session.py build --since-last-review

## 3. Review an explicit historical period

    /review-session --start 2026-07-14T00:00 --end 2026-07-16T00:00
    /review-session --last-hours 12
    /review-session --focus nutrition        # focus narrows judgment,
                                             # never hides critical signals

Times are UTC, start-inclusive, end-exclusive. Explicit selections ignore
the cursor entirely — overlapping and already-reviewed ranges are fine.

## 4. Read the report

`reviews/<id>/report.md` — findings sorted by severity, each with facts
vs assumptions, evidence levels (CONFIRMED / PROBABLE / SPECULATIVE),
frequency and scope kept separate, and the exact inspect command.

## 5. Inspect a specific finding

    python scripts/review_session.py show-finding <id> <finding-id>

Prints the finding and the exact evidence events it cites. The full
timeline is `reviews/<id>/timeline.md`; the raw events are
`reviews/<id>/events.jsonl` (both local-only).

## 6. Approve or reject findings

    python scripts/review_findings.py set-status <id> F-1 approved \
        --approver noam --scope "fix the root cause + regression test"
    python scripts/review_findings.py set-status <id> F-2 rejected

(or tell Claude which to approve — the file is the source of truth).
Approval rules the validator enforces: CONFIRMED → implementable;
PROBABLE defect → must be reproduced before being treated as a fact;
SPECULATIVE → experiment/improvement only, never a defect fix. Editing
the file by hand cannot bypass this — every consumer validates first.

## 7. Implement

    /implement-review <id>

Claude loads ONLY approved findings, revalidates each against the current
code, reproduces where required, writes regression tests, fixes root
causes, runs focused tests and `make validate`, creates one commit per
finding on branch `review/<id>`, pushes it, and writes
`reviews/<id>/implementation_report.md`. **Never merges.**

## 8. Find the results

- Branch: `review/<id>` (pushed, unmerged — merging is your decision).
- Report: `reviews/<id>/implementation_report.md`.
- Finding states inside `findings.json`: `implemented` /
  `verification_failed` / `stale` / `already_resolved` / `blocked`.

## 9. Recover from an interrupted review

Nothing to undo: the cursor moves only at the final `advance`, so an
interrupted build or analysis leaves state untouched — run
`/review-session` again and the same window is selected. A half-built
package can only exist as `reviews/.building_*` (safe to delete). If
`reviews/state.json` is ever malformed the tools say so explicitly; fix
or delete it, or use an explicit selection.

## 10. Rebuild an old review

    python scripts/review_session.py build --rebuild <old-id>

Re-resolves the recorded selection into a NEW linked package
`<old-id>_rbN` (`rebuilt_from` in its manifest). Existing packages are
immutable — evidence under findings is never silently rewritten, and
`validate <id>` detects tampering by hash.

## 11. Delete private review artifacts safely

The private artifacts are `reviews/<id>/events.jsonl` and
`reviews/<id>/timeline.md` (plus `reviews/state.json`'s convenience
cursor). Deleting them never breaks committed history — findings and
reports reference evidence by event id, and the canonical stream in the
product database remains the source of truth (a rebuild regenerates the
artifacts while retention keeps the rows). Deleting a whole package
directory is also safe; the cursor file stands alone.

## 12. What is and is not committed

Committable (sanitized by construction; the build fails closed
otherwise): `manifest.json`, `stats.json`, `signals.json`, `signals.md`,
`findings.json`, `report.md`, `implementation_report.md`.
Never committed (gitignored): `reviews/state.json`, `reviews/.building_*`,
`reviews/*/events.jsonl`, `reviews/*/timeline.md` — raw conversation
content stays on your machine even though it passed write-time redaction.

## Worked example

    # Mon–Wed: normal usage — meals, workouts, corrections, Mini App…

    /review-session
    #   → review package: 2026-07-22_1 (events 18240..19412)
    #   → report.md: 4 findings
    #     F-1 HIGH/CONFIRMED  menu fallback fired on every generation
    #     F-2 MEDIUM/CONFIRMED double reminder after reschedule
    #     F-3 MEDIUM/PROBABLE  intent misread when pain mentioned mid-meal
    #     F-4 LOW/SPECULATIVE  morning wording feels repetitive
    #   Engineering Review Required.

    python scripts/review_session.py show-finding 2026-07-22_1 F-1
    python scripts/review_findings.py set-status 2026-07-22_1 F-1 approved \
        --approver noam --scope "root-cause fix + regression"
    python scripts/review_findings.py set-status 2026-07-22_1 F-3 approved \
        --approver noam --scope "reproduce first; fix only if confirmed"
    python scripts/review_findings.py set-status 2026-07-22_1 F-2 deferred
    python scripts/review_findings.py set-status 2026-07-22_1 F-4 rejected

    /implement-review 2026-07-22_1
    #   → F-1: failing harness regression → root-cause fix → commit
    #   → F-3: reproduced via run_user_turn journey → confirmed → fix → commit
    #   → focused tests, full pytest, evaluations, make validate: PASS
    #   → pushed branch review/2026-07-22_1 (NOT merged)
    #   → reviews/2026-07-22_1/implementation_report.md

    # You review the branch and merge manually when satisfied.
