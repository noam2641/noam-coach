# Session-Driven Continuous Improvement Workflow — Design Review

Status: **IMPLEMENTED (R1–R6).** This document is the design record; the
as-built summary is §0b, the operator guide is
`docs/CONTINUOUS_IMPROVEMENT_WORKFLOW.md`, and the judgment protocol is
`docs/REVIEW_PROTOCOL.md`.
Date: 2026-07-18 (revalidated and implemented against HEAD `a807653`)
Branch: `codex/post-observability-architecture`

## 0b. As-built summary (deltas from the sections below are authoritative here)

| Batch | Commit | Content |
|---|---|---|
| R1 | `1b386ee` | `error.captured` at three real boundaries (`observed_handler` correlated + re-raise; `observed_error_callback` wrapping `on_error` at registration — jobs/dispatch net, transient Telegram noise excluded; `mini_obs_scope` excluding HTTPException); per-exception-object dedup; `harness.run_failing_user_turn` |
| R2 | `624a3a9` | `review_window.py` (Requested vs Resolved selection, cursor as convenience pointer with atomic writes) + additive `event_log` window primitives (id-range resolution, forward pagination past the 2000-row ceiling) + `build_session_trace` |
| R3 | `d103273` | `session_review.py` — 15 versioned detectors, signals with event-id evidence + digests only; repetition summarized, never declared systemic |
| R4 | `28244c4` | `review_package.py` + `scripts/review_session.py` (build/list/show/validate) — immutable hashed packages, atomic promotion, fail-closed committable redaction, pseudonymized user scope, snapshot-safe read-only build, `--rebuild` |
| R5 | `5ad295b` | `scripts/review_findings.py` (findings.json canonical, strict validator, computed implementation eligibility, approval records) + `show-finding`/`advance` (fully gated cursor move) + `docs/REVIEW_PROTOCOL.md` v1.0 |
| R6 | (this commit) | `.claude/skills/review-session` + `.claude/skills/implement-review`, operator guide, acceptance tests, as-built docs |

Key deltas vs the original design text below: canonical findings file is
**findings.json** (no YAML dependency); **report.md is derived** from it
by `render-report`, never hand-written; a **rebuild always reproduces the
recorded resolved id range** for non-explicit selections (cursor/time
modes drift by definition); cursor advance requires package hashes +
findings validation + report, all enforced by `advance`; detector count
is 15 (evidence-supported, not the illustrative 14); reproduction
vocabulary is explicit (`evidence_inspected` ≠ replay; the harness runs
one turn — no full-session replay engine was built, by decision); a
`reproduce-finding` CLI was deliberately NOT added — reproduction is
finding-specific harness test code written during implementation, with
`show-finding` as the evidence-navigation command.

## 0. Revalidation results and approved deltas (2026-07-18)

All design-time facts were re-verified against the checkout: SessionTrace
groups by `interaction_id` in canonical append order; loaders filter by
user/trace/interaction with `limit ≤ 2000`; the harness runs exactly one
turn through `observed_handler`; `observed_handler` awaits the handler with
no exception capture; `taxonomy.ERROR_CAPTURED` exists (sole emitter today:
`daily_flags_cas`); `make validate` = compileall + ruff + pytest +
evaluations + preflight + release build. Deltas adopted for implementation:

1. **Canonical findings format is `findings.json`**, not YAML — PyYAML is
   not a declared dependency and the workflow adds no new dependencies.
   `report.md` is *derived* from findings.json by a deterministic renderer.
2. **Error capture spans three real boundaries**, not one line:
   (a) `observed_handler` — correlated capture + re-raise;
   (b) the PTB dispatch/error boundary — `on_error` (callback_router.py)
   is wrapped at registration in `runtime.py` (the file itself has
   unrelated uncommitted changes and is not edited), covering scheduled
   jobs and anything else PTB routes to error handling; duplicate capture
   across nested boundaries is prevented by marking the exception object;
   `classify_telegram_error` transient/stale classifications are NOT
   recorded as crashes;
   (c) `mini_obs_scope` (mini_api.py) — API-boundary capture that excludes
   `HTTPException` (expected domain rejection). Background loops that
   intentionally own recovery (e.g. retention) are not captured, per the
   re-raise rule. Review CLI failures surface directly on the console and
   write no product events (the builder stays read-only).
3. **Time-window selection resolves to a canonical event-id range first**
   (min/max id for the requested UTC window), and all package extraction
   then pages by id — append order is the ordering authority, timestamps
   only define the requested window. Boundary semantics: start-inclusive,
   end-exclusive, UTC; naive inputs are treated as UTC.
