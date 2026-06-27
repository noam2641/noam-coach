# Codex Audit: REC-NEXT-MEAL-05

Date: 2026-06-27

Scope: implement the central "what should I eat now?" recommendation flow after
REC-PROGRAM-04 completion. The work covers deterministic nutrition context,
workout timing/status handling, Telegram surfaces, Mini App parity, dietary
restriction filtering, and acceptance tests.

## Implementation Summary

- Added `noam_coach.services.next_meal` as the single recommendation engine for
  next-meal context, budgets, options, formatting, and workout clarification.
- Replaced the old `recommendations.intraday_next_meals` path in proactive and
  on-demand health jobs.
- Wired Telegram menu callbacks and free-text `next_meal` routing to the same
  service, including workout-status clarification buttons.
- Added `/mini/api/next-meal` and a Mini App card that renders the server-built
  recommendation without duplicating calculations in JavaScript.
- Added acceptance tests for rest day, pre-workout, post-workout, unclear
  workout status, persisted clarification, calorie overage, dietary filtering,
  and Mini App API parity.

## Behavior Notes

- Workout status is evidence-based. Active or completed sessions win over plan
  timing; planned time passing without session evidence does not imply the user
  trained.
- Calorie and protein balances are signed. Overage is shown as overage and does
  not produce a large "remaining" meal.
- Provisional/default goals are labelled in the rendered recommendation.
- Meal options are filtered through the canonical dietary-restriction firewall
  introduced in REC-PROGRAM-04.

## Verification

| Command | Result |
|---|---|
| `C:\Users\user\anaconda3\python.exe -m ruff check noam_coach/services/next_meal.py noam_coach/services/health_jobs.py noam_coach/bot/callback_menu.py noam_coach/bot/assistant.py mini_api.py tests/acceptance/test_rec_next_meal_05.py` | Passed |
| `C:\Users\user\anaconda3\python.exe -m compileall -q coach_bot.py noam_coach mini_api.py` | Passed |
| `C:\Users\user\anaconda3\python.exe -m pytest tests/acceptance/test_rec_next_meal_05.py -q` | `8 passed` |
| `C:\Users\user\anaconda3\python.exe -m pytest --maxfail=0 -ra` | `565 passed, 3 warnings` |

## Remaining Limits

- Live Telegram rendering was not exercised against the external Telegram
  service; callback behavior is wired through production handlers and covered
  at the service/API level.
- Meal option generation is deterministic and conservative; richer recipe
  variety can be added later behind the same central service boundary.
