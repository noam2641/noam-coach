# TASK 61 - Pain-Aware Exercise Substitution and Load Adjustment

## Status

Open product/implementation task.

## Source Incident

The user declared a limitation consistent with tennis elbow. During workout
plan approval, the generated plan appears to have replaced or biased multiple
training days toward squat / lower-body exercises:

- day 1: full-body workout includes leg press and lower-body work
- day 2: chest/front-arm workout includes squat
- day 3: back/rear-arm workout includes squat
- day 4: shoulders/legs includes squat and lateral raises

This likely happened because elbow-sensitive upper-body work was removed or
over-penalized. However, avoiding elbow aggravation is not a valid reason to
fill unrelated training days with exercises that no longer match the planned
training intent.

## Problem

The current pain-aware exercise adaptation appears to treat a limitation such as
tennis elbow as a broad exclusion rule. That can produce two harmful outcomes:

1. Relevant exercises are removed too aggressively.
2. Replacement exercises may preserve "safe" status but lose the intended
   movement pattern, muscle-group target, or workout identity.

For example:

- a chest/front-arm day should not become a squat day simply because some
  pressing or curling variations may irritate the elbow
- a back/rear-arm day should not be filled with generic lower-body work unless
  the user explicitly asked for a lower-body substitution
- replacing every elbow-sensitive movement with squats hides the actual
  training limitation instead of coaching around it

The system should distinguish between:

- exercises that are contraindicated and should be replaced
- exercises that may remain with modified load, grip, range of motion, tempo,
  implement, or volume
- exercises that are unrelated and should not be used as substitutions for the
  target movement pattern

## Required Product Behavior

When a user has a pain, injury, or limitation such as tennis elbow, the workout
planner must adapt exercises in a movement-aware and constraint-aware way.

### Preserve Workout Intent

Exercise substitutions must preserve the original training intent as much as
possible:

- movement pattern
- target muscle group
- training day role
- equipment constraints
- user experience level
- available session time

Examples:

- a horizontal pull should prefer an elbow-friendlier horizontal pull before a
  lower-body exercise
- a press should prefer an elbow-friendlier press or machine variation before a
  squat
- an arm isolation exercise may be reduced, swapped, or omitted depending on
  the limitation severity

Lower-body substitutions are acceptable only when the original movement or day
actually targets lower body, or when a full-body day intentionally needs a
lower-body slot.

### Load / Volume Adjustment Before Replacement

For some exercises and limitations, the correct adaptation is not replacement.
The planner should be able to keep the exercise while reducing or modifying
load.

Possible adaptations include:

- reduce load
- reduce sets
- reduce reps
- change grip
- change implement
- change range of motion
- change tempo
- use machine/cable support
- avoid painful end ranges
- replace only if the movement is still high risk after modification

The system must not blindly apply load reduction to every exercise or every
pain condition. Some combinations require replacement or removal rather than
lighter loading.

### User-Facing Transparency

When the planner keeps an elbow-sensitive exercise but modifies it, the user
should see a concise explanation.

Example:

```text
הורדתי עומס ב־Dumbbell Row בגלל טניס אלבו: לעבוד קל יותר, אחיזה ניטרלית, בלי כאב.
```

The explanation should identify:

- the exercise affected
- the limitation considered
- what changed
- whether the change is a load reduction, grip/variation change, volume change,
  or substitution

Do not overload the plan with long medical prose. Keep it coaching-oriented and
actionable.

### Safety

The system must remain conservative. If an exercise is clearly unsafe for the
reported limitation, it should be replaced or removed.

The bot must not present itself as diagnosing or treating a medical condition.
It should adapt training conservatively and encourage stopping if pain occurs.

## Proposed Solution

Introduce or strengthen a structured pain-aware exercise adaptation layer.

The layer should operate on a candidate exercise with explicit metadata:

- movement pattern
- target muscles
- joint load profile
- grip demand
- elbow flexion/extension demand
- forearm/wrist loading
- equipment
- regression / progression options
- suitable substitutions by movement pattern
- whether load reduction is an acceptable mitigation for the limitation
- whether replacement is required for the limitation

For each candidate exercise and active limitation, produce an adaptation
decision:

- `KEEP`
- `KEEP_WITH_LOAD_REDUCTION`
- `KEEP_WITH_VARIATION`
- `REDUCE_VOLUME`
- `SUBSTITUTE_SAME_PATTERN`
- `SUBSTITUTE_ADJACENT_PATTERN`
- `OMIT`
- `REQUIRES_USER_CLARIFICATION`

The planner should then build the workout from these decisions instead of
simply filtering out every exercise that touches the painful region.

## Implementation Approach

First trace the current path:

```text
training_limitations / medical_constraints
-> active pain / limitation resolution
-> exercise catalog metadata
-> workout candidate generation
-> pain-aware filtering / substitution
-> plan rendering
-> approval screen
```

Identify:

- where tennis elbow is represented
- how active limitations are read
- where exercises are filtered
- where substitutions are selected
- whether movement pattern is preserved
- whether load reduction exists as an adaptation distinct from replacement
- where user-facing adaptation notes can be rendered

Then implement the smallest coherent architecture:

1. Keep the existing canonical limitation/pain source of truth.
2. Add or refine exercise metadata only where needed to make safe decisions.
3. Ensure substitution ranking preserves the original movement pattern before
   falling back to broader alternatives.
4. Add explicit adaptation decisions for load/volume/variation changes.
5. Render concise adaptation notes in the workout approval flow.
6. Add regression tests for tennis elbow and at least one non-elbow limitation
   so the logic does not become hard-coded to this incident.

## What Not To Do

- Do not replace elbow-sensitive upper-body work with squats just because
  squats are elbow-safe.
- Do not use "safe" as the only substitution criterion.
- Do not delete every exercise that has any elbow involvement.
- Do not apply a universal load-reduction multiplier to all exercises.
- Do not claim medical certainty or prescribe rehabilitation.
- Do not create a second pain/limitation state model.
- Do not hard-code only the screenshot's exercises.
- Do not silently change the workout goal without telling the user.
- Do not let substitutions break the declared split structure or training-day
  role unless the user explicitly approves that tradeoff.

## Acceptance Criteria

1. Tennis elbow does not cause chest, back, or arm days to be filled with
   unrelated squat/lower-body substitutions.
2. Substitutions preserve movement pattern and target muscle group whenever a
   safe same-pattern alternative exists.
3. Load/volume/variation adjustment is available as a distinct adaptation from
   replacement.
4. Load reduction is applied only when appropriate for the exercise and
   limitation.
5. Clearly contraindicated movements are still replaced or omitted.
6. The approval screen shows concise notes for exercises changed because of a
   limitation.
7. The plan remains coherent for the selected split and frequency.
8. Existing pain-aware training behavior and canonical limitation lifecycle are
   preserved.

## Regression Cases

Add tests for:

- tennis elbow with chest/front-arm day: no generic squat replacement for
  pressing/arm slots when suitable same-pattern alternatives exist
- tennis elbow with back/rear-arm day: pulling slots remain pulling-oriented,
  with grip/load/variation adaptation when appropriate
- full-body day: lower-body exercises remain valid only in lower-body slots
- load reduction note appears for an exercise kept with modified load
- exercise requiring replacement is substituted with a same-pattern safer
  option
- exercise where load reduction is not appropriate is not kept merely by making
  it lighter
- no active limitation: normal exercise selection is unchanged
- non-elbow limitation: adaptation is not hard-coded to tennis elbow

## Completion Report Required

At implementation completion, report:

1. root cause
2. files changed
3. limitation source of truth used
4. exercise adaptation architecture
5. substitution ranking logic
6. load/volume/variation adjustment rules
7. user-facing rendering changes
8. tests added
9. tests run and results
10. remaining risks
