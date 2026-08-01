# A10 — readiness gap classification

**Read-only analysis. Verified against `develop` @ `08fe3d1`. No code changed.**

A10 must let a legacy user receive a *degraded* plan instead of silence, while
safety-critical gaps still block. That needs a severity axis. This document
establishes one from the code rather than inventing it, and is the input to the
implementation.

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

### `blocking_safety` — must never degrade

| Fact | Evidence |
|---|---|
| **`training_limitations`** | `safety=5` (`questions.py:93`) **[V]**, and the only member of `SAFETY_QUESTIONS`. Every other **workout** fact scores `safety=0` |

One other question carries a safety score: `q_allergies` at `safety=3`
(`questions.py:253`) **[V]**. It is deliberately **out of scope** for A10 — it
lives in `PLAN_QUESTIONS`, affects `menu_planning`, and is gated on
`when=lambda ctx: ctx.get("planning_nutrition")`. It never gates a workout plan.
Noted so a later reader does not mistake its absence here for an oversight.

**Why it blocks.** It is the sole input to pain-aware exercise selection. Absent,
the builder cannot know a movement is contraindicated, and would prescribe it.
`_validate_plan_for_activation` already requires the `safety` profile
(`planning.py:1466`) **[V]**, so this is enforced twice.

**A safe degraded plan is not possible.** There is no conservative default for
"does this person have an injury" — assuming "none" is precisely the unsafe
assumption. A7 aligned this fact's TTL to 14 days for the same reason: an
expired safety answer re-asks rather than assumes.

- **Message**: state that the safety question must be answered before any plan,
  and why — one sentence, no list of everything else missing.
- **CTA**: the safety question itself, answerable inline.
- **Audit**: `outcome=blocked`, `reason=safety_gap_unanswered`.

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

| Class | Facts | Degradable |
|---|---|---|
| `blocking_safety` | `training_limitations` | **No** |
| `blocking_integrity` | `weekly_availability`, `training_days_per_week` | **No** |
| `degraded_personalization` | `session_minutes`, `strength_experience`, `equipment`, `training_location`, `primary_goal` | Yes, with disclosure |
| `informational` | `workout_window`, `training_preferences`, `performance_goal` | N/A |

**Only one fact is safety-blocking.** Two more are structurally blocking. The
remaining five already have conservative defaults in the builder — which is why a
degraded plan is achievable without inventing a single new default.

### Implementation constraints carried forward

* Route the write through **A9's `plan_mutations`** boundary — no new writer.
* Do **not** expand `_ALLOWED_FACT_WRITER_FILES`. A10 *removes*
  `onboarding.py` from it once `build_weekly_plan` stops writing the fact.
* Reason codes are bounded and privacy-safe: they name a **class**, never the
  user's answer, and never a medical detail.
* Assert audit **contents**, not merely that a row exists — an unregistered
  `(action, entity)` pair drops list-valued details silently.
