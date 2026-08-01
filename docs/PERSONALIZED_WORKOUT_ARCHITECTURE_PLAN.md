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
| `workout.py:454` — a **third** history reader **[V]** | Superseded by A5's full inventory: there are **six** history-reading sites across five functions, not three. Four are exercise-keyed; two (`build_fatigue_assessment`, `reconcile._session_perf_by_day`) key on `session_id` only and are exercise-blind by design |
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
| **A4** | **COMPLETE** | `9d0341d`, `2cc4035` | #64 | proven, exit 0 |
| **A6** | **COMPLETE** | `d07cb9e`, `cb67c9b` | #65 | proven, exit 0 |
| **A5** | **COMPLETE** | `71e0106` | #67 | proven, exit 0 |
| **A11a** | **COMPLETE** | `6acc172` | #69 | proven, exit 0 |
| **A9** | **COMPLETE** | `4345993` | #70 | proven, exit 0 |
| **A7** | **COMPLETE** | `6de87f9` | #71 | proven, exit 0 |
| A8, A10, A11b, A12, A13 | not started | — | — | — |

**A7 correction to this document.** The A7 row said `training_limitations`
"never" expires. It does — `expires_after_days=30`. The defect was the
**mismatch** with the 14-day `medical_constraints` TTL, leaving a 16-day window
where planning asserted a limitation the runtime had already stopped honouring.
Now aligned at 14, the safe direction: an expired safety fact re-asks the
question rather than assuming an answer.

### A9 contracts for A10, A11b and A12

A9 is the single supported way to change a saved plan. The three items that
depend on it consume the same shape rather than inventing their own:

* **`realign_saved_plan_to_weekdays(db, user_id, target_days, *, reason)`** —
  the only entry point today. It never edits a payload: it inserts a new
  version and activates it through `activate_plan`, which A4 authorizes to write
  the governed fact. **A10, A11b and A12 must add operations beside it in the
  same module, never a second writer elsewhere.**
* **`MutationOutcome`** — `outcome` always set; `reason` only for BLOCKED and
  FAILED. `is_failure` is False for `no_change` and `no_plan`, because "already
  correct" and "nothing to change" are successes. Any caller that reports them
  as failure is wrong.
* **Audit** — `("realign_weekdays", "plan")` is registered in
  `_AUDIT_ALLOWLIST`. A new operation needs its **own** entry: unregistered
  pairs fall through to scalar-only and lists are dropped **silently**, so
  weekday-style data must be encoded as short strings.
* **Gates are not optional.** `_validate_plan_for_activation` runs on every
  mutation. Readiness and quality raise the same exception type and are
  separated by token shape (`_looks_like_quality`) — a new quality check whose
  tokens do not match those markers would be misreported as readiness.
* **Sessions are untouched.** `sessions` has no `plan_id` and no FK to
  `plan_versions`, so an in-flight workout keeps its snapshot through a
  supersession. This is a UX/staleness concern, never a data-integrity one.

**A5 correction to this document's own inventory.** Section 3.3 claimed a third
history reader made the total three. A full sweep during A5 found **six**
history-reading sites across five functions. Four are exercise-keyed
(`recommend_load_decision` uses two distinct queries, plus `show_session` and
`previous_weight_context`); two are exercise-blind and stay out of scope.

**A5 finding, deliberately measured rather than fixed.** The session picker does
not exclude `telegram_split_secondary` while the per-session fetch does, so a
split-only session consumes one of three slots and contributes nothing. Fixing
it changes which sessions inform a load — load behaviour, not read plumbing —
so A5 emits `sessions_dropped:<n>` and leaves the decision to a later item.

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

## 3.6 Batch 2 — file ownership and merge order

Batch 2 is A4, A5, A6 and A11a. Calling them "independent" was too loose: their
*dependency graphs* are independent, but their **file surfaces overlap**, and a merge
conflict in a shared file is just as expensive as a logical dependency. Verified at
`1fc4154`:

| Pair | Shared files | Nature |
|---|---|---|
| **A4 ∩ A6** | `noam_coach/bot/onboarding.py`, `noam_coach/bot/ui.py`, `noam_coach/bot/callback_plans.py` | A4 governs the `active_workout_plan` write surface; A6 changes the pre-activation `editparams_menu:` route. Both live in the onboarding/callback layer |
| **A5 ∩ A11a** | `noam_coach/bot/workout.py` | A5 edits the history readers at `:454` and `:625`; A11a adds unmapped-slot guards at `:418`, `:691`, `:778`, `:789`, `:883`, `:950`. Distinct regions of the **same file** |

