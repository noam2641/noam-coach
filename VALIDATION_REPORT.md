# Validation Report — 2.0.0-rc5

תאריך אימות: 2026-06-25

## סביבת האימות

- Python: 3.13.5 בסביבת הבנייה. יעד ההתקנה המתועד נשאר Python 3.12+.
- מערכת הקבצים שנבדקה הייתה עותק נקי ללא `.env`, credentials או נתוני משתמש.
- סנכרון HealthKit השוטף נשאר כבוי במהלך האימות; ייבוא ZIP/XML נבדק דטרמיניסטית.

## שערים שעברו בפועל

- `python -m compileall -q .` — עבר.
- `python -m ruff check .` — עבר ללא ממצאים.
- `python -m pytest -q` — **372 בדיקות עברו**.
- `python scripts/run_evaluations.py` — **33/33 מקרים ב־11 קטגוריות עברו**.
- `python scripts/preflight.py --skip-runtime-secrets` — עבר.
- `python scripts/build_release.py` — עבר; הארכיון נבנה מ־allowlist וכולל **161 קבצים**.
- בדיקת forbidden entries בארכיון — 0.
- startup בפועל — הבוט עולה, DB נפתח, Telegram ו־FastAPI פעילים.

## סקירת אבטחה ותיקוני באגים — 2026-06-25

### תיקונים קריטיים

| # | קובץ | תיאור | תיקון |
|---|-------|--------|-------|
| 1 | `coach_bot.py` | `NameError: ensure_user_record` בעליית הבוט — `runtime_bind` לא מצא את מודול `coach_bot` ב־`sys.modules` כשרץ כ־`__main__` | רישום `sys.modules["coach_bot"]` לפני `run()` |
| 2 | `db.py:1014` | SQL injection במיגרציה — f-string עם `now` | פרמטריזציה עם `?, ?` |
| 3 | `noam_coach/api/security.py:30` | `int(bytes)` זורק `TypeError` — Content-Length לא נבדק | `raw_length.decode("ascii")` + catch `TypeError` |
| 4 | `noam_coach/services/goals.py:220,240` | שני UPDATE ללא transaction — קריסה באמצע משאירה משתמש ללא יעד פעיל | עטיפה ב־`DB.transaction()` |
| 5 | `models.py` | mutation של `FoodItem` עוקפת ולידציה — NaN/infinity/שליליים עוברים | `validate_assignment=True` + `object.__setattr__` בוולידטור |
| 6 | `helpers.py:27` | Stored XSS דרך `_safe_html_block` — תגיות עם attributes מוחזרות | regex שמאפשר רק `<b>`, `</b>`, `<i>`, `</i>` ללא attributes |

### תיקוני אבטחה ותקינות

| # | קובץ | תיאור | תיקון |
|---|-------|--------|-------|
| 7 | `noam_coach/api/security.py:22` | `/mini/` paths לא מוגנים | הרחבת body limit ו־rate limit ל־`/mini/` |
| 8 | `noam_coach/api/watch_routes.py:41,83` | `IndexError` ללא bounds check | בדיקת טווח לפני indexing |
| 9 | `noam_coach/bot/workout.py:466` | קריאות לא-טרנזקציונליות ב־workout summary | עטיפה ב־transaction אחת |
| 10 | `noam_coach/api/health_routes.py:130` | ספירה שגויה של cumulative health samples | הבחנה בין insert ל־update |
| 11 | `health_service.py:73` | `conn.total_changes` לא אמין | שימוש ב־`SELECT changes()` |
| 12 | `mini_api.py:406` | I/O סינכרוני חוסם event loop | `asyncio.to_thread` לכתיבת chunks |

### תיקוני datetime naive/aware

| # | קובץ | תיאור |
|---|-------|--------|
| 13 | `data_quality.py:198` | `fromisoformat` naive — gate פרואקטיבי לא עובד |
| 14 | `user_model.py:779` | `is_fact_fresh` — facts ישנים נחשבים טריים |
| 15 | `conversation.py:130` | `is_expired` — flows לא פגים |
| 16 | `callback_session.py:755,854` | duration calculation שגוי |
| 17 | `noam_coach/jobs/proactive.py:258` | `astimezone` על naive — gap שגוי |