4. **Cursor advancement ships with the findings validator (R5)** — the R2
   module provides atomic read/advance primitives, but no CLI advances the
   cursor until package + findings + report validation can all be checked.
5. Detector count follows evidence support, not the illustrative "14".

---

## 1. Executive summary

The mission is a deterministic engineering workflow:

> use the product → run one review command → get an Engineering Review
> Report → approve findings → run one implementation command → get
> architectural fixes + regression tests + verification + commit + pushed
> review branch + implementation report.

The architectural audit shows that **roughly 70% of this workflow already
exists** in the O1–O10 observability program and the repository's
verification pipeline. The session can already be reconstructed
deterministically, end to end, from `product_events` alone. What is missing
is a thin layer on top:

| Capability | Status |
|---|---|
| Canonical correlated event stream (trace/interaction/span, surface, status, outcome) | ✅ exists (`event_log.py`, migration 13) |
| Full coverage: ingress, routing, AI (11/11 call sites), validation, decisions, state, flows, renders, delivery, callbacks, Mini App views/actions, media, jobs | ✅ exists (O2–O8) |
| Machine trace model + deterministic human timeline | ✅ exists (`session_trace.py`) |
| Inspection CLI | ✅ exists (`scripts/trace_inspect.py`) |
| Deterministic reproduction / journey regression API | ✅ exists (`harness.run_user_turn`) |
| Redaction / capture modes (privacy of the review package) | ✅ exists (`redaction.py`, `modes.py`) |
| Full verification pipeline | ✅ exists (`make validate`) |
| **Session boundary ("everything since my last review")** | ❌ missing |
| **Review Package builder (one command, self-contained bundle)** | ❌ missing |
| **Deterministic signal scan (pre-analysis of failures / repetitions)** | ❌ missing |
| **Unhandled handler exceptions recorded in the trace** | ❌ missing (gap found in audit) |
| **Findings schema + approval mechanism** | ❌ missing |
| **Claude review protocol + implementation protocol (commands)** | ❌ missing |

The design below adds exactly those six pieces, in six small batches, with
**zero changes** to the event schema, zero new event stores, and no
duplication of SessionTrace, replay, or observability.

---

## 2. Architectural audit — what exists and is reused as-is

### 2.1 The canonical stream is already the Review Package's data source

`product_events` (O1) carries everything section "PRODUCT VISION step 3"
asks for: interaction timeline (`interaction.received` incl. pre-routing
flow snapshot), routing (`routing.decided` with handler/action/reason), AI
requests/responses (`ai.call.*` with purpose, model, redacted request,
preserved output, duration, failures), validations (`validation.*` with
violation codes), decisions (`decision.finalized/repaired/
fallback_selected` with raw-vs-final resolution), state mutations
(`state.mutated`, `flow.*` with before/after), rendered messages
(`ui.render.prepared` exact text + control model), delivery truth
(`delivery.*` incl. stale-edit fallback chains), callbacks
(`ui.control.activated` with proven source render), user corrections (meal
reanalysis `decision.finalized` with `{ai_grams, final_grams, reason}`
overrides; `MEAL_UNDONE`; fact invalidations), and Mini App semantics
(`ui.view.rendered`, `ui.action.activated`).

**Nothing new needs to be logged for the review** except one gap (§2.4).

### 2.2 Reconstruction and rendering are done

`SessionTrace → InteractionTrace → AITrace/RenderTrace` plus
`render_timeline()` already produce exactly the "human timeline" a reviewer
needs, with explicit `unresolved_links` (nothing guessed). The Review
Package builder is therefore a *composer*, not a new trace engine.

### 2.3 Deterministic reproduction is done