### Ownership

Exactly one writer per file. Where a file is shared, the **earlier item in merge order
owns it** and the later one rebases onto the result.

| Item | Owns exclusively | Shares (as owner) | Must not touch |
|---|---|---|---|
| **A4** | `tests/test_workout_catalog_architecture_guards.py`, `user_model.py` | `onboarding.py`, `ui.py`, `callback_plans.py` | `workout.py`, `training.py` |
| **A5** | `noam_coach/services/training.py`, new implementations service, `db.py` | `workout.py` | onboarding/callback layer |
| **A6** | `noam_coach/bot/workout_compat.py` | — (rebases onto A4) | `workout.py`, `training.py` |
| **A11a** | `exercise_plans.py`, new reader-helper module | — (rebases onto A5) | onboarding/callback layer, `training.py` |

### Merge order — fixed in advance

```
A4  ──►  A6        (A6 rebases; both in the onboarding/callback layer)
A5  ──►  A11a      (A11a rebases; both in workout.py)
```

The two chains are genuinely parallel: no file appears in both. Within a chain the
order is strict.

**Why this order.** A4 lands first because it *adds a guard* — if the write surface
is governed before A6 changes a route that mints callbacks, A6 gets the guard's
protection for free rather than having to be re-audited afterwards. A5 lands before
A11a because A5 changes query semantics in `workout.py` while A11a only adds
defensive `.get()` guards there; rebasing guards onto changed queries is safe, the
reverse is not.

**Worktrees.** One per item, four total, since all four may start concurrently:
`wt-a4`, `wt-a5`, `wt-a6`, `wt-a11a`. A6 and A11a do not open PRs until their
predecessor is merged and they have rebased — starting early is fine, merging out of
order is not.

**A6 is gated on a harm test.** It is classified `[H]`. If the harm test cannot be
made to fail, A6 closes as documentation and no code is written — in which case A4
owns the shared onboarding/callback files outright.

### Rebase discipline (binding)

**1. Rebase onto `origin/develop` after the merge, never onto the lead item's branch
head.** The canonical state includes the merge commit and any CI change that landed
with it. Rebasing onto a branch head validates against something that was never the
integration state.

**2. A clean rebase is not evidence.** "No conflict" says the text merged, not that
the behaviour survived. After rebasing, every later item re-runs, and reports:

- its own focused tests;
- **the lead item's tests**;
- the architecture guards and the callback-prefix guard;
- **at least one test proving the lead item's change is still active.**

That last one is the point. A rebase can silently revert a semantic change while
leaving both diffs syntactically intact — the only way to know the lead is still doing
its job is to assert it.

**3. Write against the future contract; do not guess the implementation.** A6 and
A11a may start research and tests immediately, but must not locally reproduce the lead
item's logic to move faster. Concretely: A11a may add reader helpers and unmapped-slot
guards, and must **not** copy A5's history-reader logic. A duplicated implementation is
worse than waiting — it creates the second reader this programme exists to remove.

### Risk: A4 → A6 must not be closed by an allowlist entry

"A6 inherits A4's protection" holds **only** if A4 states, explicitly and testably:

- who the permitted owners of an `active_workout_plan` write are;
- which wrappers count as legitimate;
- how the runtime assertion detects an **indirect** call;
- how the protection itself is verified by breaking it on purpose.

Without that, A6 hits the new guard and the cheapest escape is to add itself to the
allowlist — going green while never routing through the canonical boundary. That is
the exact failure the guard exists to prevent, dressed as compliance.

**Therefore A6's acceptance criterion is that its route uses the managed interface —
not that CI is green.** Broadening `_ALLOWED_*` to pass a check is explicitly
forbidden, consistent with the standing rule for the reader guard.

### Risk: A5 → A11a must not turn a contract violation into a silent fallback

A11a adds guards on exercise payload structure. After A5 changes query semantics,
those guards must not convert a *semantic error* into a quiet empty state. If A5
returns history for the wrong implementation, or `None` from a malformed query, A11a
rendering "no history" would hide a real defect behind a plausible screen.

