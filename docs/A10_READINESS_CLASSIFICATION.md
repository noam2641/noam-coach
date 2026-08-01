# A10 — readiness gap classification

**Read-only analysis. Verified against `develop` @ `08fe3d1`. No code changed.**

A10 must let a legacy user receive a *degraded* plan instead of silence. That
needs a severity axis. This document establishes one from the code rather than
inventing it, and is the input to the implementation.

**Owner decision, superseding an earlier reading of this document.** A missing
`training_limitations` is **`degraded_safety`**, not a hard blocker: the plan is
built under conservative behaviour **that A10 must create** (§3 requirement 2 —
no existing path produces caution from absence), the unknown stays explicitly
unknown, the missing adaptation is disclosed, and activation requires explicit
confirmation through the A9 boundary. What still blocks is **structural** — an
input without which there is no plan object to degrade. See §3.

---

## 1. Correction to the pre-flight

The wave-2 pre-flight said A10 "must introduce a severity axis into a structure
that has none". **That was wrong.** The axis exists: `questions.Question` carries
`safety`, `plan_impact`, `urgency`, `uncertainty` and `burden`, each 0–5
(`questions.py:52-56`) **[V]**.

What is missing is *propagation*. `check_plan_readiness` reads those questions
and returns a flat `list[str]` of **Hebrew display strings**
(`onboarding.py:2676-2688`) **[V]**, discarding every score. Its only caller then
treats any non-empty list as a hard block (`assistant.py:580-586`) **[V]**.

**But the safety gate has five enforcement points, not one** *(first corrected
after review from "single caller" to four; corrected again **during
implementation**, when a fifth was found by measurement — see below)*:

| Enforcer | Location |
|---|---|
| `check_plan_readiness` | `assistant.py:580` **[V]** |
| `_block_plan_for_pending_safety` | `onboarding.py:696` and `:3077` **[V]** |
| `_require_readiness(…, "safety")` | `planning.py:1253` (candidate build) **[V]** |
| `_require_readiness(…, "safety")` | `planning.py:1466` (activation) **[V]** |
| `_require_readiness(…, "workout")` | `planning.py:1252` and `:1464` **[V]** |

The fifth was invisible to a search for `"safety"`. `training_limitations` is a
member of **both** `READINESS_PROFILES["workout"].required` and
`READINESS_PROFILES["safety"].required` (`user_model.py:713-727`) **[V]**, so the
*workout* gate refuses on the safety fact one line before the safety gate is
reached. With only the four known gates changed, the degraded path was
unreachable while appearing fully implemented — the build was still refused,
just by a different profile.

Measured directly: with every other workout fact answered and
`training_limitations` recorded as `KIND_GAP`, `compute_readiness(…, "workout")`
returns `ready=False missing=['training_limitations']`, and `activate_plan`
raises `PlanningBlockedError(missing=['training_limitations'])` **[V]**.

Resolved by `_require_readiness(…, ignore=_SAFETY_FACTS)` at the two workout
sites, which both run the dedicated safety gate immediately afterwards.
`_SAFETY_FACTS` is pinned equal to the safety profile's `required` set by
`test_the_ignore_set_matches_the_safety_profile_exactly`, so a fact can only be
skipped by the general gate while the safety gate still enforces it.

Relaxing only the first gate would be **silently overridden** by the others: the
free-text path would soften while the two onboarding build paths still hard-block
and re-ask. `_block_plan_for_pending_safety`'s own docstring cites **LOG-015** and
states the gate must block on *every* build path — so softening it is a
deliberate tradeoff against a closed incident, not a detail.

So A10 propagates an existing *scale* rather than designing one — but the change
surface is five gates, not one, and the conservative *behaviour* the degraded
path needs does not yet exist. Smaller than the pre-flight assumed on the
severity axis; larger on both of these.

---

## 2. What `check_plan_readiness` actually checks

Only **two** things **[V]**:

| Gap | Source |
|---|---|
| unanswered safety questions | `questions.pending_safety_questions` |
| `primary_goal` is None | `user_model.get_value` |

`SAFETY_QUESTIONS` contains exactly **one** entry: `training_limitations`
(`questions.py:80-99`) **[V]**.

**But the activation gate requires eight facts.** `READINESS_PROFILES["workout"]`
(`user_model.py:713-720`) **[V]** requires `primary_goal`,
`training_days_per_week`, `training_limitations`, `session_minutes`,
`training_location`, `equipment`, `strength_experience`, `weekly_availability`.

So `check_plan_readiness` is a **narrower** pre-screen than the gate it precedes.
A user can pass it and still be refused at activation — which is one reason the
free-text path produces silence today.

---

## 3. The classification

Derived from three verified properties per fact: its `safety` score, whether the
builder has a **safe default**, and what the plan loses without it.

