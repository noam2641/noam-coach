# Rebuild Report — Noam Coach 2.0.0-rc5

תאריך: 2026-06-24

## תוצאה

הקוד פורק ממבנה מונוליטי ונבנה מחדש כחבילה מודולרית, תוך שמירת ה־API ההיסטורי ותכונות הפרויקט הקיים.

## לפני ואחרי

| מדד | לפני | אחרי |
|---|---:|---:|
| `coach_bot.py` | כ־9,000 שורות | 550 שורות |
| `handle_callback` | router חלקי בתוך מונולית | 66 שורות |
| `handle_session_action_callback` | כ־800 שורות | 75 שורות |
| חבילת אפליקציה | רוב הקוד בקובץ ראשי | `noam_coach/app`, `api`, `bot`, `services`, `jobs` |
| בדיקות | 332 baseline | 351 |
| coverage | לא סונכרן בתיעוד | 67% |

## תיקונים מרכזיים

- ייבוא Apple Health תקופתי מנתיב מקומי מאובטח; HealthKit שוטף כבוי כברירת מחדל.
- מדריך Mini App ו־Cloudflare Tunnel להפעלה ללא שרת בתשלום בשלב הפיתוח.

- פיצול callbacks למשפחות פעולה ושמירת סדר routing.
- חילוץ HealthKit, Shortcuts, Watch ו־system routes.
- הפרדת runtime, UI, onboarding, meals, workouts, services ו־jobs.
- תיקון resolution של DB ב־`health_service.py` לשמירת תאימות dependency injection/monkeypatch.
- הוספת בדיקות ארכיטקטורה ו־device API.
- עדכון build כדי לכלול את חבילת `noam_coach` ולמנוע קבצים אסורים.
- סנכרון גרסה, manifest, validation, status ותיעוד ארכיטקטורה.

## אימות

- 351 tests passed.
- 33/33 evaluations passed ב־11 קטגוריות.
- Ruff passed.
- compileall passed.
- preflight passed ללא runtime secrets.
- release ZIP נבדק: 160 קבצים ו־0 forbidden entries.

## מגבלות חיצוניות

לא בוצעו קריאות production ל־Telegram/OpenAI, לא נבדקו Telegram/OpenAI production, נתיב Windows אמיתי מתוך הצ'אט או HTTPS ציבורי באייפון. פריטים אלה דורשים credentials, מחשב Windows פעיל ורשת חיצונית.
