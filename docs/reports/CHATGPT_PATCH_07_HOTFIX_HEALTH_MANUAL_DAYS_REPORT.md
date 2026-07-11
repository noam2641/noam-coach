# PATCH-07 Hotfix — Health import must not re-ask workout frequency when manual days already exist

## Why this patch exists

The previous HealthKit/manual-availability work prevented Health-derived training days from becoming the active plan source when the user had manually supplied days.
However, the Health confirmation wizard could still show the insufficient-data screen:

> אין מספיק שבועות עם נתוני שעון... כמה אימונים בשבוע תרצה לתכנן?

That is confusing when the bot already has confirmed manual training days/frequency.

## Product rule added

If the user already has confirmed manual data, the Health wizard must not re-ask the same training field from a weak/old Health export:

- confirmed `active_training_days` / `preferred_training_days` / `training_days_per_week` => skip Health workout-frequency prompt.
- confirmed `active_training_days` / `preferred_training_days` / `weekly_availability` => skip Health detected-days prompt.
- confirmed `workout_window` => skip Health detected-hour prompt.

The Health data can remain visible/detected, but it must not interrupt the user or override manual plan inputs.

## Files changed

- `noam_coach/services/health_jobs.py`
- `tests/regression/test_re13_health_quality.py`

## New regression coverage

- `test_insufficient_frequency_does_not_reask_when_manual_training_days_exist`
- `test_health_wizard_does_not_offer_detected_days_over_manual_active_days`

## Local validation in ChatGPT environment

- `python -m compileall -q noam_coach/services/health_jobs.py tests/regression/test_re13_health_quality.py` passed.
- Full pytest could not run here because the sandbox is missing project dependencies such as `aiosqlite`.

## What to run on Noam's machine

```powershell
python -m pytest -p no:cacheprovider -q tests/regression/test_re13_health_quality.py::test_insufficient_frequency_does_not_reask_when_manual_training_days_exist tests/regression/test_re13_health_quality.py::test_health_wizard_does_not_offer_detected_days_over_manual_active_days
python -m pytest -p no:cacheprovider -q
```