Three states must stay distinguishable, and must not collapse into one branch:

| State | Meaning | Correct behaviour |
|---|---|---|
| **Legacy / legitimately absent** | pre-migration row, no history yet | render the empty state; this is normal |
| **Deliberately unmapped slot** | the slot has no implementation on purpose | render the slot placeholder; not an error |
| **Contract violation** | shape or value that should be impossible | must be **loud** — never rendered as "no history" |

The third case needs an explicit signal (raise, or log-and-flag), never a `.get()`
default that makes it indistinguishable from the first.

## 3.7 Wave 2 pre-flight — A10, A11b, A12 ownership and merge order

The wave diagram in §7 draws A10, A11b and A12 as three parallel children of A9.
**On file surface they are not**, by the same standard §3.6 applied to batch 2:
dependency graphs being independent does not make file surfaces independent, and
a merge conflict in a shared file costs the same as a logical dependency.
Verified at `f039b3f`.

### Pairwise intersections

| Pair | Shared | Severity |
|---|---|---|
| **A10 ∩ A11b** | `onboarding.py`, `plan_mutations.py`, `core.py`, `planning.py` | **Severe — see below** |
| A10 ∩ A12 | `plan_mutations.py`, `core.py` | Low, mechanical |
| A11b ∩ A12 | `callback_session.py`, `plan_mutations.py`, `core.py` | Moderate — identity coupling |

### A10 ∩ A11b is a dependency, not just an overlap

Three layers, each verified:

1. **File adjacency.** `build_weekly_plan` ends at `onboarding.py:2766`;
   `format_weekly_plan` begins at `:2800`. One function apart, same module.
2. **Call-site adjacency.** `assistant.py:597-601` invokes both consecutively on
   the same object, inside `_handle_plan_text_action` — A10's primary edit target.
   A10 is rewriting the function that contains A11b's call site.
3. **Payload contract — the real coupling.** `format_weekly_plan:2848-2849` reads
   `PLANS.get(code)` *because* `build_weekly_plan` produces sessions carrying only
   `weekday/time/code/name` and no exercises. A11b's fix is to render from the
   stored plan instead — **which is only possible once A10 delivers real
   exercises** by routing through the canonical pipeline.

If A11b landed first, its renderer would have to special-case the exercise-less
shape A10 is about to delete: dead code on arrival, and a second rendering branch
in the file this programme exists to unify.

### Chains

```
CHAIN 1 (strict serial)      A10  ──►  A11b
CHAIN 2 (parallel)           A12, merging after A11b
```

**A10 leads** for the same reason A5 led A11a: producer before consumer.
Rebasing a renderer onto a changed producer is safe; the reverse is not. A10 is
also on the critical path (A4 → A9 → A10) and retires A4's temporary
authorization — every day `build_weekly_plan` keeps its escape hatch, the
governance guard has a known live exception.

**A12 runs parallel** with one rule: its two-consecutive-substitution detector
reads an identity A11b redefines. If A12 starts immediately, build the ledger,
migration and transports in parallel — zero overlap — and defer only the
detection predicate until A11b's slot identity is fixed.

### Shared surfaces, and why they are tolerable

**All three** add an operation to `plan_mutations.py` and a key to
`_AUDIT_ALLOWLIST`. Both are additive: new top-level functions, new dict entries.
Conflicts are confined to the `__all__` list and the constants block — mechanical,
unlike the `onboarding.py` adjacency. The `_AUDIT_ALLOWLIST` conflict **fails
safe**: a lost entry falls through to scalar-only rather than leaking data. But
each item must assert its audit row's **contents**, because an unregistered pair
drops list-valued details silently and a lost entry would pass a
row-exists-only test.

**No migration collision** — only A12 needs one, claiming 17 at merge time.

**No callback-prefix collision in this trio** — only A12 mints. The real prefix
contention is A8 ∩ A12, outside this wave.

One caveat for A11b: if it keeps the `sub:` prefix but changes what the second
colon-part means, it silently falls out of `_SESSION_SCOPED_PREFIXES` handling.
The orphan guard checks the prefix only, so it would still pass while the
callback died in the handler. Keep part-2 numeric, or register the prefix as
router-owned.

