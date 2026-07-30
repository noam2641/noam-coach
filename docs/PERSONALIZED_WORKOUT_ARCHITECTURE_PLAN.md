# Personalized Workout Architecture — Authoritative Implementation Plan

**Registered 2026-07-29. Baseline `develop` @ `0c0724d`.**

This is the single authoritative plan for the personalized workout programme. It supersedes
three architecture drafts held outside the repository (deleted on registration) and reconciles
`docs/WAVE1_WORK_PLAN.md`, whose status markers were stale.

Evidence classification used throughout:

| Label | Meaning |
|---|---|
| **[V]** | Directly verified against code at `0c0724d` |
| **[I]** | Inference supported by verified code |
| **[H]** | Hypothesis requiring a failing or harm-demonstrating test before work begins |
| **[D]** | Explicit product or architecture decision by the owner |

**No claim in this document inherits `[V]` from an earlier draft.** Every load-bearing claim was
re-verified during the final consolidation pass.

---

## 1. Product direction

Separate **what the user needs to train** (a professional slot) from **how they perform it** (a
personal implementation on a specific machine). The slot is stable; the exercise implementing it
may change without destroying the training intent, the plan structure, or the load history.

The programme also repairs the runtime and governance defects that would otherwise be inherited
by that model.

---

## 2. Owner decisions in force

| # | Decision |
|---|---|
| **D-1** | Free text keeps its conversational entry but is **not** an independent plan-creation path. Extract all usable intent, route into the canonical pipeline, derive the compatibility fact from the authoritative plan |
| **D-2** | `slot_id` represents a stable professional need. Removal, blocking or substitution never deletes a slot. Seven states defined, three shipped. Duplicate detection keys on **slot identity**, never on `exercise_id`. A slot without an implementation is a core-model state every surface must render |
| **D-3** | Machine identity is offered through a **non-blocking secondary CTA** after a significant weight deviation. The workout never waits for an answer |
| **D-4** | Two calibration routes (declared capability, or observed performance). **A real set always counts** — the CTA must never become a hidden requirement. Four-tier recommendation ladder |
| **D-5** | Pattern promotion asks after **two consecutive** occurrences, at the end of the workout, with an 8-week cooldown on decline |
| **D-6** | One shared pattern-detection and cooldown mechanism, with three consumers |
| **D-7** | Pain influences **training only** — never nutrition, sleep, or proactive messaging |
| **D-8** | `compute_fact_rev` remains a Tier-2 revision. Slot staleness is validated in **Tier-1 callback identity**. *(Revokes an earlier decision to add `slot_id` to the fingerprint.)* |
| **D-9** | `_require_readiness` is not a universal binary gate. Minimum-safe inputs are separated from full-personalization readiness; degraded plans are permitted with explicit disclosure of what could not be applied. *(Revokes an earlier position.)* |

---

## 3. Reconciliation ledger

Recorded so the reasoning survives, and so no future session re-derives disproven work.

### 3.1 Claims investigated and **disproven** — deleted from the backlog

| Claim | Why it is false |
|---|---|
| `avg_rir = 3.0` biases load upward | It feeds `build_fatigue_assessment` → a display banner. The load path already requires `_rir_known(...) and rir >= 2` on every set to increase **[V]**. `tests/test_fatigue_wiring.py::test_unknown_rir_not_treated_as_hard` pins the opposite intent |
| Mid-workout pain never reaches `training_limitations` | The mirror exists at `callback_session.py:840-862` **[V]**. Scoping this would have shipped a duplicate write |
| Two override readers silently corrupt live users' weights | They serve **disjoint populations**: `get_user_plan` is reachable only via `template_fallback`, which requires no Tier-1 plan **and** no Tier-2 fact **[V]**. For that population the template *is* the plan, so both keying rules are provably identical. Downgraded to **[H]**, scoped to the plan-loss edge case |
| Proposals can ride `deliver_proactive_message` | `claim_job_delivery` defers non-urgent messages while a flow is active **[V]** — every workout-surface proposal would be silently dropped |

