# Final Post-Observability Architecture Report

Program: **MASTER POST-OBSERVABILITY ARCHITECTURE IMPLEMENTATION** (B1–B13),
branch `codex/post-observability-architecture`, executed autonomously on top of
the completed observability program (O1–O10) and the protected working tree
(17 modified + 2 untracked files, byte-frozen throughout).

---

## 1. Executive summary

Thirteen batches converted the audited architecture backlog (ARCH-01 … ARCH-16)
into shipped, regression-pinned architecture. The recurring theme: **state that
looked authoritative but wasn't** — day keys that split at midnight, callbacks
that resolved against regenerated lists, flows whose continuation stores
outlived them, facts that drove decisions before confirmation, "later" that was
stored as if it were a time. Each batch established one canonical owner
(coaching day, active_flow, active recommendation card, typed fact accessors,
the six-class coaching memory) and made every other reader derive from it,
enforced at install-time wrap seams so the protected files stayed byte-identical.

All work is verified by production-path tests asserting machine traces (the
canonical observability events), with the full suite green at zero deselections
after every batch, evaluations 33/33, and the release build clean.

## 2. Starting architecture at 8233abe

The pre-program baseline had the observability system (O1–O10) but the product
architecture still carried the audited defects: last-write-wins daily_flags
blobs; permissive callback token parsing (any trailing segment could be read as
a flow id/version); calendar-day nutrition state that split the coaching
conversation at midnight; next-meal controls that re-generated and resolved by
index; proactive prompts that ignored explicit cancellation; restart deleting
live deferred-plan continuations; wizard scratchpads that outlived their flows;
free-text references guessed by the LLM without candidates; ad-hoc memory
writes; ~55 raw fact reads in decision paths; and a resolver that (correctly,
since Batch D) refused to treat vague "later" as imminent but never collected a
real time. The quantity anchor fix (8233abe) was the identity pattern the
program generalized.

## 3. B1 / ARCH-03 result (`036fa58`)