### Risk and value

**Riskiest: A10.** Not the largest, but the only one carrying a product-judgment
requirement rather than a mechanical one. "Degraded but shippable" versus
"blocked" is a line that does not exist today: `check_plan_readiness` returns a
flat gap list with **no severity dimension at all**, and any non-empty list is a
hard block. A10 must introduce a severity axis into a structure that has none —
and must not repeat A9's `_looks_like_quality` trap, where one exception type
carried two operator meanings separated by fragile token matching.

**Most downstream value: A12.** WAVE 3 is explicitly blocked on it, and its
ledger is generic suppression infrastructure any future proposal type reuses.
A10 unblocks more *user-visible* value today; A12 unblocks more *work*.

## 4. Track A — Workout architecture

| ID | Task | Domain | Depends | Justification | Acceptance criteria |
|---|---|---|---|---|---|
| **A1** | Pain mirror inside the transaction boundary | Runtime | — | The mirror sits after `except _StaleSetStep`, outside the transaction and unwrapped **[V]**. A failure leaves the constraint written, the planning fact stale, and surfaces an error on a report that succeeded | Forced mirror failure leaves both stores consistent; no error surfaced to the user; no second write path introduced |
| **A2** | Occurrence identity on `sets` | Runtime | — | `undo_last_set` reverse-maps `exercise_id` with `next()`, returning the first match **[V]**. `sets` has no `exercise_index` column **[V]** | Plan with the same exercise at index 0 and 3: a set logged at 3 undoes to 3. Split sets share the index; rewind keys off the primary |
| **A3** | Effort CTA on the rest screen | Runtime | — | One-tap logging writes `RIR_UNKNOWN` and never asks, so progression can rarely confirm mastery **[V]**. This is the real fix for sparse RIR — it adds data | Ignoring the CTA changes nothing; tapping updates only a row whose value is `RIR_UNKNOWN`; targeted by set id captured at arm time, never "most recent set" |
| **A4** | Write governance: AST guard + runtime assertion | Governance | — | The guard is a regex blind to writes, and `[^,]+` fails on any call whose first two arguments contain a comma **[V]**. Two ungoverned `set_fact` writers exist | A new writer fails CI; a non-constant key fails CI; `getattr` spelling and raw SQL covered; runtime contextvar assertion complements the static guard. **Must also publish the contract A6 depends on**: the permitted owners of an `active_workout_plan` write, which wrappers are legitimate, how the runtime assertion detects an indirect call, and a test that verifies the protection by breaking it deliberately |
| **A5** | Ladder-ready history reads | Load | — | **Four** exercise-keyed readers, not three **[V]**; no equipment/machine/gym/brand column exists anywhere in the schema **[V]**; a query failure was indistinguishable from "never trained" **[V]** | **DONE** — one `HistorySelection` contract; `no_history` vs `history_unavailable` kept distinct and logged with ids only; layer reported on every decision; guards pin that every exercise-keyed history SELECT keys on `exercise_id` and none on `exercise_name` |
| **A6** | Pre-activation `editparams_menu:` route | Identity | A4 | The plan-review wizard mints the legacy callback before activation **[V]**; with no active plan the user edits template parameters believing they edit their plan. Harm **[H]** | Harm test fails first, then passes. If it cannot be made to fail, the item closes as documentation. **The route must go through the managed interface — a green CI is not sufficient, and adding A6 to any allowlist to satisfy A4's guard is forbidden** |
| **A7** | Pain persistence semantics — **DONE** | Runtime | A1 | `medical_constraints` expires after 14 days; `training_limitations` never does **[V]**, and the mirror concatenates strings — a one-time report becomes a permanent limitation | Four states distinguished (temporary event / active / confirmed / historical); expiry clears the planning fact; non-pain limitations survive; recompute, not append |
| **A8** | Substitution occurrence correctness | Slot | A2 | `alts.index(alt)` is a value-based lookup **[V]**; re-ranking mutates the list, so a stale callback substitutes a valid-but-wrong exercise | A mutated list causes a stale callback to be rejected, never misapplied. Keyed on alternative id plus occurrence identity |
| **A9** | Mutation boundary + saved-plan reconciliation (**W1-44**) — **DONE** | Governance | A4 | An availability correction deliberately leaves the plan contradicting it **[V]**, and the reply reports success with no hint of the divergence | The corrected day is removed after approval; the divergence is named before it; an in-flight session defers the swap; audit rows are recoverable |
| **A10** | Free-text intent + degraded plan | Governance | A9 | Only a frequency integer survives parsing **[V]**; exercise-less plans become active with no readiness gate | Weekdays, time and duration all land; a legacy user below full readiness receives a degraded plan with explicit disclosure and a completion CTA, never silence; safety-critical gaps still block. **Must also retire A4's temporary authorization**: remove `noam_coach/bot/onboarding.py` from `_ALLOWED_FACT_WRITER_FILES`, delete the `authorize_governed_fact_write` block in `build_weekly_plan`, and delete `test_build_weekly_plan_is_marked_a_temporary_owner`. After A10 the writer allowlist contains `planning.py` only, and a reintroduced independent mirror write must fail CI |
| **A11a** | Slot model — read-only | Slot | A5 | Static data, reader helpers and consumer guards; no-ops on current payloads | Every consumer tolerates a slot with no implementation without raising. **A contract violation must stay loud**: legitimately-absent history, a deliberately unmapped slot, and an impossible payload shape must remain three distinguishable states, never one silent empty branch |
| **A11b** | Slot model — minting | Slot | A9 | No identity survives regeneration **[V]**; the weekly plan renders from the global template, so substitutions are invisible **[V]** | A slot survives removal, blocking, substitution and reordering; two slots may share one canonical exercise; repair never silently deletes a slot |
| **A12** | Proposal ledger + two transports | Patterns | ledger: — · write: A9 | No cooldown state exists **[V]**; `job_state` is keyed per-day so an 8-week cooldown is inexpressible **[V]** | A decline suppresses the same subject ≥8 weeks; workout-moment proposals render directly rather than through the deferred pipeline |
| **A13** | Persist the load decision to audit | Observability | A5 (landed) | `LoadRecommendation.to_audit_dict()` exists and is complete, but its **only** consumer is a Mini App debug endpoint **[V]**. Nothing writes it to `audit` or the event stream, so there is no way to reconstruct what load was recommended for a past set — the one question an operator asks when a user disputes a weight | See A13 scope below |

