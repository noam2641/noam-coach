# CHATGPT PATCH-06 HOTFIX — restore `menu:morning` on unified home menu

## Cause
Full suite failed in `tests/test_coach_bot_logic.py::test_home_keyboard_is_the_single_unified_menu` because the patched `home_keyboard()` exposed `menu:daily_menu` but no longer exposed the legacy/expected `menu:morning` callback.

## Fix
Updated `noam_coach/bot/ui.py` so the single unified home menu includes both:

- `menu:daily_menu` — the new daily menu entry.
- `menu:morning` — the expected morning update callback still required by existing tests and handlers.

No product flow was removed. This preserves PATCH-04's unified menu while keeping backward compatibility with the existing test and callback contract.

## Files changed
- `noam_coach/bot/ui.py`

## Validation to run locally
```powershell
python -m pytest -p no:cacheprovider -q tests/test_coach_bot_logic.py::test_home_keyboard_is_the_single_unified_menu
python -m pytest -p no:cacheprovider -q
```
