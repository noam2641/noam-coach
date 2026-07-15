# TASK 60 - Workout Time Averages and Outlier-Day Approval

## Status

Open product/implementation task.

## Source Request

During the Health import confirmation flow, after the user confirms the number
of weekly training days and the specific training weekdays, the bot should also
use historical workout times more intelligently:

```text
הייתי רוצה שיוסיף שעות ממוצעות לכל יום ושיציע לי שעת אימון לפי ממוצע של הממוצעים
אלא אם יש ימים עם ממוצע חריג לשעת אימון ואותם ימים יופיעו לאישור עם שעות שונות
```

Meaning:

- compute average workout time for each recurring workout day
- suggest the default workout time from the average of those per-day averages
- if specific weekdays have materially unusual average workout times, show
  those days for approval with their own different times

## Problem

The current Health confirmation wizard separates:

- weekly workout frequency
- workout weekdays
- typical workout hour
- typical workout duration

This is better than one bundled "workout pattern" confirmation, but the
workout-hour step is still too coarse.

Current behavior can collapse all historical workouts into one typical hour.
That loses important day-specific routine evidence. For example:

- Sunday workouts are usually around 19:00
- Tuesday workouts are usually around 19:15
- Wednesday workouts are usually around 19:00
- Friday workouts are usually around 10:00

A single global average may produce a reasonable default for most days, but it
should not silently overwrite the clearly different Friday pattern.

The user should not have to manually inspect or correct this when the Health
history already contains enough evidence.

## Required Product Behavior

After the workout-days step has established the proposed/approved training
weekdays, the Health confirmation wizard must use historical workout start
times to propose training times.

### Per-Day Averages

For every recurring proposed training weekday, calculate a typical workout
start time from actual historical workout records for that weekday.

The calculation should:

- use local time
- handle midnight wrap correctly
- be robust to occasional outlier sessions
- expose enough sample-count evidence to avoid false confidence
- only produce a per-day average when there are enough sessions for that day

### Default Suggested Workout Time

Compute the default suggested workout time as the average of the per-weekday
average times, not as the average of all workout rows.

Rationale:

- each recurring training day should contribute equally to the default plan
  time
- a weekday with many historical records should not dominate the global
  suggestion
- the default should represent the normal training schedule across the selected
  days

Example:

```text
ראשון: 19:00
שלישי: 19:15
רביעי: 19:05
שישי: 10:00
```

The default should be derived from the day-level averages, and the Friday
morning pattern should be detected as potentially different from the rest.

### Outlier Day Detection

If one or more selected weekdays have an average workout time that is materially
different from the default suggested time, those days must be surfaced for
approval as day-specific workout times.

The implementation should define a clear threshold for "materially different",
for example a clock-distance threshold such as 90 minutes, subject to
engineering judgment and tests.

The comparison must be circular-clock aware:

- 23:30 and 00:30 are close
- 23:30 and 12:00 are far

### Confirmation UX

The wizard should present a clear confirmation screen such as:

```text
לפי היסטוריית האימונים:
• ראשון: סביב 19:00
• שלישי: סביב 19:15
• רביעי: סביב 19:05
• שישי: סביב 10:00

אשתמש ב־19:05 כשעת האימון הרגילה.
שישי נראה שונה מהשאר — לאשר אותו סביב 10:00?
```

The exact wording may follow existing bot style, but the UX must make clear:

- what the normal/default suggested time is
- which days are different
- what time each different day would receive
- that the user can approve or correct the proposal

### Persisted Plan Semantics

When approved:

- the default workout time should be saved as the general `workout_window`
  planning preference
- selected outlier weekdays should retain their own day-specific workout time
  in the canonical weekly availability / training-day representation
- non-outlier days should use the default time

Do not create a separate parallel workout-time state system.

Reuse the existing canonical planning facts and availability structures already
used by the Health confirmation wizard and plan generation.

## Implementation Approach

First trace the current Health import and confirmation path:

```text
Health import
-> routine.learn_workout_pattern
-> routine profile persistence
-> workout_pattern fact
-> Health confirmation wizard
-> workout frequency confirmation
-> workout day confirmation
-> workout hour confirmation
-> weekly_availability / workout_window facts
-> plan generation
```

Then implement the smallest coherent extension:

1. Enrich the learned workout pattern with per-weekday time evidence.
2. Preserve the existing single `typical_hour` behavior as a default fallback.
3. Change the workout-hour confirmation step to use the per-day evidence when
   available.
4. Store approved outlier-day times in the existing weekly availability slots.
5. Keep manual text correction supported for a single global hour.
6. Do not change workout frequency or day-selection behavior except where the
   approved days are needed to decide which per-day times matter.

## Constraints

- Do not infer day-specific times from template/planned sessions; use actual
  workout history.
- Do not treat one unusual workout as a stable per-day pattern.
- Do not let stale or sparse Health data create false precision.
- Do not create another independent "today" or workout schedule model.
- Do not regress existing behavior where a single typical hour is all that is
  available.
- Preserve existing Health data quality, freshness, manual override, and
  confirmation behavior.

## Regression Cases

Add focused tests for:

1. Per-weekday average workout times are calculated from local workout start
   times.
2. The default suggested hour is computed from weekday averages rather than all
   workout rows.
3. A weekday with a materially different average time is detected as an
   outlier.
4. Non-outlier weekdays use the default workout time.
5. Approved outlier weekdays are persisted with their own `start` time in
   weekly availability.
6. Manual global hour correction still works.
7. Sparse weekday data does not create a day-specific override.
8. Midnight-adjacent workout times are compared using circular clock distance.
9. Existing workout-frequency, workout-day, and workout-duration confirmation
   tests continue to pass.

## Acceptance Criteria

- The Health confirmation flow can show average workout time per proposed
  training weekday.
- The bot suggests one default workout time derived from the average of
  per-day averages.
- Days with materially different average workout times are surfaced separately
  for approval.
- Approval persists default and day-specific times through the existing
  canonical planning facts.
- No production behavior is implemented through hard-coded weekday examples.
- Existing user-confirmed manual training schedule values continue to take
  precedence over Health-derived estimates.

## Completion Report Required

At implementation completion, report:

1. root cause
2. files changed
3. data model / fact fields used
4. per-day averaging logic
5. outlier threshold and rationale
6. confirmation UX behavior
7. tests added
8. tests run and results
9. remaining risks
