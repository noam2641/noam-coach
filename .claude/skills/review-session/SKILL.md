---
name: review-session
description: Build a review package from real production usage and run the full multi-perspective engineering review. Ends with a findings report awaiting approval — never changes product code. Args: optional selection (default --since-last-review; or --last-hours N / --start T --end T / --rebuild ID) and optional focus (full|nutrition|workout|conversation|ux|architecture|reliability).
---

# /review-session — session-driven engineering review

You are running the review half of the continuous-improvement workflow.
The deterministic mechanics live in repository code; your job is the
qualitative analysis, following `docs/REVIEW_PROTOCOL.md` (the versioned
protocol — read it before analyzing; its rules override this summary).

## Steps

1. **Determine selection.** Default: `--since-last-review`. If the user
   passed a selection in the args, use it verbatim. If the cursor does not
   exist yet (first review), the builder will say so — rerun with
   `--last-hours 48` and tell the user you did.
2. **Build the package** (read-only; no cursor movement):
   `python scripts/review_session.py build <selection>`
   Note the printed review id. On `--rebuild <old-id>`, the recorded
   selection is reused and a linked `<old-id>_rbN` package is created.
3. **Verify integrity:** `python scripts/review_session.py validate <id>`.
4. **Analyze** per `docs/REVIEW_PROTOCOL.md`: read `manifest.json`,
   `stats.json`, `signals.md`, then the FULL `timeline.md` (chunked if
   large), drilling into `events.jsonl` where property-level evidence is
   needed. Signals are a floor and navigation aid — every CRITICAL/HIGH
   signal must become a finding or be explicitly dispositioned in the
   report; they never limit what you may find. Apply the requested focus;
   critical deterministic signals stay visible under every focus.
5. **Write findings:** `python scripts/review_findings.py init <id>
   [--focus F]`, then fill `reviews/<id>/findings.json` — every finding
   with the full schema, `status: proposed`, facts separated from
   assumptions, honest evidence levels and reproduction vocabulary
   (`evidence_inspected` means you read the trace — never call that
   "replayed"). Quote conversation text minimally; findings.json is
   committable.
6. **Validate:** `python scripts/review_findings.py validate <id>` must
   pass. Fix findings until it does.
7. **Render the report:** `python scripts/review_findings.py
   render-report <id>` (report.md is always derived, never hand-written).
8. **Complete:** `python scripts/review_session.py advance --review <id>`
   — the only point the last-review cursor moves; it refuses if anything
   above was skipped. If advance refuses, fix the stated problem; do not
   force anything.
9. **Stop.** Summarize the findings for the user (id, title, severity,
   evidence level, one-line impact each) and how to approve:
   `python scripts/review_findings.py set-status <id> <finding> approved
   --approver <name> --scope "<approved scope>"` (or ask you to do it).
   Make NO product-code change in this flow. End your summary with:

   **Engineering Review Required.**
