# ChatGPT PATCH-02 — five forward steps

תאריך: 2026-07-07

הפאץ׳ הזה הוא מצטבר: הוא כולל גם את PATCH-01 של למידת מאכלים/מוצרים וגם את חמשת שלבי ההמשך כאן. אפשר לחלץ את ה-ZIP מעל תיקיית האב של הבוט, ואין צורך להוריד ולאחד חלקים קודמים.

## חמשת השלבים שבוצעו

### STEP-01 — למידת מאכלים ומוצרים מארוחות מאושרות
- נוסף `noam_coach/services/learned_foods.py`.
- ניתוח תמונה/טקסט מקבל context של פריטים שהמשתמש כבר העלה ואישר.
- `nutrition_context` כולל `learned_foods`.
- `מה לאכול עכשיו` נותן בונוס קטן להצעות שמכילות פריטים מוכרים, אחרי בדיקות תקציב/מגבלות.
- תפריט יומי fallback יכול לשלב פריטים שחוזרים אצל המשתמש.

### STEP-02 — HealthKit לא דורס ימי אימון ידניים
- נוספו facts מפורשים ב-`user_model.py`:
  - `detected_training_days`
  - `preferred_training_days`
  - `active_training_days`
- `resolve_availability()` קורא קודם `active_training_days` ו-`preferred_training_days`, לפני הסקה מ-HealthKit.
- אם HealthKit מזהה ימים אחרים, הם נשמרים כזיהוי/קונפליקט, אבל הערך הידני נשאר הפעיל.

### STEP-03 — שמירת זמינות ידנית כמקור אמת פעיל
- נוסף helper `save_user_training_availability()` ב-`availability.py`.
- תשובה חופשית כמו `ראשון שני רביעי שישי` שומרת גם:
  - `weekly_availability`
  - `training_days_per_week`
  - `preferred_training_days`
  - `active_training_days`
- גם תיקון ידני בתוך אשף HealthKit מעדכן את הימים הפעילים.

### STEP-04 — ולידציית משקל יעד חריג
- נוסף `noam_coach/services/goal_validation.py`.
- יעד כמו 50 ק״ג מול משקל נוכחי 101.8 ק״ג לא נשמר אוטומטית.
- הבוט מבקש אישור מפורש בנוסח `כן 50` או יעד אחר.
- יעד סביר כמו 83 ק״ג ממשיך להישמר רגיל.

### STEP-05 — ניקוי הצגת זמינות בפרופיל
- `format_availability_summary()` מציג עכשיו `ימים פעילים לתוכנית` במקום ניסוח עמום.
- מוצג מקור קצר: `עודכן ידנית`, `זוהה מ־HealthKit ואושר`, `זוהה מ־HealthKit — דורש אישור`, או `ברירת מחדל`.
- אם יש קונפליקט בין HealthKit לידני, הפרופיל מציין ש-HealthKit הציע ימים אחרים אבל הערך הידני פעיל.

## קבצים שנוספו
- `noam_coach/services/learned_foods.py`
- `noam_coach/services/goal_validation.py`
- `tests/regression/test_learned_foods_personalization.py`
- `tests/regression/test_goal_weight_validation.py`
- `CHATGPT_PATCH_REPORT.md`
- `CHATGPT_PATCH_02_REPORT.md`

## קבצים שעודכנו
- `noam_coach/bot/assistant.py`
- `noam_coach/bot/onboarding.py`
- `noam_coach/services/availability.py`
- `noam_coach/services/health_jobs.py`
- `noam_coach/services/next_meal.py`
- `noam_coach/services/nutrition_context.py`
- `noam_coach/services/profile.py`
- `recommendations.py`
- `user_model.py`
- `tests/test_availability_parser.py`

## אימות שבוצע בסביבה הזו
עבר:

```bash
python -m compileall -q \
  noam_coach/services/availability.py \
  noam_coach/services/goal_validation.py \
  noam_coach/bot/onboarding.py \
  noam_coach/services/health_jobs.py \
  noam_coach/bot/assistant.py \
  user_model.py \
  tests/test_availability_parser.py \
  tests/regression/test_goal_weight_validation.py \
  tests/regression/test_learned_foods_personalization.py
```

וגם smoke test קצר ל-`goal_validation` ול-parser של זמינות אימונים עבר.

לא הורץ `pytest` מלא ולא `ruff` מלא בסביבה הזו כי חסרות תלויות כמו `openai` ו-`ruff`.

## בדיקות להרצה אצלך אחרי חילוץ

```powershell
python -m ruff check noam_coach/services/learned_foods.py noam_coach/services/goal_validation.py noam_coach/services/availability.py noam_coach/services/health_jobs.py noam_coach/bot/onboarding.py noam_coach/bot/assistant.py noam_coach/services/next_meal.py noam_coach/services/nutrition_context.py noam_coach/services/profile.py recommendations.py user_model.py tests/regression/test_learned_foods_personalization.py tests/regression/test_goal_weight_validation.py tests/test_availability_parser.py

python -m pytest -p no:cacheprovider -q tests/regression/test_learned_foods_personalization.py tests/regression/test_goal_weight_validation.py tests/test_availability_parser.py
```

אחרי זה מומלץ להריץ full suite.

## מה עדיין לא סומן כסגור
- `TASK-07/08` — ExerciseCatalog מלא + Substitution Engine מקצועי עמוק: קיימת תשתית חלקית, אבל זה עדיין פרויקט גדול נפרד.
- `TASK-05/06` — תפריט יומי נעיץ ואיחוד מלא של כל מסכי התפריט: לא נפתח בפאץ׳ הזה.
- `TASK-09` — נראה שכבר קיימת תשתית עריכת פרמטרים עם אישור, אבל לא סימנתי כסגור בלי הרצת בדיקות מלאה אצלך.