### 3.2 Work withdrawn

Override backfill migration; the quarantine question it created; the `exercise_overrides` PK
change to `(user_id, code, exercise_id, field)`; a discovery gate that did not measure what was
claimed; and re-pinning three tests that stay green unmodified.

Rationale for the backfill withdrawal: with no session at backfill time, the verification rule
degenerates to "assign the template's id at that index" **[V]** — laundering a positional guess
into permanent identity and destroying the information `NULL` currently carries.

### 3.3 Readers, routes and tests discovered late

| Item | Consequence |
|---|---|
| `workout.py:454` — a **third** history reader **[V]** | The machine-aware ladder must apply at three sites, not two |
| `workout.py:625` `previous_weight_context` **[V]** | Second reader; drives the user-facing "previous weight" line |
| `onboarding.py:2278` mints `editparams_menu:` pre-activation **[V]** | The genuine route into the override edge case |
| `_allowlist_audit_details` in `services/core.py` **[V]** | Silently drops list-valued audit details |
| `tests/test_coach_bot_logic.py`, `tests/regression/test_recording_20260628_re8.py` **[V]** | Exercise `get_user_plan`; absent from earlier drafts |

### 3.4 Status corrections to existing documents

- **W1-7 and W1-8** are marked `[REPRODUCED]` but both fixes are live in code **[V]**.
- **`docs/WORK_MANAGER_STATE.md`** names W1-7 as the active task and lists W1-7/W1-8 as
  outstanding. Without correction, the next session resumes completed work.
- **W1-10's defect description is factually wrong**: it states `workout.py:868` raises
  `StopIteration`. It is caught at `:871` **[V]**. The real residual defect is that the fallback
  uses the live pointer, and that `next()` returns the **first** match — so a plan containing the
  same exercise twice rewinds to the wrong occurrence.

---

## 3.5 Delivery status

| ID | Status | Commit | PR | Reachability |
|---|---|---|---|---|
| **A1** | **COMPLETE** | `0691da3` | #59 | proven, exit 0 |
| **A2** | **COMPLETE** | `09e06d9` | #60 | proven, exit 0 |
| **A3** | **COMPLETE** | `295bb9f`, `2bd99da` | #61 | proven, exit 0 |
| A4–A12 | not started | — | — | — |

Two findings from delivering batch 1, recorded because they change how later items
should be approached:

1. **The callback-orphan guard is real and local runs miss it.**
   `tests/regression/test_re10_regression.py::test_no_orphan_callback_prefixes` scans `bot/`
   for every literal `callback_data` prefix and fails on any the router does not own. A3
   minted `seteffort` without declaring it and CI caught it in both contexts. Any later item
   that mints a new callback prefix must register it in the same pass — **A8 and A12 both
   will**.

2. **A guard test can pass for the wrong reason.** A3's isolation test initially passed
   because the router already resolves the session with `AND user_id=?` before any handler
   runs — so the in-query ownership scope was never exercised. The test was rewritten to
   target the one client-supplied value the router does *not* validate (the set id), and
   only then did removing the scope fail it. **Every guard added from here must be verified
   by breaking the thing it guards**, not merely by passing.

## 4. Track A — Workout architecture