`daily_flags` writes converged on a revision-CAS: `patch_daily_flags` (5
retries, each retry re-applies only the writer's own diff) and the task-local
`read_flags_for_update`/`commit_flags_update` ledger adapter, with a
`FIELD_OWNERS` registry naming one owning service per key and conflict
observability (`state.mutated(outcome=patched_after_retry)`,
`error.captured(entity=daily_flags_conflict)`). B13's sweep found **zero**
remaining full-blob writes.

An immediate production regression (startup `no such column: trace_id`) was
root-caused to O1 placing migration-13 indexes in the base SCHEMA and fixed in
`06fc1e5` (data-preserving; base-SCHEMA/migration ownership contract documented;
legacy-DB test now runs the real `init()`).

## 4. Callback grammar architecture (B2, `3617341`)

`noam_coach/services/callback_grammar.py` installs strict extractors over
`conversation.extract_version`/`extract_flow_id`: flow tokens must match
`^f(f-\d+-[0-9a-f]{6,})$`, versions `^v(\d{1,9})$`, scanned only in the last
two segments. Ordinary payload segments can no longer be misread as identity
tokens. The protected router resolves these as module attributes at call time,
so no protected file changed.

## 5. Confirmation entity-addressing (B2)

`confirm:*` callbacks pass a gate before the protected handler: cancel is
always admitted; otherwise the callback value must match the pending
`confirm_number` flow value, and the source render must be fresh (O3 render
registry vs the subject's `changed_at` — goal `decided_at`, fact `updated_at`).
Stale confirmations are refused with
`validation.failed(entity=confirmation, outcome=stale)` instead of mutating a
different entity than the one the user saw.

## 6. Coaching-day authority matrix (B3 `af0e89f`, verified adversarially in B4 `ec51f0d`)

`resolve_coaching_day` (sleep-anchored: confirmed bedtime + 4h grace → rollover
hour; calendar fallback without a confirmed sleep_schedule fact) is owned by
`daily_state` policy accessors (`coaching_day_for/key/bounds_utc`). Migrated to
coaching day: meal windows, daily_flags day key, daily menu day + menu-edit
memory, next-meal context/scales/rejections/recent titles, workout-status
clarification, proactive nutrition-quality gating, check-in flags. Intentionally
calendar: workout completion evidence, split selector, weekly aggregation,
proactive send budget, routine learning, Health-import attribution. B4's
midnight adversarial matrix (23:50 → 00:30 journeys) proved one coaching row,
one budget, no splits — including the Mini App write path (ARCH-07A).

## 7. Daily flags ownership/CAS architecture

See §3. Every flags key has exactly one owning service in `FIELD_OWNERS`
(including B12's `next_meal_workout_expected_at`); concurrent writers merge by
diff instead of clobbering; conflicts are observable events, not silent data
loss.

## 8. Recommendation/control identity architecture (B5 `fd0fa84`, hardened in B13)

TASK-03 exposes one top-ranked option and re-ranks per generation, so option
indexes are only meaningful against the **stored active card**. Service level:
`save_next_meal_option_feedback` (and nostock via delegation) resolve from
`get_active_recommendation_options`, refusing (ValueError → safe refresh UX)
without active state. Render level: the identity gate covers
dislike/dislikeitem/nostock/smaller/bigger/editqty/qty/choose/save (+ plan,
added by the B13 audit) — no active state or a press on a message other than
the active card's recorded `message_id` is refused
(`validation.failed(entity=recommendation_control, outcome=stale)`); unknown
message ids stay permissive (unprovable mismatch is not staleness). B13's audit
additionally moved `dislikeitem`'s *permanent* preference persistence onto the
displayed-card anchor (it had still resolved from a regeneration) and pinned it
under real rotation divergence.

## 9. Proactive cancellation semantics (B6 `72614c4`)

`deliver_proactive_message` — the single boundary all proactive jobs cross —
suppresses `WORKOUT_PENDING_ASSUMING_KEYS` (`workout_prompt`,
`motivation_pre_workout`) when the day's workout is explicitly cancelled
(`decision.fallback_selected(reason=workout_cancelled_today)`) or already
completed (`workout_already_completed`, a *distinct* reason — cancellation is
never reinterpreted as completion), before any `delivery.attempted` and before
the send-budget claim. Pending delivers unchanged; nutrition prompts are
unaffected.

## 10. Restart/resume architecture (B6)

`load_pending_state` deleted live `deferred_plan` continuation rows as
"legacy" on every restart — the flow silently dead-ended. The wrap preserves
them (true-legacy keys still cleaned) and, mirroring the existing onboarding
restart pattern, offers a one-time resume card on the first menu interaction
after restart, its controls carrying the persisted `active_flow.flow_id` (B2
grammar). Continue re-enters through `advance_after_answer`; stale flow ids are
refused; free-text answers resume without any offer via the persisted
active_flow routing.

## 11. Canonical flow architecture (B7 `6b8e20b`)

`active_flow` owns routing, lifecycle, and continuation identity
(flow_id/version/expiry/suspension snapshots, O7-traced). Interrupt/suspend/
resume is one authoritative lifecycle: the meal-correction microflow suspends
the wizard (flow.suspended), `clear_meal_fix` resumes it with its original
flow_id (flow.resumed) — proven end-to-end in the required interrupt trace.

## 12. Wizard/scratchpad ownership model (B7, B8 `ac5d4c8`)

`conversation_state` rows are scratchpad keyed to canonical flow identity.
Dual-write agreement is no longer a correctness requirement: expiry deletes
flow-scoped scratchpads (goal_wizard, profile_field_edit) in the same step
(`flow.expired` never leaves a live-looking wizard row); late `qa:*` answers
against an orphaned wizard row are refused with a fresh-start offer (wizard
answers carry no version tokens, so the router's stale check cannot protect
them); resumable continuations (deferred_plan, plan_completion) and
health_confirm (own step lifecycle) survive by explicit policy.

## 13. Home/cancel lifecycle semantics (B7, B8)

Resolved product decision enforced: **Home preserves** — meaningful multi-step
goal/plan work is suspended into the idle row's snapshot (explicitly resumable,
free text routes idle → no invisible continuation) with a resume control
carrying the flow_id; trivial flows keep the old exit (their flow-scoped
scratchpad cleared, traced `reason=home_exit`). **Cancel terminates** —
`/cancel` (re-registered in runtime as `terminal_cancel_command`) invalidates
every continuation store with per-store
`state.mutated(outcome=invalidated, reason=user_cancel)`; nothing resumable or
actionable remains (pinned: no restart offer, no orphan rows).

## 14. AssistantTurnContext (B9 `699800a`)

`build_turn_context` produces a bounded, decision-grade context (never a state
dump): active/suspended flow snapshots, pending confirmation entity (kind
recovered from the *displayed* confirm control via render evidence),
recommendation option identities (number/title/fingerprint) + active card
message id + selection, resumable stores, and (B10) the coaching-memory
snapshot. Emits `context.built` per free-text turn.

## 15. Deterministic reference resolver (B9)

High-confidence references resolve **before** the LLM and dispatch through the
same gated callback paths a button press takes (the B2/B5 gates validate the
resolution): "כן"/"לא" → the pending confirmation's proven displayed control;
ordinals → the N-th *displayed* option by fingerprint; "תשמור את זה" → the
selected option pressed on the active card's message id; "תחזור" → the
suspended flow (resume + advance). Each resolution emits
`decision.finalized(entity=reference_resolution, entity_id=<fingerprint/kind/flow_id>)`
so the final domain mutation references the same id. Unresolved turns hand the
bounded candidates to `classify_intent` via a contextvar — the LLM classifies
*with* structured context instead of guessing; without render evidence "כן" is
deliberately left unresolved rather than guessed.

## 16. Coaching-memory taxonomy (B10 `94a52fb`)

Six classes with the resolved persistence contract enforced in
`coaching_memory.py`: one-turn constraints (never persisted — the API refuses),
meal-instance corrections (instance-only, never auto-generalized),
food-identity knowledge (≥2 consistent corrections → automatic *proposal*;
decision-grade only after one explicit confirmation), terminology aliases
(explicit/confirmed-clarification provenance only), persistent preferences
(existing preference service), safety/medical (user-explicit only, never
behavioral inference).

## 17. Coaching-memory provenance policy (B10)

Storage rides the user_facts confirmation machinery (`food_identity:<key>`,
KIND_ESTIMATE until confirmed) with per-observation entity_refs in the value.
Every persistent mutation emits `state.mutated(domain=coaching_memory)` with
memory class, provenance, and confirmation state. The required regression is
green: *"why did the coach assume 250g?"* is answerable machine-readably —
observation_recorded → proposal_created → confirmed chain, plus the analyzer
prompt block carrying the confirmed default-quantity line.

## 18. Fact read policy (B11 `e2045df`)

Decision-grade consumers migrated to `get_decision_value` (personal-target
body metrics; food_environment_context for menu-style decisions);
safety-conservative raw reads kept and documented in place
(diet/allergies/dislikes fail closed); display/draft flows unchanged by design;
the policy is stated at the source (`get_value` docstring). Pinned: an
unconfirmed derived weight cannot silently produce targets; a confirmed one
does; an unconfirmed allergy still restricts.

## 19. Reschedule semantics (B12)

"האימון אחר כך" now collects a **concrete** time: slot controls (+1h/+2h/
typical hour, `wktat:HHMM`) plus free-text input ("19:30", "בעוד שעה"),
normalized and validated (past/unparseable input gets a visible correction and
keeps the question pending; a non-time message releases it — the user is never
trapped). Only a concrete decision persists (`next_meal_workout_expected_at`,
CAS-owned); the canonical resolver turns it into a real user-clarification
candidate with phase derived from the user-stated time, so meal timing and
budget decisions see it everywhere. "לא יודע עדיין" remains a vague later —
pending, never stored as a time. ARCH-13: planned meals carry durable
identity (`plan_id`) and an explicit lifecycle — planned → consumed
(fingerprint match on save, traced with the linked `meal_id`) / expired
(view-level after 6h); the follow-up's substring matching survives only as the
text-log fallback where no fingerprint can exist.

## 20. Trace-based regression strategy

Every batch's contract is pinned by production-path tests that assert the
machine trace, not just return values: the required trace sequences from the
program spec map to tests as — B4 midnight matrix (coaching-row unity), B5/B13
rotation-divergence identity (rejection fingerprints), B6 suppression
(`decision.fallback_selected` + zero `delivery.attempted`) and restart harness
(real `load_pending_state`), B7 interrupt/expiry/Home traces
(flow.suspended/resumed/expired), B8 terminal-cancel invalidation set, B9
resolution traces (`context.built`, `decision.finalized` with entity ids), B10
provenance chain, B12 reschedule persistence trace. Assertions target semantic
identity (fingerprints, flow ids, entity ids) — not list positions or text
coincidence.

## 21. Mini App work explicitly completed (shared-state safety, ARCH-07A)

Proven: the Mini App workout-status endpoint writes through the same
`save_next_meal_workout_status` boundary onto the same canonical coaching-day
row Telegram reads — one row at 00:30, no split, no corruption of
Telegram-visible state (B4). Mini App flag writes ride the same CAS.

## 22. Mini App work explicitly deferred (ARCH-07B)

Deliberately not done, per the binding priorities: conflict UX (silent
refresh-and-retry vs explicit "data changed" messaging — product decision #3
still open), profile/plans revision plumbing beyond the day-token/CAS safety,
and any Mini App client architecture work. The Mini App remains functionally
unchanged beyond shared-state safety.

## 23. Performance assessment

All added seams are O(1) per interaction: attribute-wrap dispatch, one flags
read per gated callback, one active_flow read per turn-context build, a
bounded (limit 100–400) event scan for render evidence. `save_next_meal_option_feedback`
dropped from two generations to one. The full suite runs in the same ~8–13 min
band as the pre-program baseline; no hot-path regressions observed in suite
timing.

## 24. Backward compatibility

Legacy rows behave: planned meals without `plan_id`/`status` read as planned;
flags without revision fields migrate through the CAS; clarifications without
timestamps are treated valid (never guessed stale); pre-migration DBs start
cleanly (06fc1e5, verified on a real backup copy); uninstall functions restore
every wrapped attribute (closure-bound originals — a live wrapper can never
dereference a torn-down global).

## 25. Known limitations

- The Home suspension writes through `set_active_flow(idle, suspend_current=True)`,
  which O7 also records as `flow.completed` alongside the explicit
  `flow.suspended(reason=home_exit)` — a known trace artifact, documented in
  `flow_convergence`.
- The deterministic resolver's vocabulary is intentionally small
  (yes/no/ordinals/save/return); everything else goes to the LLM *with*
  candidates. That is the designed split, not full deterministic NLU.
- `planned_meal` "replaced" transitions are defined in the lifecycle but not
  wired to any user action — no existing control maps to "I did something
  else instead"; wiring it would have invented UX (out of ARCH-13's evidence
  scope).
- Coaching-memory consumers cover meal analysis/reanalysis, next-meal, menu
  generation and the assistant context; deeper consumer integration (e.g.
  workout-side memory) was not in the resolved contract.
- B12's first cut called the undecorated `_render_next_meal_screen` from a
  wrap without syncing its facade globals — a cold-path NameError the B13
  audit caught and fixed (explicit `runtime_bind._sync`) before any deploy;
  noted here because it shows the wrap seams' one sharp edge.
- (Correction of an earlier stale claim:) the two evening-deterministic re8
  test failures identified during the observability program were fixed BEFORE
  this program, in the baseline commit `8233abe` itself
  (`test_quantity_edit_recalculates_option_totals_through_callback` — a real
  product defect in quantity adjustment plus a positional-identity test
  defect; `test_selected_option_quantity_text_recalculates_without_saving` —
  a wall-clock test defect). Both tests are included in the current full
  suite, both pass, nothing is skipped/xfailed/deselected — they are NOT a
  remaining limitation of this program.

## 26. Explicitly unresolved architecture gaps

- ARCH-07B (Mini App conflict UX / client architecture) — deferred by
  instruction (§22).
- Full ARCH-01 phase-3 style store *unification* (single physical continuation
  table) — the program converged **ownership and lifecycle** (active_flow owns,
  scratchpads derive); the stores still live in their existing tables by
  design ("do not yet redesign all wizard stores").
- `_planned_session_candidate`'s vague-later branch still reads
  `WORKOUT_STATUS_UNKNOWN` when the user declines to give a time — correct per
  the resolved contract (a vague later is not a schedule), noted for
  completeness.
- The two audit findings fixed in B13 (§8) are closed; no other P0/P1
  contradiction survived the final sweeps.

## 27. Batch commit list

| Batch | Scope | Commit |
|---|---|---|
| B1 | ARCH-03 daily_flags CAS | `036fa58` |
| — | DB startup regression fix | `06fc1e5` |
| B2 | ARCH-04+05 callback grammar + confirmation gate | `3617341` |
| B3 | ARCH-02 coaching-day authority | `af0e89f` |
| B4 | ARCH-02 midnight adversarial + ARCH-07A | `ec51f0d` |
| B5 | ARCH-06 recommendation identity | `fd0fa84` |
| B6 | ARCH-09+11 suppression + restart resume | `72614c4` |
| B7 | ARCH-01 ph1 goal-wizard convergence | `6b8e20b` |
| B8 | ARCH-01 ph2 + ARCH-10 cancel/Home | `ac5d4c8` |
| B9 | ARCH-08+16 turn context + reference resolution | `699800a` |
| B10 | ARCH-12 coaching memory | `94a52fb` |
| B11 | ARCH-15 fact read policy | `e2045df` |
| B12 | ARCH-13+14 reschedule + planned-meal identity | `af56486` |
| B13 | Final adversarial audit fixes | `13e05f3` |

(The user's parallel task-spec commits 3ab12ad/3a876bf and the staged
`tasks/TASK_61_*.md` swept into `72614c4` are their content, preserved intact.)

## 28. CI/evaluation/build results

No hosted CI is configured for this repository; the local full suite is the
verification gate. Every batch ran the complete suite with **zero
deselections** (counts: 1523 → 1618 across the program, all passing; final run 1618 passed / 0 deselected),
`scripts/run_evaluations.py` 33/33, and `scripts/build_release.py` green (no
forbidden entries) after every batch.

## 29. Protected baseline verification

The 19 fingerprints (17 modified + 2 untracked protected files,
`scratchpad/protected_baseline_df7ac23.txt`) were verified byte-identical after
every batch and one final time after the B13 commit: **PASS** — no protected
file was edited, staged, restored, or deleted at any point; all product fixes
in protected paths were delivered as install-time attribute wraps wired in
`noam_coach/app/runtime.py`.