### `degraded_safety` — a plan is possible, but only under explicit confirmation

**Product decision, superseding the earlier `blocking_safety` classification.**

| Fact | Evidence |
|---|---|
| **`training_limitations`** | `safety=5` (`questions.py:93`) **[V]**, and the only member of `SAFETY_QUESTIONS`. Every other **workout** fact scores `safety=0` |

One other question carries a safety score: `q_allergies` at `safety=3`
(`questions.py:253`) **[V]**. It is deliberately **out of scope** for A10 — it
lives in `PLAN_QUESTIONS`, affects `menu_planning`, and is gated on
`when=lambda ctx: ctx.get("planning_nutrition")`. It never gates a workout plan.
Noted so a later reader does not mistake its absence here for an oversight.

**Why this is not a hard blocker.** Refusing outright is what produces silence
for a legacy user, and silence is not safer — it just moves the failure
somewhere the user cannot see. The safer construction is to build under
conservative assumptions, say plainly what could not be adapted, and require the
user to confirm before anything is activated.

**Four requirements, each load-bearing:**

1. **Preserve an explicit unknown state.** Absent must remain *absent* — never
   coerced to "no limitations". The distinction is already modelled: a deferred
   answer is stored as `KIND_GAP` and `pending_safety_questions` counts a
   `KIND_GAP` fact as unanswered (`questions.py:523`) **[V]**. That is the state
   to preserve, not a new one to invent.
2. **A conservative path must be BUILT — it does not exist.** *(Corrected after
   adversarial review; the original claim here was false and would have made the
   whole design rest on a false premise.)*

   `client_training_profile_from_facts` does handle `limitations is None`
   (`training_intelligence.py:348-359`) **[V]**, but for a legacy user with no
   `active_pain` and no `medical_avoidance` — **exactly the population A10 serves**
   — the fallback produces `""`, which flows to empty tuples. Measured:

   ```
   injuries=()  pain_areas=()  movement_limitations=()  medical_flags=()
   active_pain_regions([]) -> {}
   ```

   `adapt_exercises` therefore receives **no constraints and loads every joint
   freely**. That is not conservative — it is a silent assumption of "no
   limitations", the precise thing requirement 1 forbids.

   The only textual difference from a user who typed "no limitations" is the
   literal `'none'` they supplied; behaviourally the plans are identical.

   **Consequence for A10:** it must introduce a real conservative path — a plan
   built under *unknown* limitations must differ from one built under *known-none*.
   This is a genuine NEW mechanism, and under §2a it requires recorded evidence:
   the evidence is the measurement above, showing no existing path produces
   caution from absence.
3. **Disclose the missing adaptation.** The plan must state that no
   injury/limitation information was available and that exercises were **not**
   adapted for pain. Generic "some info is missing" is insufficient: the user has
   to know which adaptation is absent to judge the risk.
4. **Require explicit confirmation before activation**, through the A9 boundary.
   The plan is generated and shown; it becomes active only on a deliberate tap.

**Why confirmation rather than silent activation.** §2.3 of the specification
requires no permanent change without approval, and this is the one gap where the
cost of a wrong assumption is physical. Confirmation is also what makes the
unknown state honest — the user is told what is missing *and* chooses anyway,
rather than being handed a plan that silently assumed they are uninjured.

- **Message**: the plan, plus an explicit line that no limitation information was
  available and exercises were not adapted for pain.
- **CTA**: two actions — answer the safety question now (upgrades the plan), or
  confirm and activate as-is.
- **Audit**: `outcome=degraded_safety_pending_confirmation`, then
  `reason=safety_unknown_confirmed` on activation. Never the answer itself, and
  never a medical detail.

**What still blocks.** A *stated* limitation that cannot be honoured is a
different matter and is not covered here: that is a plan-quality defect, not a
readiness gap. See §4.

### `blocking_integrity` — the plan would be structurally invalid

| Fact | Evidence |
|---|---|
| **`weekly_availability`** | `_validate_plan_for_activation` rejects any session missing `time` or `minutes`, raising with `missing=["weekly_availability"]` (`planning.py:1471-1479`) **[V]** |
| **`training_days_per_week`** | Required by `READINESS_PROFILES["workout"]` (`user_model.py:717`) **[V]**, so `_require_readiness` refuses activation without it — measured below |

**Citation corrected after review, conclusion re-verified and upheld.** An
earlier draft cited `planning.py:167` as proof of "no default". That line is
inside `build_goal_proposal` — the **nutrition** path **[V]** — and is irrelevant
to building a workout. The reviewer was right about the citation.

The conclusion nonetheless holds, for a different and stronger reason. The
workout builder does *not* read the fact: it derives frequency from
`resolve_availability`, which defaults to `_DEFAULT_DAYS_PER_WEEK = 3`
(`availability.py:60`) **[V]**. Measured with no fact present:

