# ארכיטקטורת Noam Coach — 2.0.0-rc5

## מטרת המבנה

הגרסה הנוכחית מפרידה את שכבת ההרכבה, ה־Telegram UI, נתיבי ה־HTTP, השירותים והעבודות המתוזמנות. `coach_bot.py` נשאר נקודת כניסה ותאימות לאחור בלבד; המימוש הפעיל נמצא בחבילת `noam_coach`.

## מפת מודולים

```text
coach_bot.py                    composition root + compatibility facade
noam_coach/
  app/runtime.py                בניית Telegram app, startup/shutdown והרצת השרת
  api/security.py               auth, rate limits ומגבלת גוף בקשה
  api/system_routes.py          /healthz ו-/readyz
  api/health_routes.py          HealthKit/Shortcuts אופציונליים (כבויים כברירת מחדל)
  api/watch_routes.py           Apple Watch current/set אופציונלי
  api/mini_auth.py              login/session של ה-Mini App
  bot/ui.py                     keyboards, callback builders ורינדור UI
  bot/onboarding.py             onboarding ופרופיל
  bot/workout.py                תכנון והצגת אימונים
  bot/workout_runtime.py        ניהול סשן אימון פעיל
  bot/meals.py                  תמונות, ניתוח ושמירת ארוחות
  bot/meal_text.py              ניתוב טקסט ותיקוני ארוחה
  bot/callback_router.py        dispatcher מרכזי קצר
  bot/callback_menu.py          callbacks של תפריטים
  bot/callback_plans.py         callbacks של תוכניות והגדרת אימון
  bot/callback_meals.py         callbacks של ארוחות והבהרות
  bot/callback_session.py       callbacks של סשן אימון, מחולקים למשפחות פעולה
  bot/checkins.py               דגלים, צ'ק-אין ומעקב
  bot/assistant.py              הודעות סיכום והמלצות שיחתיות
  services/core.py              משתמש, flow, approvals, audit ואדמין
  services/profile.py           פרופיל, שגרה וניתוח ארוחה
  services/training.py          חישובי אימון, RIR, fatigue והמלצת עומס
  services/goals.py             יעדים וסטטוסים
  services/health_jobs.py
  services/local_health_path.py  אימות ובחירת ZIP/XML מנתיב מקומי       מסמכי Health ועבודות קליטה
  jobs/proactive.py             הודעות מתוזמנות, retry ו-delivery claims
  runtime_bind.py               גשר תאימות זמני ל-API ההיסטורי של coach_bot
mini_api.py                     נתיבי Mini App העסקיים
miniapp/                        HTML, JavaScript ו-CSS נפרדים
```

## כיוון תלויות

1. `coach_bot.py` מרכיב ומייצא שמות היסטוריים.
2. `noam_coach.app`, `noam_coach.api` ו־`noam_coach.bot` משתמשים בשירותים ובמודולי domain הקיימים.
3. שירותים משתמשים ב־repositories/DB ובמודולי domain, אך אינם מרנדרים Telegram UI.
4. נתיבי API ו־handlers מתרגמים קלט, מבצעים validation וקוראים לשירותים.
5. `runtime_bind` קיים לשמירת תאימות לבדיקות ול־monkeypatching ישן; קוד חדש צריך להעדיף dependencies מפורשים.

## הוספת callback

1. בחר namespace קיים או צור מודול callback חדש תחת `noam_coach/bot/`.
2. כתוב handler ממוקד שמחזיר `True` כאשר טיפל ב־callback ו־`False` אחרת.
3. חבר אותו פעם אחת ב־`callback_router.py`.
4. הוסף callback builder ב־`ui.py` במקום concatenation חופשי.
5. הוסף בדיקת routing, stale callback ו־double tap.

## הוספת endpoint

1. בחר router תחת `noam_coach/api/`.
2. החל security dependencies מ־`security.py`.
3. שמור business logic בשירות ולא בתוך endpoint.
4. רשום את ה־router ב־composition root פעם אחת בלבד.
5. הוסף בדיקות FastAPI ל־success, auth, validation, idempotency ושגיאה.

## הוספת שירות

- פונקציות domain טהורות אינן מייבאות Telegram/FastAPI.
- שירות מקבל dependencies מפורשים כאשר ניתן.
- DB mutation משמעותי מתבצע בתוך transaction.
- פעולות שניתנות לחזרה מקבלות idempotency key או constraint מתאים.

## תאימות לאחור

`coach_bot.py` ממשיך לייצא שמות שבהם משתמשים deployment scripts ובדיקות ישנות. אין להוסיף אליו לוגיקה עסקית חדשה. re-export חדש צריך בדיקת תאימות והצדקה מפורשת.
