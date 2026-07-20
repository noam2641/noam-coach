# TASK 62 - Proactive Morning Briefing and Daily Menu Delivery

## Status

Open product/implementation task.

## Source Incident

The morning Telegram experience shows a long nutrition plan with remaining
calories/protein, workout timing, detailed meal schedule, recipe-like meal
descriptions, and explanatory coaching text. In the screenshot, the user asks
why the daily summary appears before the user asks for it and notes that the
morning update should be concise:

- remaining calories
- remaining protein
- workout status/time
- one immediate recommendation or next action

The user should not receive a long full-day menu automatically when the desired
interaction is a short morning briefing.

## Problem

The codebase has separate concepts for:

- short morning briefing (`build_morning_briefing_text`)
- full standalone daily menu (`build_morning_menu_text`)

However, the proactive morning job still sends both:

```text
job_morning
-> morning_checkin
-> morning_menu
```

That means even if `menu:morning` is correctly short, the scheduled morning
experience can still push a full daily menu as an additional message. This
reintroduces the same UX problem through a different surface.

## Required Product Behavior

The proactive morning experience must be short by default.

It should include:

- greeting / day context
- remaining calories
- remaining protein
- today's workout status and time when known
- one or two immediate priorities
- buttons for the next action

It should not automatically send a long full-day menu unless the user explicitly
asked for a daily menu or has opted into a pinned daily menu behavior.

## Proposed Solution

Unify the proactive morning delivery policy:

1. Keep `menu:morning` as a short briefing.
2. Make `job_morning` send the short briefing/check-in by default.
3. Move full daily menu delivery behind an explicit action such as
   `menu:daily_menu`.
4. If automatic daily-menu delivery remains supported, make it opt-in and
   visibly distinct from the morning briefing.
5. Ensure all proactive and on-demand morning surfaces use the same naming and
   message semantics.

## What Not To Do

- Do not make the short briefing include the full meal plan.
- Do not silently send a full menu immediately after the short briefing unless
  that behavior is explicitly configured.
- Do not duplicate the same nutrition context in two messages with conflicting
  wording.
- Do not remove the full daily menu feature; keep it available by explicit
  action.
- Do not create another parallel morning renderer.

## Acceptance Criteria

1. The scheduled morning message is concise by default.
2. A full daily menu is sent only after explicit user action or explicit opt-in.
3. `menu:morning` and proactive morning delivery share the same short-briefing
   semantics.
4. `menu:daily_menu` remains available for the full pinnable plan.
5. The morning flow does not send duplicate or conflicting calorie/protein
   summaries.
6. Tests cover scheduled morning delivery, on-demand morning briefing, and
   explicit daily-menu request.

## Completion Report Required

At implementation completion, report:

1. root cause
2. files changed
3. morning delivery policy
4. renderers reused
5. tests added
6. tests run and results
7. remaining risks
