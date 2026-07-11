# CHATGPT PATCH-04 — Flow/Integration Audit + Professional Cleanup

מטרה: לוודא שהפאצ׳ים הקודמים באמת מחוברים ל־flows המרכזיים, לא רק קיימים כקוד, ולהוסיף תיקונים קטנים שמונעים UX לא עקבי.

## מה נבדק

נבדקו חוזי המוצר המרכזיים מתוך ה־Backlog/תמונות:

1. `מה לאכול עכשיו` צריך להיות המלצה אחת, עם עד 4 פעולות, בלי “אפשרות 1/2” ובלי “תכנן להמשך”.
2. אחרי אישור תמונת אוכל צריך להופיע `post-meal day status` קצר, לא סיכום יום מלא.
3. תפריט יומי צריך להיות entrypoint ברור אחד: `menu:daily_menu`, עם aliases ישנים שלא נשברים.
4. ימי אימון ידניים צריכים לגבור על HealthKit ולשמש את בניית התוכנית.
5. למידת מאכלים/מוצרים צריכה להיות מחוברת גם לניתוח אוכל, גם ל־NutritionContext, גם ל־Next Meal וגם לתפריט יומי fallback.
6. קטלוג התרגילים צריך לחשוף metadata מקצועי בסיסי וחלופות לפי ציוד/כאב.

## תיקון קטן שנוסף בפועל

### ניקוי ניסוח ב־`מה לאכול עכשיו`
ב־`noam_coach/services/next_meal.py` היה טקסט שאמר:

> “אפשר לעדכן בכפתורים”

אבל לפי TASK-03 הסרנו את כפתורי סטטוס האימון ממסך “מה לאכול עכשיו” כדי להישאר עד 4 פעולות. לכן הניסוח עודכן ל:

> “אפשר לעדכן את סטטוס האימון דרך מצב היום.”

זה מונע מצב שבו הבוט מפנה לכפתורים שכבר לא קיימים במסך.

## בקרת חיבור שנוספה

נוסף קובץ בדיקות סטטי:

`tests/regression/test_chatgpt_patch04_flow_contracts.py`

הבדיקה לא דורשת Telegram / OpenAI / aiosqlite ולכן רצה גם בסביבה חלקית. היא מוודאת ברמת קוד מקור:

- `next_meal` חושף הצעה אחת בלבד.
- אין טקסטי `אפשרות 1/2` או `תכנן אפשרות` במסך המיידי.
- `menu:daily_menu` מחובר ומחליף את פיזור התפריטים הישן.
- אישור ארוחה משתמש ב־`render_post_meal_confirmation_day_status` ולא ב־`build_daily_status` הארוך.
- `active_training_days`, `preferred_training_days`, `detected_training_days` קיימים ומחוברים.
- `learned_foods` מחובר לניתוח, context, next meal ותפריט.
- `training_intelligence` חושף metadata מקצועי וחלופות.

## בדיקות שהורצו כאן

עברו:

```bash
python -m compileall -q noam_coach/bot noam_coach/services recommendations.py training_intelligence.py user_model.py tests/regression/test_chatgpt_patch04_flow_contracts.py
python -m pytest -q tests/regression/test_chatgpt_patch04_flow_contracts.py -q
```

תוצאה:

- `compileall_ok`
- `6/6` בדיקות PATCH-04 עברו

לא הורץ כאן full suite בגלל חוסרים בסביבה (`aiosqlite`, `telegram`, `apscheduler`, `ruff`). בסביבת הפרויקט שלך חובה להריץ את הפקודות בסוף המסמך.

## סטטוס משימות אחרי PATCH-04

| Task | סטטוס בקרה |
|---|---|
| TASK-01 | מחובר — Flow תזונה לא אמור ליפול לשאלות אימון. |
| TASK-02 | מחובר חלקית־טוב — עדיין מומלץ E2E מלא מול onboarding. |
| TASK-03 | מחובר ונבדק סטטית — הצעה אחת + 4 פעולות. |
| TASK-04 | מחובר לפי בדיקות קיימות — consumed בלבד נספר. |
| TASK-05 | baseline מחובר — תפריט יומי כ־`menu:daily_menu`. |
| TASK-06 | baseline מחובר — aliases קיימים, אבל מיפוי מלא של כל היסטוריית callbacks עדיין מומלץ. |
| TASK-07 | baseline מחובר — `build_weekly_plan` משתמש ב־`resolve_availability`. |
| TASK-08 | baseline מקצועי — metadata + substitutions קיימים; לא קטלוג מושלם לכל תרגיל. |
| TASK-09 | baseline מחובר — scope current/all/program קיים. נדרש full pytest בסביבה שלך. |
| TASK-10 | חלקי — שופרה זמינות/מקור, אבל עיצוב פרופיל מלא עדיין לא סגור. |
| TASK-11 | מחובר — active/preferred/detected training days. |
| TASK-12 | תלוי בעבודת Claude, לא שונה בפאץ׳ הזה. |
| TASK-13 | baseline מחובר — ולידציה במסלולי onboarding/assistant. |
| TASK-14 | מחובר — post-meal renderer קצר + 4 כפתורים. |
| Learned Foods | מחובר — ניתוח/Context/NextMeal/Menu. |

## מה עדיין לא הייתי מסמן כ־100% סגור

1. **TASK-08 קטלוג תרגילים מלא** — יש תשתית טובה, אבל לא ספריית תרגילים מלאה עם contraindications לכל תרגיל.
2. **TASK-10 פרופיל מלא** — צריך מעבר UX מלא על כל הפרופיל, לא רק זמינות.
3. **TASK-05 עריכת ארוחה ספציפית בתפריט** — יש תפריט עצמאי ונקודת החלפה, אבל לא מנגנון עריכה טבעית מלא לכל ארוחה בתפריט.
4. **Idempotency ברמת DB לכל כתיבה** — debounce קיים, אבל registry עמיד לאירועי כתיבה עדיין נדרש בעתיד.

## פקודות בדיקה מומלצות אחרי חילוץ

```powershell
python -m ruff check noam_coach/services/learned_foods.py noam_coach/services/goal_validation.py noam_coach/services/availability.py noam_coach/services/health_jobs.py noam_coach/bot/onboarding.py noam_coach/bot/assistant.py noam_coach/bot/callback_menu.py noam_coach/bot/callback_plans.py noam_coach/bot/meal_text.py noam_coach/bot/ui.py noam_coach/services/next_meal.py noam_coach/services/nutrition_context.py noam_coach/services/profile.py recommendations.py user_model.py training_intelligence.py tests/regression/test_chatgpt_patch04_flow_contracts.py

python -m pytest -p no:cacheprovider -q tests/regression/test_chatgpt_patch04_flow_contracts.py tests/acceptance/test_rec_next_meal_05.py tests/regression/test_recording_20260628_re8.py tests/test_next_meal_callbacks.py tests/test_availability_parser.py tests/regression/test_goal_weight_validation.py tests/regression/test_learned_foods_personalization.py tests/test_exercise_plan_quality.py

python -m pytest -p no:cacheprovider -q
```