### תיקוני לוגיקה

| # | קובץ | תיאור |
|---|-------|--------|
| 18 | `routine.py:214,253` | `round(None, 1)` crash כש-robust_mean מחזיר None |
| 19 | `planning.py:335` | תיקון protein מוריד מתחת ל-15g floor |
| 20 | `planning.py:868` | הפעלה שקטה נכשלת כש-lastrowid=None |
| 21 | `conversation.py:378` | `extract_flow_id` נכשל עבור legacy flows |
| 22 | `noam_coach/bot/onboarding.py:1315` | `question_by_id` ללא null check |
| 23 | `noam_coach/bot/onboarding.py:451` | `int(index_str)` ללא try/except |
| 24 | `noam_coach/bot/callback_meals.py:193` | `split/float` ללא validation |
| 25 | `coach_intelligence.py:112` | `float(declared)` ללא try/except |
| 26 | `retention.py:73` | cleanup פונה לטבלה ישנה |
| 27 | `targets.py:109` | `or` מחליף ערך 0 בברירת מחדל |
| 28 | `targets.py:129` | protein reference ב-maintenance goal |
| 29 | `reconcile.py:108` | MIN_OBSERVATIONS א-סימטרי |
| 30 | `routine.py:299` | bucket key 24 כפילות midnight |
| 31 | `recommendations.py:159` | dead code — `flags.get("flag")` |
| 32 | `health_jobs.py:463` | `notify_admin` לא ב-suppress |
| 33 | `config.py:169` | VERSION file חסר גורם crash |
| 34 | `db.py:883` | `PRAGMA foreign_keys=ON` הועבר ל-finally |
| 35 | `noam_coach/jobs/proactive.py:550` | `hours_left_until_sleep` מחזיר 24h |
| 36 | `callback_session.py:278` | `or None` מבטל dict עם ערכי 0 |

## בדיקות רגרסיה שנוספו

- `tests/test_bugfix_regression.py` — 21 בדיקות חדשות המכסות את כל הממצאים.
- סה"כ: 372 בדיקות (מ-351 קודם).

## כיסוי

- coverage כולל: **67%**.
- `coach_bot.py`: 99%.
- `health_service.py`: 92%.
- `training_intelligence.py`: 94%.
- `noam_coach/services/local_health_path.py`: 89%.
- routers של Health/Watch/System: 85%–88%.

## דברים שלא נבדקו מול מערכת אמיתית

- Telegram production ונתיב Windows אמיתי מתוך צ'אט.
- OpenAI בתשלום.
- Cloudflare Tunnel/HTTPS ציבורי מתוך Telegram באייפון.
- עומס וריבוי משתמשים production.

אלו דורשים credentials, מחשב Windows פעיל, רשת או תשתית שאינם חלק מה־ZIP.
## REC-PROGRAM-04 validation — 2026-06-27

- `C:\Users\user\anaconda3\python.exe -m compileall -q coach_bot.py noam_coach`: exit code 0.
- `C:\Users\user\anaconda3\python.exe -m ruff check .`: exit code 0.
- `C:\Users\user\anaconda3\python.exe -m pytest --maxfail=0 -ra`: exit code 0; `557 passed, 3 warnings in 30.61s`; skipped 0; xfailed 0.
- `C:\Users\user\anaconda3\python.exe scripts\run_evaluations.py`: exit code 0; 33/33 passed.
- `C:\Users\user\anaconda3\python.exe scripts\preflight.py --skip-runtime-secrets`: exit code 0.
- Bounded startup smoke: reached Telegram app start, scheduler start, bot identity, FastAPI/Uvicorn startup; no traceback.
- Release build: `dist\noam_coach_2.0.0-rc5.zip`, 169 files, 0 forbidden entries, SHA-256 `48ffab080da0b07fda606c9a108cfd5777481b177670057dc9e91ba4fd4d9fe6`.
