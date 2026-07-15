# TASK 59 - Render "Remaining Day" as a Chronological Action Timeline

## Status

Open implementation task.

## Current Production Behavior

After meal approval, the bot currently renders a generic block such as:

```text
המשך היום:
😴 00:36 שינה
🏋️ 19:09 אימון
🍽️ ארוחה לפני אימון: כ־1411 קל׳ | כ־131 ג׳ חלבון
יש אימון מתוכנן היום, עדיין לפניו.
```

This is difficult to scan and does not behave like an actual coaching plan for
the rest of the day.

## Required UX

Replace the generic remaining-day summary with one chronological timeline of
concrete next actions.

Example desired output:

```text
המשך היום:
🍽️ 18:00 ארוחה לפני אימון: כ־300 קל׳ | כ־40 ג׳ חלבון
🏋️ 19:09 אימון
🍽️ 20:40 ארוחת ערב: כ־400 קל׳ | כ־61 ג׳ חלבון
🍽️ 23:00 ארוחת לילה: כ־100 קל׳ | כ־30 ג׳ חלבון
😴 00:36 שינה
```

## Requirements

1. Render all remaining-day events in chronological order.

2. Timeline events may include:
   - meal
   - workout
   - sleep
   - other already-supported routine events when relevant

3. Each planned meal must have:
   - concrete planned time
   - meal role/name
   - approximate calorie allocation
   - approximate protein allocation

4. Do not show the entire remaining daily calorie/protein budget as the target
   for every individual meal.

   Example of invalid behavior:

   ```text
   ארוחה לפני אימון: 1411 kcal | 131g protein
   ארוחת ערב: 1411 kcal | 131g protein
   ```

   The remaining daily budget must be allocated across the remaining meals.

   Invariant:

   ```text
   sum(planned_remaining_meal_calories)
   ```

   should approximately equal the calorie budget intentionally allocated to the
   remainder of the day.

   ```text
   sum(planned_remaining_meal_protein)
   ```

   should approximately equal the remaining protein target, subject to
   realistic meal construction and rounding.

5. Meal timing must respect:
   - current time
   - workout time/status
   - sleep time
   - already consumed meals
   - previously planned meal timing
   - reasonable pre-workout and post-workout spacing

6. The timeline must be recalculated after a state-changing event, including:
   - meal approval
   - meal correction
   - meal deletion
   - workout completion
   - workout cancellation
   - workout postponement/reschedule
   - target change
   - relevant routine change

7. Do not add explanatory prose below the timeline when the timeline itself
   already communicates the state.

   For example, remove redundant text such as:

   ```text
   יש אימון מתוכנן היום, עדיין לפניו.
   ```

   when the chronological timeline already clearly shows the upcoming workout.

8. Preserve existing canonical coaching-day, meal lifecycle, workout state, and
   freshness/invalidation behavior introduced by FIX 1-57.

9. Audit all user-facing surfaces that render the remaining day or post-meal
   continuation plan.

   The same timeline semantics should be reused rather than independently
   reconstructed in multiple Telegram views.

## Implementation Process

First trace the existing post-meal status and remaining-day planning path.

Identify:

- the canonical planner
- calorie/protein budget allocation logic
- meal timing logic
- workout event source
- sleep/routine event source
- all renderers that independently rebuild the same concept

Implement the smallest coherent change.

Do not create another independent TODAY representation.

## Regression Tests

Add regression tests for:

- workout day
- non-workout day
- meal approval
- meal correction
- workout completed
- workout postponed
- chronological ordering
- remaining protein allocated across meals rather than repeated per meal

## Completion Report Required

At the end report:

1. root cause
2. files changed
3. planner/rendering architecture used
4. allocation logic
5. tests added
6. tests run and results
7. remaining risks
