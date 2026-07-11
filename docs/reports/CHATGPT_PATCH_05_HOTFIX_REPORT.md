# CHATGPT PATCH-05 — Hotfix for `build_nutrition_context(local_now=...)`

## Problem
Full pytest stopped at:

`TypeError: build_nutrition_context() got an unexpected keyword argument 'local_now'`

The regression test `test_learned_foods_personalization.py` calls `build_nutrition_context(..., local_now=...)`, while PATCH-04's implementation exposed only `now=`.

## Fix
`noam_coach/services/nutrition_context.py` now accepts both:

- `now=` — preferred new parameter
- `local_now=` — compatibility alias for existing tests/callers

If both are passed, `now` wins. This is a tiny compatibility fix, not a product behavior change.

## Verification in ChatGPT environment
- `python -m compileall -q noam_coach/services/nutrition_context.py` — passed

Run locally after extracting:

```powershell
python -m pytest -p no:cacheprovider -q tests/regression/test_learned_foods_personalization.py
python -m pytest -p no:cacheprovider -q
```