| ID | Task | Domain | Depends | Justification | Acceptance criteria |
|---|---|---|---|---|---|
| **A1** | Pain mirror inside the transaction boundary | Runtime | — | The mirror sits after `except _StaleSetStep`, outside the transaction and unwrapped **[V]**. A failure leaves the constraint written, the planning fact stale, and surfaces an error on a report that succeeded | Forced mirror failure leaves both stores consistent; no error surfaced to the user; no second write path introduced |
| **A2** | Occurrence identity on `sets` | Runtime | — | `undo_last_set` reverse-maps `exercise_id` with `next()`, returning the first match **[V]**. `sets` has no `exercise_index` column **[V]** | Plan with the same exercise at index 0 and 3: a set logged at 3 undoes to 3. Split sets share the index; rewind keys off the primary |
| **A3** | Effort CTA on the rest screen | Runtime | — | One-tap logging writes `RIR_UNKNOWN` and never asks, so progression can rarely confirm mastery **[V]**. This is the real fix for sparse RIR — it adds data | Ignoring the CTA changes nothing; tapping updates only a row whose value is `RIR_UNKNOWN`; targeted by set id captured at arm time, never "most recent set" |
| **A4** | Write governance: AST guard + runtime assertion | Governance | — | The guard is a regex blind to writes, and `[^,]+` fails on any call whose first two arguments contain a comma **[V]**. Two ungoverned `set_fact` writers exist | A new writer fails CI; a non-constant key fails CI; `getattr` spelling and raw SQL covered; runtime contextvar assertion complements the static guard |
| **A5** | Ladder-ready history reads (three sites) | Load | — | Three readers key on canonical `exercise_id` **[V]**; no equipment, machine, gym, brand or model column exists anywhere in the schema **[V]** | All three surfaces agree; none blends machines; an unattributed set still counts via the canonical tier |
| **A6** | Pre-activation `editparams_menu:` route | Identity | — | The plan-review wizard mints the legacy callback before activation **[V]**; with no active plan the user edits template parameters believing they edit their plan. Harm **[H]** | Harm test fails first, then passes. If it cannot be made to fail, the item closes as documentation |
| **A7** | Pain persistence semantics | Runtime | A1 | `medical_constraints` expires after 14 days; `training_limitations` never does **[V]**, and the mirror concatenates strings — a one-time report becomes a permanent limitation | Four states distinguished (temporary event / active / confirmed / historical); expiry clears the planning fact; non-pain limitations survive; recompute, not append |
| **A8** | Substitution occurrence correctness | Slot | A2 | `alts.index(alt)` is a value-based lookup **[V]**; re-ranking mutates the list, so a stale callback substitutes a valid-but-wrong exercise | A mutated list causes a stale callback to be rejected, never misapplied. Keyed on alternative id plus occurrence identity |
| **A9** | Mutation boundary + saved-plan reconciliation (**W1-44**) | Governance | A4 | An availability correction deliberately leaves the plan contradicting it **[V]**, and the reply reports success with no hint of the divergence | The corrected day is removed after approval; the divergence is named before it; an in-flight session defers the swap; audit rows are recoverable |
| **A10** | Free-text intent + degraded plan | Governance | A9 | Only a frequency integer survives parsing **[V]**; exercise-less plans become active with no readiness gate | Weekdays, time and duration all land; a legacy user below full readiness receives a degraded plan with explicit disclosure and a completion CTA, never silence; safety-critical gaps still block |
| **A11a** | Slot model — read-only | Slot | — | Static data, reader helpers and consumer guards; no-ops on current payloads | Every consumer tolerates a slot with no implementation without raising |
| **A11b** | Slot model — minting | Slot | A9 | No identity survives regeneration **[V]**; the weekly plan renders from the global template, so substitutions are invisible **[V]** | A slot survives removal, blocking, substitution and reordering; two slots may share one canonical exercise; repair never silently deletes a slot |
| **A12** | Proposal ledger + two transports | Patterns | ledger: — · write: A9 | No cooldown state exists **[V]**; `job_state` is keyed per-day so an 8-week cooldown is inexpressible **[V]** | A decline suppresses the same subject ≥8 weeks; workout-moment proposals render directly rather than through the deferred pipeline |

### Deferred, with cause

Time and meal habit consumers — requires A12 **and** closes a blocking gap:
`learn_workout_pattern` learns from HealthKit workouts only and never reads `sessions` **[V]**,
so a bot-only user has no baseline to detect drift against. Image recognition. Mini App mapping
screens — it renders workout titles only today **[V]**. Document program import.

