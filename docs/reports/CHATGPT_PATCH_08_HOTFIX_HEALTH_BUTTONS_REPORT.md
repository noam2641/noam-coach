# PATCH-08 hotfix — remove weak HealthKit numeric buttons

## What changed

The Health import wizard no longer shows generic `2 / 3 / 4` buttons when the HealthKit export is too weak or stale to infer a reliable workout frequency.

Instead, when Health data is insufficient, the bot asks the user to type the desired weekly workout frequency, e.g. `3`, and keeps only the explicit `דלג על שאר האישורים` escape button.

## Why

The product rule from the Telegram screenshots was not only “fix the data override”; it was also to avoid noisy or misleading buttons. When the export is not reliable, quick-choice buttons nudge the user into an arbitrary plan and make the flow look unfinished.

## Files

- `noam_coach/services/health_jobs.py`
- `tests/regression/test_re13_health_quality.py`

## Verification here

- `compileall` passed for the changed Python files.

Run locally:

```powershell
python -m pytest -p no:cacheprovider -q tests/regression/test_re13_health_quality.py::test_insufficient_frequency_asks_user_directly
python -m pytest -p no:cacheprovider -q
```
