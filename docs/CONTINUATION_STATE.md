# Continuation State — 2.0.0-rc5

עודכן: 2026-06-24

## הושלם

- סנכרון HealthKit/Shortcuts/Watch שוטף כבוי כברירת מחדל.
- ייבוא Apple Health תקופתי נשאר דרך ZIP/XML.
- נוסף ייבוא מנתיב קובץ או תיקייה מקומית בצ'אט ובפקודה `/importpath`.
- הנתיב מוגבל לשורשים מורשים ואינו יכול להיות UNC או נתיב חיצוני.
- תיקייה בוחרת את export החדש ביותר; קובץ המקור אינו נמחק.
- Telegram upload, Mini App upload ונתיב מקומי משתמשים בליבת ייבוא משותפת.
- נוסף מדריך Mini App ו־PowerShell helper ל־Cloudflare Tunnel.

## אימות

- compileall: עבר.
- Ruff: עבר.
- pytest: 351 בדיקות עברו.
- evaluations: 33/33 ב־11 קטגוריות.
- preflight: עבר.
- coverage כולל: 67%.
- build release: 160 קבצים, 0 forbidden entries.

## תלות חיצונית

- Telegram token ו־OpenAI key אמיתיים.
- Mini App בטלפון דורש HTTPS ציבורי; לפיתוח ניתן להשתמש ב־Quick Tunnel.
- נתיב מקומי עובד רק כשהבוט רץ על אותו מחשב שמכיל את הקובץ.
## REC-PROGRAM-04 checkpoint — 2026-06-27

- Current batch completed/reviewed; `REC-NEXT-MEAL-05` has since been implemented.
- Audit: `docs/CODEX_AUDIT_REC_PROGRAM_04.md`.
- Test matrix: `docs/TEST_MATRIX.md`.
- Verified gates: compileall passed, Ruff passed, pytest `557 passed, 3 warnings`, evaluations `33/33`, preflight passed, bounded startup smoke passed.
- Release ZIP: `dist\noam_coach_2.0.0-rc5.zip`, 169 files, 0 forbidden entries, SHA-256 `48ffab080da0b07fda606c9a108cfd5777481b177670057dc9e91ba4fd4d9fe6`.
- Remaining external limitations: full live Telegram/OpenAI behavior depends on external credentials/services; extracted release full startup needs a private `.env` that is intentionally excluded.

## REC-NEXT-MEAL-05 checkpoint — 2026-06-27

- Implemented a central deterministic next-meal service in `noam_coach/services/next_meal.py`.
- Telegram menu, Telegram free-text, proactive health jobs, and Mini App `/mini/api/next-meal` now share the same recommendation logic.
- Workout status is evidence-based: active/completed sessions override plans, and passed planned workout times ask for clarification instead of assuming completion.
- Added acceptance coverage in `tests/acceptance/test_rec_next_meal_05.py`.
- Audit: `docs/CODEX_AUDIT_REC_NEXT_MEAL_05.md`.
- Verified gates: targeted Ruff passed, compileall passed, targeted acceptance `8 passed`, full pytest `565 passed, 3 warnings`.

## Functional UX checkpoint - 2026-06-27

- Added `docs/FUNCTIONAL_UX_TRACEABILITY.md` with production-path coverage for Telegram, Mini App, planning, meals, workouts, Health import, proactive jobs, and release readiness.
- Fixed a Telegram runtime regression where `menu:smartplan` could raise `NameError("name 'clear_flow_state' is not defined")`.
- Plan-completion flow now has regression coverage for continuing questions and clearing state when returning to the plan hub.
- Mini App "what to eat now" now exposes workout clarification actions and persists them through the same `daily_flags` service used by Telegram.
- Mini App now displays today's meals from `/mini/api/meals/today`, including tracking quality, so Telegram-logged meals are visible in the web surface.
- Next-meal recommendations now read the fasting daily flag; active fasting uses a fast-break budget, and "not fasting" returns the recommendation to the normal path.
- Added `noam_coach/services/nutrition_context.py`; Telegram/proactive morning menu, evening nutrition summary, and AI meal reanalysis fallback now receive a structured nutrition AI request with reported meals, planned meals, daily flags, restrictions, workout context, and tracking quality.
- Restored dietary restriction classification event logging across all classification branches.
- Verified gates: compileall passed, Ruff passed, pytest `573 passed, 3 warnings`, evaluations `33/33`, preflight passed.
- Smoke checks: FastAPI route smoke passed with runtime readiness flags, Telegram initialization smoke passed with 13 handlers and 6 jobs, Mini App route smoke passed, DB migration smoke passed on a temporary DB.
- Release ZIP: `dist\noam_coach_2.0.0-rc5.zip`, 178 files, 0 forbidden entries, SHA-256 `088b1f573415eb2bece1607250a4f65478e7b0e2623635aa3e15daf81e684b8b`.
