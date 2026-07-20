# TASK 65 - Focused "What Should I Eat Now?" Response

## Status

Open product/implementation task.

## Source Incident

When the user taps or asks:

```text
מה לאכול עכשיו?
```

the bot returns a long response containing:

- remaining daily calories/protein
- one or more meal options
- after-meal budget calculations
- snack or later-meal planning
- workout context
- buttons for planning/eating options
- sometimes an extended explanation of the rest of the day

The user expects this action to answer the immediate question only: what to eat
now.

## Problem

The code already contains comments saying the next-meal screen should be
answer-first and focused. However, the rendered first screen still combines
multiple concepts:

- daily status
- workout status
- remaining-day timeline
- immediate meal recommendation
- after-meal projections
- explanatory rationale

This makes the response difficult to scan and turns a quick action into another
planning screen.

## Required Product Behavior

The first response to "מה לאכול עכשיו?" must be focused on one immediate eating
action.

It should show:

- the recommended meal/snack now
- approximate calories and protein
- minimal reason if needed
- one primary action button
- optional small controls such as refresh or "why this fits"

It should not show a full daily status, full remaining-day timeline, or long
future meal plan on the first screen.

Those details may exist behind secondary actions:

- "למה זה מתאים"
- "מצב היום"
- "תכנון שאר היום"
- "אפשרויות נוספות"

## Proposed Solution

Separate first-screen next-meal rendering from detailed explanation rendering:

```text
format_next_meal_recommendation -> immediate answer only
format_next_meal_explanation -> calculations and context
remaining-day timeline -> separate detail/status surface
```

Keep the same canonical recommendation engine, but reduce the default renderer.

## What Not To Do

- Do not remove budget-aware recommendation logic.
- Do not hide safety warnings that directly affect the immediate food.
- Do not show multiple comparable options unless the user explicitly asks for
  alternatives.
- Do not include the full remaining-day plan in the first response.
- Do not duplicate "מצב היום" inside "מה לאכול עכשיו".
- Do not make the user read long rationale before seeing the food.

## Acceptance Criteria

1. The first "מה לאכול עכשיו" response fits on a compact Telegram screen.
2. The first response contains one immediate recommendation by default.
3. Daily status and remaining-day timeline are not shown on the first screen.
4. Detailed calculation remains available behind "למה זה מתאים" or equivalent.
5. Workout-sensitive recommendations still account for pre/post-workout timing.
6. Safety warnings relevant to the immediate meal still appear.
7. Regression tests verify the first-screen text does not include daily-status
   headings, timeline headings, or multiple future meal slots.

## Completion Report Required

At implementation completion, report:

1. root cause
2. files changed
3. renderer architecture
4. information moved to detail screens
5. tests added
6. tests run and results
7. remaining risks