```
resolve_availability(...).max_days_per_week -> 3
```

So a plan *object* can be built. But the fact is in the workout profile's
`required` tuple **[V]**, and readiness measured on a user with no facts returns:

```
ready   : False
missing : [... 'training_days_per_week' ...]
```

`_require_readiness("workout")` therefore refuses at `planning.py:1252` and again
at `:1464` **[V]**. Building a plan that activation is certain to reject is not a
degraded outcome — it is a wasted one, and the user is told nothing either way.

**Classification: `blocking_integrity` is upheld.** Not because no plan can be
constructed, but because no plan can be *activated*. If A10 later chooses to
relax the readiness profile itself, this becomes degradable — that is a separate
decision with its own evidence, not an artefact of this classification.

**Why they block.** Not a safety matter — a structural one. Without them there is
no plan *object* to produce, so "degraded" has nothing to degrade. Activation
would refuse the result anyway, so blocking early is the honest answer rather
than building something certain to be rejected.

- **Message**: name the specific missing input, not a generic failure.
- **CTA**: the question that fills it.
- **Audit**: `outcome=blocked`, `reason=structural_gap`.

### `degraded_personalization` — a plan is possible, and says what it lacks

| Fact | Safe default | Evidence |
|---|---|---|
| `session_minutes` | `50` | `planning.py:986` **[V]** |
| `strength_experience` | `"beginner"` | `planning.py:1013` **[V]** — the conservative direction |
| `equipment` | `{"bodyweight"}` | `normalize_equipment` falls back when nothing matches (`training_intelligence.py:406-407`) **[V]** |
| `training_location` | folded into equipment | same function, same fallback **[V]** |
| `primary_goal` | `"fat_loss_muscle_retention"` | `planning.py:165` **[V]** |

**Why they degrade rather than block.** Each already has a default *in the
builder*, chosen conservatively — beginner progression, bodyweight-only
equipment. The plan is real and safe; it is merely less personalised. Blocking on
these is what produces silence for a legacy user today, which is the defect A10
exists to fix.

**The disclosure is the point.** A degraded plan must say which capabilities were
not applied. A plan that silently assumes bodyweight-only for a user with a full
gym is worse than one that says so and offers to fix it.

- **Message**: the plan, plus a plain line naming what was assumed.
- **CTA**: complete the missing answers to upgrade the plan.
- **Audit**: `outcome=degraded`, `reason=personalization_gaps`,
  `gap_count=<int>`.

### `informational` — never affects activation

`workout_window`, `training_preferences`, `performance_goal` — the `optional`
tuple of the workout profile (`user_model.py:721`) **[V]**. Absent, nothing
changes about whether or how a plan is built.

- **Message**: none at plan time.
- **CTA**: none.
- **Audit**: not recorded as a gap.

---

## 4. Plan-quality defects are a separate axis

Kept deliberately distinct, because A9 already proved the cost of conflating
them. `_validate_plan_for_activation` raises the **same exception type** for a
missing readiness fact and for a plan-quality defect, and A9 had to separate them
by token shape after the mislabelling surfaced in testing
(`plan_mutations.py::_looks_like_quality`) **[V]**.

| Axis | Means | Fix |
|---|---|---|
| **readiness gap** | the user still owes an answer | ask the question |
| **quality defect** | we built something unusable | rebuild or repair |

A10 must not route a quality defect into the degraded path. A plan failing
`workout_quality_issues` is not "less personalised" — it is wrong, and shipping
it with a disclosure would be worse than refusing.

---

## 5. Summary

| Class | Facts | Degradable | Activation |
|---|---|---|---|
| `degraded_safety` | `training_limitations` | Yes, under disclosure | **Explicit confirmation required** |
| `blocking_integrity` | `weekly_availability`, `training_days_per_week` | **No** | Blocked |
| `degraded_personalization` | `session_minutes`, `strength_experience`, `equipment`, `training_location`, `primary_goal` | Yes, with disclosure | Normal |
| `informational` | `workout_window`, `training_preferences`, `performance_goal` | N/A | Normal |

**No fact is a hard safety blocker.** Two are structurally blocking — without
them there is no plan object to degrade. The remaining six already have
conservative handling in the builder. `KIND_GAP` already models "asked and not
answered" **[V]**, so the unknown *state* needs no invention — but the
conservative *behaviour* does: see §3 requirement 2. A10 reuses the state and
builds the caution.

### Reuse assessment