`harness.run_user_turn` drives one turn through the **real** ingress
envelope and returns the machine trace. This is the mandated vehicle for
"Suggested Regression Test" in findings: every behavioral finding can be
reproduced as a harness journey test asserting on trace semantics — the
strategy the B-program already standardized ("trace-based regression
strategy", post-observability report §20).

### 2.4 The one instrumentation gap that matters for reviews

`observed_handler` (telegram_ingress.py:239-244) opens the interaction
scope and calls the handler **without catching exceptions**. An unhandled
handler crash produces an interaction whose trace simply *stops* — no
`error.captured` event exists anywhere on that path (the taxonomy constant
exists; the only current emitter is the daily-flags CAS conflict path). For
a review workflow whose core question is "what failed?", crashes must be
first-class evidence, not an inference from a truncated trace.

### 2.5 Verification pipeline is done

`make validate` = compileall + ruff + full pytest + offline evaluation
suite + preflight + release build. The implementation protocol simply
invokes it; nothing new is built.

---

## 3. Design

### 3.0 Principles

- **Judgment lives in the skill, determinism lives in the repo.** Product
  code produces deterministic, tested artifacts (window, package, signal
  scan). Claude's multi-perspective analysis is a documented protocol
  (project skill), not product code. This keeps the system a workflow, not
  an AI-learning system, and keeps every code artifact testable.
- **One command UX, layered internals.** The user runs `/review-session`;
  internally it is script (deterministic build) + skill (analysis).
- **Evidence is event ids.** Every signal and every finding cites
  `product_events.id` ranges / interaction ids, so any claim can be
  re-derived from the store and replayed with `trace_inspect.py`.

### 3.1 Session boundary — a review cursor, not sessionization

"Session" = **everything since the last completed review** (matching the
real usage pattern: use for hours/days, then review). No time-gap
heuristics, no new DB tables.

- `reviews/state.json` (repo-local, engineering-workflow state — *not*
  product state, so it deliberately does not live in the product DB and
  works unchanged against a copied DB snapshot):
  `{"cursor_event_id": <last reviewed product_events.id>, "advanced_at": …, "package": …}`
- Window resolution: `(cursor_event_id, MAX(id)]` for the configured user;
  overrides `--since-id`, `--since <ISO time>`, `--last-hours N` for ad-hoc
  reviews. First run (no state file) defaults to `--last-hours 48` with an
  explicit notice rather than silently reviewing all history.
- The cursor **advances only when a review completes** (explicit
  `advance` step run by the review skill after the report is written), so
  an interrupted review never loses its window.

New module: `noam_coach/observability/review_window.py` (~100 lines).

### 3.2 Review Package builder — `scripts/review_session.py`

One CLI in the style of `trace_inspect.py`:

```
python scripts/review_session.py build   [--db PATH] [--user U] [--since-id N | --since T | --last-hours H] [--out DIR]
python scripts/review_session.py advance --package reviews/<id>   # move cursor to package end
python scripts/review_session.py list
```

`build` loads the window's events via the **existing** `event_log.list_events`
/ `session_trace` model and writes a self-contained package directory
`reviews/<YYYY-MM-DD>_<n>/`:

| File | Content | Producer |
|---|---|---|
| `manifest.json` | window (event-id range, time range, user), db path, counts, package version | new |
| `timeline.md` | deterministic human timeline, per trace | existing `render_timeline` |
| `events.jsonl` | raw redacted events of the window (self-contained even if retention later prunes the DB) | existing `ProductEvent` → asdict |
| `stats.json` | per-family event counts, interactions per surface/kind, AI calls per purpose (count/failures/p50-p95 duration), flows started/completed/expired | new (pure aggregation) |
| `signals.json` + `signals.md` | deterministic signal scan (§3.3) | new |

The builder is read-only against the DB, adds **no un-redacted source**
(events were redacted at write time), and never calls AI.

### 3.3 Deterministic signal scan — `noam_coach/observability/session_review.py`

Pure functions `SessionTrace → list[Signal]`; each `Signal` has
`kind`, `severity_hint`, `evidence` (event ids + interaction ids),
`count`, and `systemic: bool` (same kind ≥3 occurrences in the window ⇒
flagged systemic, per the "repeated issue ⇒ systemic problem" requirement).
Detectors (all derivable from existing events — verified in the audit):

1. `ai_call_failed` — `ai.call.failed`, grouped by purpose.
2. `ai_call_unresolved` — started with no completion (from `unresolved_links`).
3. `delivery_failed_terminal` — `delivery.failed` with no subsequent success on the same render.
4. `decision_fallback` / `decision_repaired` — grouped by reason (repeated fallback = systemic degradation the user silently experienced).
5. `validation_failed` — grouped by entity/violation codes.
6. `handler_exception` — `error.captured` (enabled by Batch R1).
7. `flow_expired` / `flow_abandoned` — flows started but expired/replaced without completion.
8. `user_correction` — meal reanalysis overrides, `MEAL_UNDONE`, fact invalidations; repeated corrections of the same entity ⇒ systemic.
9. `user_retry` — near-identical consecutive user inputs, or the same callback pressed repeatedly on one render (frustration/UI-not-responding evidence).
10. `unresolved_correlation` — from `InteractionTrace.unresolved_links`.
11. `observability_write_failed` — the trace itself degraded during the window.
12. `ai_purpose_unclassified` — a new un-wrapped AI call site appeared.
13. `legacy_uncorrelated_in_window` — new code wrote events outside any scope ⇒ instrumentation-coverage regression.
14. `interaction_without_output` — user input that produced no delivered render and no state change (the "bot ignored me" class).

The scan **pre-focuses attention and guarantees a floor** (mechanical
failure classes can't be missed); it does not cap the review — Claude's
analysis (§3.5) covers UX, conversation design, AI reasoning quality, and
everything judgment-shaped, reading the full timeline.

### 3.4 Findings schema + approval — `reviews/<id>/findings.yaml`

Machine-readable findings file, one entry per finding, exactly the mandated
fields:

```yaml
- id: F-2026-07-18-01
  title: …
  severity: critical | high | medium | low
  evidence_level: CONFIRMED | PROBABLE | SPECULATIVE
  evidence:            # facts only — event ids, interaction ids, quotes from timeline
    - events: [18234, 18241]
      interaction: in_…
      note: "…"
  root_cause_hypothesis: …   # clearly an assumption unless evidence_level CONFIRMED
  affected_components: [noam_coach/bot/meals.py, …]
  suggested_fix: …
  suggested_regression_test: …   # phrased as a harness journey + trace assertions
  implementation_scope: S | M | L
  status: proposed        # → approved | rejected | deferred  (human-edited)
```

Approval = the human edits `status:` (or replies "approve F-…01, F-…03" and
Claude edits it for them — the file remains the single source of truth).
`scripts/review_findings.py` validates the schema and lists approved
findings; the implementation command refuses findings whose status is not
`approved`, and refuses `SPECULATIVE` findings outright (only `CONFIRMED`
may flow to implementation automatically; an approved `PROBABLE` finding
requires a confirmation step — reproduce it via the harness first — before
any fix).

`REVIEW_REPORT.md` (human-readable) is rendered from the same findings +
session stats; findings.yaml is authoritative.

### 3.5 The review command — project skill `/review-session`

`.claude/skills/review-session/SKILL.md` — the protocol Claude follows:

1. Run `scripts/review_session.py build` (or accept an existing package).
2. Read `manifest.json`, `stats.json`, `signals.md`, `timeline.md`
   (chunked; `events.jsonl` consulted for evidence detail).
3. Analyze across the mandated perspectives: product bugs, UX, conversation
   design, AI assumption quality, state/memory/workflow consistency,
   routing, duplicated logic, architectural smells, tech debt, missing
   regression tests / observability / validations; repetition ⇒ systemic.
4. Separate facts (event-cited) from assumptions; assign evidence levels
   (CONFIRMED = provable from events; PROBABLE = consistent hypothesis;
   SPECULATIVE = plausible but unverified).
5. Write `findings.yaml` (all `status: proposed`) + `REVIEW_REPORT.md`.
6. Run `review_session.py advance` (review completed ⇒ cursor moves).
7. **Stop.** Present the report and wait for approval. Nothing is
   implemented.

### 3.6 The implementation command — project skill `/implement-review`

`.claude/skills/implement-review/SKILL.md` — invoked as
`/implement-review reviews/<id>` after approval:

1. Load approved findings via `review_findings.py approved` (schema-gated).
2. Create branch `review/<package-id>` from the current main line.
3. Per finding (or per coherent group): identify the *architectural* root
   cause (read the code, not just the trace); reject local patches when the
   trace shows a systemic signal; implement the fix; add a regression test
   — default vehicle `harness.run_user_turn` + trace assertions, matching
   the established B-program strategy; run focused tests.
4. After all findings: full suite, `python scripts/run_evaluations.py`,
   `make validate` (release verification included).
5. One logical commit per finding (repo convention from RE/B programs),
   push `review/<package-id>`, never merge.
6. Write `reviews/<id>/IMPLEMENTATION_REPORT.md`: per finding — root cause
   (confirmed), fix, tests added, verification results, commit SHA;
   explicitly list preserved-behavior checks.

### 3.7 Privacy / git policy for `reviews/`

Content in packages is redacted at write time by the canonical boundary,
but `events.jsonl` and `timeline.md` still contain personal conversational
content (that is inherent to session replay — same posture as the DB
itself). Policy: `.gitignore` `reviews/**/events.jsonl` and `timeline.md`;
**commit** `manifest.json`, `stats.json`, `signals.*`, `findings.yaml`,
`REVIEW_REPORT.md`, `IMPLEMENTATION_REPORT.md` on the review branch so the
engineering record is durable while raw conversational material stays
local. (Flippable by one gitignore line if full packages should be
committed instead.)

---

## 4. Design challenges (alternatives considered and rejected)

1. **"One product command that also analyzes" (analysis in product code).**
   Rejected: heuristic "bug classification" in product code would be a
   brittle, untestable simulation of judgment, and drifts toward the
   explicitly-forbidden self-evaluating system. The split — deterministic
   scan in code, judgment in a versioned skill protocol — keeps every repo
   artifact testable and the workflow auditable.
2. **Cursor in the product DB.** Rejected: review state is engineering
   state; keeping it repo-local means reviews work against DB snapshots and
   the product schema stays untouched.
3. **Time-gap sessionization (infer sessions from inactivity).** Rejected:
   nondeterministic edge cases, and it answers a question nobody asked —
   the real boundary is "since my last review".
4. **New `review_*` event types / separate review log.** Rejected: the
   spec's own principle — do not duplicate observability. The package is a
   *view* over the canonical stream.
5. **Auto-advancing the cursor at build time.** Rejected: a crashed or
   abandoned review would silently swallow the window. Advance is an
   explicit completion step.
6. **Telegram-delivered review reports.** Deferred: the review is an
   engineering artifact consumed in the IDE; delivering it through the
   product surface adds coupling for no workflow value today. The design
   leaves room (a package is a directory; any future renderer can consume
   it).

---

## 5. Implementation batches

Each batch: one responsibility, independently testable, regression tests,
one logical commit, no protected-file edits (all new modules or the
established wrap points).

| # | Batch | Content | New/changed files | Tests |
|---|---|---|---|---|
| R1 | Crash evidence | `observed_handler` catches handler exceptions → emits `error.captured` (entity=handler, error type/class, redacted message, routing context) → **re-raises** (PTB error handler unchanged). Same envelope for the proactive-job wrapper. | `telegram_ingress.py` (+~25 lines), `health_jobs` wrap | harness turn with raising handler ⇒ trace contains `error.captured`, exception still propagates; job-path test |
| R2 | Review window | cursor state file, window resolution (cursor / since-id / since-time / last-hours), first-run default | `noam_coach/observability/review_window.py`, `reviews/` + gitignore | window math, cursor lifecycle, snapshot-DB behavior, first-run notice |
| R3 | Signal scan | 14 detectors + systemic aggregation over `SessionTrace` | `noam_coach/observability/session_review.py` | per-detector fixture traces (harness-generated), systemic threshold, empty-window |
| R4 | Package builder | `scripts/review_session.py` (`build`/`advance`/`list`), stats aggregation, package layout, manifest | `scripts/review_session.py` | end-to-end: harness-generated DB ⇒ build ⇒ assert package contents deterministic; advance semantics |
| R5 | Findings tooling | findings schema, `scripts/review_findings.py` (`validate`/`approved`/`set-status`), CONFIRMED/PROBABLE gate | `scripts/review_findings.py`, schema doc | schema validation, status gating, SPECULATIVE refusal |
| R6 | Workflow protocol | `/review-session` + `/implement-review` skills, workflow doc (`docs/CONTINUOUS_IMPROVEMENT_WORKFLOW.md`) | `.claude/skills/…`, docs | n/a (docs); smoke: skills reference only existing commands |

Dependency order: R1 independent; R2→R4; R3→R4; R5 independent; R6 last.
Estimated total: ~6 commits, ~1.2–1.5k lines including tests, no schema
migration, no protected-file edits.

## 6. Success-criteria walkthrough

1. Use the bot normally (hours/days). All evidence accrues in
   `product_events` — already live.
2. `/review-session` → builder creates `reviews/2026-07-20_1/`; Claude
   analyzes; you receive `REVIEW_REPORT.md` + `findings.yaml`; cursor
   advances.
3. You mark findings `approved` (or tell Claude which).
4. `/implement-review reviews/2026-07-20_1` → root-cause fixes, harness
   regression tests, focused tests, full suite, evaluations,
   `make validate`, one commit per finding on pushed branch
   `review/2026-07-20_1`, `IMPLEMENTATION_REPORT.md`. No merge.
5. Repeat — next review window starts where this one ended.