#### A13 — scope, deliberately narrow

Surfaced by A5 and given an owner rather than left as a loose observation.

**In scope**: persist the **existing** `to_audit_dict()` output through the **existing**
mechanism — `services/core.write_audit` or `observability.emit_event`, whichever the
implementer verifies is the better fit at the time. One decision record per recommendation
that a user actually acts on.

**Explicitly out of scope**: any change to how a load is calculated, chosen or displayed.
A13 is a recording change. If a load decision differs before and after A13, the change is
wrong.

**Privacy**: structured fields only — `decision`, `confidence`, `data_completeness`,
bounded `signals` / `missing_context` tokens, `exercise_id`, internal session id. Never the
Hebrew `explanation` prose, never raw set rows, never plan payloads. Note
`_allowlist_audit_details` silently drops list-valued details **[V]**, so `signals` and
`missing_context` must be encoded as bounded scalars or the fields will vanish while the
test still passes.

**Acceptance criteria**:
1. A recommendation acted on produces exactly one audit record with the fields above.
2. A write failure is logged and **does not** break the workout — recording is best-effort,
   the set is not.
3. Duplicate suppression: re-rendering the same step must not produce a second record.
   `ui.py` calls `recommend_load` **per exercise in a render loop** **[V]**, so a naive
   write-on-every-call would emit N records per screen view.
4. A test asserts the Hebrew explanation is absent from what is persisted.
5. Verified by deliberate breakage, per the standing rule.

**Not blocking A11a.**

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
               ┌─────────────┴─────────────┐
               v                           v
        A10 ──► A11b                      A12
   free-text +   slot minting        proposal ledger
   degraded plan                     (merges after A11b)

   NOTE: A10 and A11b are drawn as siblings above by dependency, but they
   are SERIAL by file surface and payload contract -- see §3.7. A11b's
   renderer fix is only possible once A10 delivers real exercises.
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
| Per-machine load history (§18) | Four exercise-keyed readers, no equipment dimension **[V]** | A5 (contract landed; per-machine identity still to come) | Load |
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
