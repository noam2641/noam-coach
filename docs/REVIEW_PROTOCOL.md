# Session Review Protocol — v1.0

This is the versioned protocol Claude follows when reviewing a Noam Coach
review package. The deterministic mechanics (selection, package building,
signal detection, findings validation, cursor advancement) live in tested
repository code; **this document owns the qualitative judgment**. The
protocol version is recorded in every package manifest and findings file
(`noam_coach/observability/review_versions.py` — bump it when this
document changes materially).

## Inputs

A complete package under `reviews/<review_id>/`:
`manifest.json` (identity/versions/selection), `stats.json` (volumes and
durations), `signals.json` + `signals.md` (deterministic scan),
`timeline.md` (human timeline), `events.jsonl` (raw redacted evidence).
Verify integrity first: `python scripts/review_session.py validate <id>`.

## Analysis order

1. Read `manifest.json` and `stats.json` — window shape, volumes, AI
   failure ratios, delivery health.
2. Read `signals.md` — the mechanical floor. Every CRITICAL/HIGH signal
   must be either turned into a finding or explicitly dispositioned in
   the report (with a stated reason).
3. Read `timeline.md` in full, interaction by interaction. The signals
   are a navigation aid, **never a limit** — most UX, conversation and
   AI-quality findings come from reading what the user actually
   experienced.
4. Drill into `events.jsonl` for any interaction that needs
   property-level evidence (`show-finding` prints cited events later).

## Review perspectives (all of them, every full review)

Software correctness · UX · conversation clarity (tone, repetition,
dead-ends, Hebrew phrasing) · AI interpretation quality (wrong intent,
wrong assumptions, ignored context) · nutrition behavior · workout
behavior · routing · state consistency · memory/context consistency ·
validation coverage · delivery correctness · observability coverage ·
regression coverage · architecture smells evidenced by behavior ·
technical debt evidenced by behavior · improvement opportunities.

### Focus mode

The operator may narrow a review: `full` (default) | `nutrition` |
`workout` | `conversation` | `ux` | `architecture` | `reliability`.
Focus narrows perspectives 4–16 — it NEVER hides deterministic signals:
every CRITICAL/HIGH signal appears in the findings/report regardless of
focus (a crash does not disappear during a nutrition review). Record the
focus in `findings.json`.

## Evidence discipline

Every conclusion separates: **observed fact** (cited event ids) →
**interpretation** → **assumption** → **root-cause hypothesis** →
**recommendation**. The findings schema enforces the separation
(`deterministic_facts` / `explicit_assumptions` / `missing_information`).

Evidence levels: `CONFIRMED` — provable from the cited events alone;
`PROBABLE` — the evidence strongly suggests it, but an alternative
explanation exists; `SPECULATIVE` — plausible pattern, unverified.
Never inflate: a PROBABLE claim labeled CONFIRMED corrupts the approval
gate downstream.

Severity ≠ evidence ≠ frequency ≠ scope. One reproducible crash may be
CRITICAL; ten wording papercuts stay LOW. Three similar retries do NOT
prove one shared root cause: use `repeated` or `systemic_candidate`
until a shared implementation cause is verified — `systemic_confirmed`
requires CONFIRMED evidence of the shared cause plus a stated root
cause, and the validator enforces that.

Reproduction status is honest vocabulary: `evidence_inspected` (you read
the trace — this is NOT replay), `deterministically_replayed`,
`synthetically_reproduced` (harness journey with synthetic input),
`manually_reproduced`, `not_reproducible`, `reproduction_not_required`.
The existing harness runs ONE turn through the real ingress envelope
(`run_user_turn` / `run_failing_user_turn`); multi-turn findings get the
smallest justified journey fixture through real handlers, not a claimed
"session replay" that does not exist.

## Output contract

1. `findings.json` — the canonical machine-readable findings
   (`scripts/review_findings.py init <id>` creates the shell; the schema
   is enforced by `validate`). All findings start `status: proposed`.
   Quote conversation text minimally — only what a finding needs;
   findings.json is committable and must stay free of secrets and raw
   identifiers.
2. `report.md` — derived, never hand-written:
   `python scripts/review_findings.py render-report <id>`.
3. Validate: `python scripts/review_findings.py validate <id>` must pass.
4. Complete: `python scripts/review_session.py advance --review <id>` —
   this is the ONLY point the last-review cursor moves, and it refuses
   unless package + findings + report are all valid.
5. **Stop. Make no product-code change.** End with:
   `Engineering Review Required.`

## Approval rules (enforced by the validator, restated for humans)

- CONFIRMED findings may be approved for implementation as-is.
- PROBABLE findings may be approved, but a PROBABLE **defect** must be
  reproduced (harness or manual) before the implementation flow may
  treat it as a factual defect (`eligible_with_reproduction`).
- SPECULATIVE findings can never be approved as defect fixes; only a
  SPECULATIVE `improvement_opportunity` may proceed, as an explicitly
  approved experiment (`eligible_as_experiment`).
- Approval requires an approval record (approver, timestamp, scope);
  hand-editing eligibility is rejected by the validator.