```
Requirement:                 severity axis; unknown state; conservative fallback
Existing mechanisms searched: questions.Question scores; KIND_GAP; READINESS_PROFILES;
                              client_training_profile_from_facts; plan_mutations (A9)
Found:                        Question.safety/plan_impact/... (questions.py:52-56)
                              KIND_GAP as unanswered (questions.py:523)
                              limitations-is-None fallback (training_intelligence.py:349)
                              builder defaults (planning.py:165, :986, :1013)
                              equipment fallback (training_intelligence.py:406)
                              A9 boundary for the write (plan_mutations.py)
Traced:                       check_plan_readiness -> assistant.py:580 (sole caller);
                              READINESS_PROFILES["workout"] requires 8 facts vs the
                              pre-screen's 2; _validate_plan_for_activation enforces both
Decision:                     EXTEND -- propagate the existing scores; reuse KIND_GAP;
                              reuse the existing conservative paths; write via A9
Evidence for NEW:             SAFETY_UNKNOWN only. Measured: with no training_limitations,
                              no active_pain and no medical_avoidance,
                              client_training_profile_from_facts yields injuries=(),
                              pain_areas=(), movement_limitations=(), medical_flags=() --
                              byte-identical to a user who answered "none". No existing
                              path produces caution from absence, so the distinction had
                              to be created rather than reused.
Superseded paths retired:     none. (Corrected: the plan said A10 would retire
                              build_weekly_plan's independent fact write and drop
                              onboarding.py from _ALLOWED_FACT_WRITER_FILES. It did NOT.
                              That write is the plan-BUILDING mirror, untouched by the
                              degraded-safety flow; retiring it is A11b's scope, where
                              build_weekly_plan is already being rewritten. Claiming it
                              here would have been a retirement recorded but not made.)
Post-implementation search:   done. Confirmed on the final diff:
                              - no new set_fact / authorize_governed_fact_write /
                                INSERT INTO plan_versions -- activation goes through A9;
                              - no new table, approval type or idempotency rule --
                                decide_approval's WHERE status='pending' is the single
                                claim, and the post-claim re-read was REMOVED rather than
                                kept as a second divergent rule;
                              - no new logger or audit mechanism -- LOGGER and write_audit
                                reused; planning.py borrows plan_readiness.LOGGER rather
                                than declaring its own;
                              - _ALLOWED_FACT_WRITER_FILES unchanged (empty diff);
                              - KIND_GAP reused to separate "never asked" from "asked and
                                deferred" -- the distinction already written by record_gap
                                and discarded by pending_safety_questions' single bucket.
```

**One mechanism was extended rather than reused as-is.** `_require_readiness`
gained an `ignore=` parameter instead of A10 adding a parallel gate beside it.
That keeps one refusal path, one message and one `missing` payload; a second
gate would have been a duplicate of the thing it was meant to soften.

### Two protections were declared before they were wired

Both were caught by asking "which runtime code reads this?" rather than by a
failing test — the tests passed in both cases, because each asserted the
mechanism in isolation rather than at the point where the user is affected.

**Requirement 1 (conservative behaviour).** `SAFETY_UNKNOWN`,
`is_safety_unknown` and `conservative_limitations_value` were defined, exported
and tested, and three source comments described plans being "built with
`SAFETY_UNKNOWN`". A repository-wide search found **no runtime consumer** — the
constant was inert, and `client_training_profile_from_facts` still collapsed
unknown into known-none exactly as measured. Fixed by adding
`ClientTrainingProfile.safety_unknown`, set from `limitations is None` and
cleared when the existing `active_pain` / `medical_avoidance` fallback yields a
real signal, then added to `public_payload` (an explicit allowlist, so a new
field is otherwise dropped and the *stored* plan keeps the same ambiguity at
rest). Verified: `{}` → `safety_unknown=True injuries=()`;
`training_limitations="none"` → `False`; `active_pain="knee"` → `False`.

**Requirement 2 (disclosure).** Softening the gates made three plan-render paths
reachable with limitations unknown, and all three rendered `format_weekly_plan`
unchanged — a degraded plan would have looked identical to an adapted one. Fixed
by `onboarding._with_safety_disclosure` (shared by both onboarding paths) and the
equivalent prefix in `assistant.py`, plus an `ast`-based guard asserting every
`format_weekly_plan` call site routes through the disclosure, so a *fourth* build
path added later cannot silently skip it.

Both are covered by deliberate breakage: reverting the profile flag, dropping it
from the payload, and removing the disclosure at either call site each fail.

### Implementation constraints carried forward

* Route the write through **A9's `plan_mutations`** boundary — no new writer.
* Do **not** expand `_ALLOWED_FACT_WRITER_FILES`. A10 *removes*
  `onboarding.py` from it once `build_weekly_plan` stops writing the fact.
* Reason codes are bounded and privacy-safe: they name a **class**, never the
  user's answer, and never a medical detail.
* Assert audit **contents**, not merely that a row exists — an unregistered
  `(action, entity)` pair drops list-valued details silently.
