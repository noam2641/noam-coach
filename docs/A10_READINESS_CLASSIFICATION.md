# A10 — readiness gap classification

**Read-only analysis. Verified against `develop` @ `08fe3d1`. No code changed.**

A10 must let a legacy user receive a *degraded* plan instead of silence. That
needs a severity axis. This document establishes one from the code rather than
inventing it, and is the input to the implementation.

**Owner decision, superseding an earlier reading of this document.** A missing
`training_limitations` is **`degraded_safety`**, not a hard blocker: the plan is
built under existing conservative behaviour, the unknown stays explicitly
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
(`onboarding.py:2676-2688`) **[V]**, discarding every score. Its single caller
then treats any non-empty list as a hard block (`assistant.py:580-586`) **[V]**.

So A10 propagates an existing scale rather than designing a new one. That is a
materially smaller and safer change than the pre-flight assumed.

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
2. **Use the existing conservative behaviour.** `client_training_profile_from_facts`
   already handles `limitations is None` (`training_intelligence.py:349`) **[V]**,
   and `adapt_exercises` has no pain regions to work with, so nothing is
   prescribed *because* a limitation was assumed away. A10 adds no new
   conservative path — it relies on the one that exists.
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
| **`training_days_per_week`** | No default at `planning.py:167` **[V]**; without it there is no session count to build |

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
conservative handling in the builder, which is why a degraded plan is achievable
**without inventing a single new default or a new unknown state**: `KIND_GAP`
already models "asked and not answered", and `limitations is None` already falls
back rather than assuming "none".

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
Evidence for NEW:             none required; no new mechanism proposed
Superseded paths retired:     build_weekly_plan's independent fact write (A10 removes it
                              and drops onboarding.py from _ALLOWED_FACT_WRITER_FILES)
Post-implementation search:   pending
```

### Implementation constraints carried forward

* Route the write through **A9's `plan_mutations`** boundary — no new writer.
* Do **not** expand `_ALLOWED_FACT_WRITER_FILES`. A10 *removes*
  `onboarding.py` from it once `build_weekly_plan` stops writing the fact.
* Reason codes are bounded and privacy-safe: they name a **class**, never the
  user's answer, and never a medical detail.
* Assert audit **contents**, not merely that a row exists — an unregistered
  `(action, entity)` pair drops list-valued details silently.