---

## 5. Track B — Surviving WAVE-1 backlog

| Lane | Open items | Note |
|---|---|---|
| **C — Observability** | W1-11, W1-13, W1-14, W1-15, W1-16 | W1-17 done (#42). **W1-14** and **W1-16** directly serve A9's audit requirement — sequence adjacent |
| **A — Nutrition** | W1-18, W1-20, W1-21, W1-22, W1-23 | W1-19 done (#40). Independent of Track A |
| **D — Workout** | *(none)* | W1-10 dissolved into A2 + A8 |

Completed and removed from the active backlog: W1-7, W1-8, W1-17, W1-19.

---

## 6. Implementation order

```
WAVE 1 — no prerequisites, parallel-safe
  A1   pain transaction boundary
  A2   occurrence identity        ──┐
  A3   effort CTA                   │   <- first user value
  A4   write governance    ──┐      │
  A5   ladder reads (3)      │      │
  A6   pre-activation route  │      │
  A11a slot read-only        │      │
                             v      v
WAVE 2                      A9 ──> A8
                     mutation boundary
                         + W1-44
                             │
               ┌─────────────┼─────────────┐
               v             v             v
              A10          A11b           A12
         free-text +    slot minting   proposal ledger
         degraded plan
  A7 pain semantics ── requires A1

WAVE 3 — deferred (needs A12 + sessions as a learning source)
  time and meal habit consumers
```

**Critical path:** A4 → A9 → A10.

**First user value:** A3 — additive, non-blocking by construction, and it begins accumulating the
RIR data every later analysis depends on.

**First implementation batch:** A1 + A2 + A3. All independent, all runtime-safety, one additive
nullable column between them, no callback-compatibility impact, each rolling back alone.

---

## 7. Cross-domain items still open

| # | Item | Resolution path |
|---|---|---|
| X-1 | If a slot's implementation changes, its overrides do not follow | Defer until after A11b. Arguably correct — weight belongs to the exercise **[H]** |
| X-2 | Migration numbers for A2, A5 and A12 | Claim at merge time, never pre-assign **[D]** |
| X-3 | `training_limitations` may hold non-pain limitations written by onboarding | One writer inventory before A7; the recompute must preserve the non-pain component **[H]** |
| X-4 | Seven slot states, three with no identified producer | Ship `mapped` / `unmapped` / `blocked`; reserve the rest as accepted-but-unhandled **[D]** |

---

## 8. Traceability

| Product requirement | Verified gap | Work item | Domain |
|---|---|---|---|
| Plan must reflect stated availability (§28.1) | `health_jobs.py:1906-1916` divergence **[V]** | A9 (W1-44) | Governance |
| Per-machine load history (§18) | Three readers, no equipment dimension **[V]** | A5 | Load |
| Substitution scope choice (§19.2) | Session-only; `original_id` never read **[V]** | A8, A12 | Slot / Patterns |
| Slot survives implementation change (§3) | Identity is `(plan_id, session_index)` **[V]** | A11a, A11b | Slot |
| AI may not activate a plan from text alone (§25) | `assistant.py:568` → exercise-less fact **[V]** | A10 | Governance |
| Pain must not become permanent (§20) | TTL asymmetry **[V]** | A7 | Runtime |
| Never block the workout with questions (§2.2) | One-tap path never asks **[V]** | A3 | Runtime |
| Decline must be respected (§2.1) | No cooldown state exists **[V]** | A12 | Patterns |

Requirements already satisfied by existing code, for which **no work item exists**: unknown-RIR
preservation (§32.12), duplicate-set protection (§32.13, four independent guards), Health never
fabricating strength records (§25), joint-load pain filtering (45/45 alternatives carry
`joint_load`), and fail-closed alternative selection under pain.
