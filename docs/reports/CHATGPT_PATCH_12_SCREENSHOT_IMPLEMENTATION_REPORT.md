# CHATGPT PATCH-12 — Screenshot implementation pass

תאריך: 2026-07-08

## למה הפאץ׳ הזה קיים
המשתמש הראה שוב צילומי מסך חיים מטלגרם שבהם עדיין הופיעו תקלות UX ותפקוד:

- הודעת Telegram טכנית גולמית בצ׳אט + הודעת fallback כפולה.
- פרופיל שנראה כמו debug: הרבה שורות, מקורות כפולים, זמינות סותרת.
- ״מה לאכול עכשיו״/מסכי תזונה שעדיין היו מבלבלים בחלק מהמסלולים.
- מסך ״שלוש הצעות תזונה״ שנראה כמו תפריט יומי במקום בחירת סגנון חד־פעמית.
- HealthKit/אונבורדינג ששאל שוב שאלות או עבר למסלול לא נכון.
- תוכנית אימון שעדיין עלולה להיראות גנרית או להפחית יום אימון כשמבקשים ABC.
- תצוגת תוכנית שבועית שהייתה קצרה מדי ולא הציגה סטים/חזרות/מנוחה/RIR/דגשים.

## קבצים ששונו

- `noam_coach/bot/onboarding.py`
- `noam_coach/bot/assistant.py`
- `noam_coach/bot/callback_router.py`
- `tests/regression/test_patch12_screenshot_regressions.py`

## תיקונים בפועל

### 1. `/profile` כבר לא debug dump
`build_profile_text()` נבנה מחדש:

- מחולק לקטגוריות: יעד וגוף, תזונה, אימונים, זמינות פעילה, מגבלות, מוכנות.
- לא מציג ״דווח על ידך״ בכל שורה.
- לא מציג גם `weekly_availability` וגם `preferred_training_days` בצורה סותרת.
- משתמש ב־`resolve_availability()` כמקור אחד לזמינות הפעילה.
- אם יש קונפליקט HealthKit מול ידני — מוצג ש־HealthKit הוא רמז בלבד והידני הוא המקור הפעיל.

### 2. סיום Flow תזונה לא זורק לאימונים
נוספה `render_plan_completion_done()`.

כאשר Flow תזונה מסתיים:

- הבוט לא עובר לשאלות אימון.
- מוצגים רק כפתורי המשך תזונה: תפריט יומי, מה לאכול עכשיו, עדכון יעד, חזרה לתוכניות.

כאשר Flow אימונים מסתיים:

- מוצג המשך אימונים ייעודי.

### 3. בקשת ABC לא מפילה יום אימון
`requested_split_frequency()` עודכן:

- בקשת ABC חופשית כבר לא מניחה 3 ימים בלבד.
- ברירת המחדל היא 4 ימים: ABC + Full Body.
- כך בקשה כמו ״אני רוצה ABC״ לא מוחקת יום ראשון אם המשתמש הזין ראשון/שני/רביעי/שישי.

### 4. תוכנית שבועית מוצגת מקצועית יותר
`format_weekly_plan()` כבר לא מציג רק שמות ימים/אימונים.

לכל יום מוצגים:

- תרגילים מרכזיים.
- סטים.
- טווח חזרות.
- מנוחה.
- RIR.
- דגש טכני קצר.

### 5. לא שולחים raw Telegram errors לצ׳אט
`on_error()` ב־`callback_router.py` לא שולח יותר למנהל/משתמש הודעה עם:

- `Telegram error (...)`
- `safe_error`
- traceback או טקסט חריגות טכני.

הוא שולח הודעה ידידותית בלבד, והפרטים נשארים בלוגים.

### 6. מסך שלוש הצעות תזונה קיבל היררכיה ברורה
`render_candidate_list()` עבור nutrition שונה ל:

- ״בחירת סגנון תזונה — חד־פעמי״.
- הסבר שזה לא תפריט יומי.
- כפתור ״בחר סגנון״ במקום ״בחר הצעה״.

## בדיקות שנוספו

- `tests/regression/test_patch12_screenshot_regressions.py`

הבדיקה מכסה:

- הפרופיל לא debug dump.
- סיום Nutrition completion חוזר לפעולות תזונה בלבד.
- בקשת ABC לא מפילה 4 ימי אימון.
- תצוגת תוכנית שבועית כוללת RIR/מנוחה/דגש/תרגילים.
- Telegram errors לא נשלחים גולמיים לצ׳אט.
- מסך הצעות תזונה אינו מתחזה לתפריט יומי.

## אימות שבוצע כאן

```powershell
python -m compileall -q noam_coach/bot/onboarding.py noam_coach/bot/assistant.py noam_coach/bot/callback_router.py tests/regression/test_patch12_screenshot_regressions.py
python -m pytest -p no:cacheprovider -q tests/regression/test_patch12_screenshot_regressions.py tests/regression/test_patch11_remaining_contracts.py tests/regression/test_backlog_14_contracts.py
```

תוצאה:

```text
19 passed
```

## מה עדיין דורש בדיקה חיה בטלגרם

- שיחה חופשית אחרי שאלה שחזרה פעמיים בעבר — לוודא שב־DB אמיתי אין state ישן שתוקע את השאלה.
- עריכת תפריט יומי נעוץ בפועל עם `daily_menu_message_id`.
- אימון בפועל מקצה לקצה: החלפת תרגיל, כאב, מנוחה לכל התרגילים.
- הרחבת ExerciseCatalog לרמה מלאה של מוצר, מעבר לקטלוג הבסיסי הקיים.

## פקודות מומלצות אחרי חילוץ

```powershell
python -m pytest -p no:cacheprovider -q tests/regression/test_patch12_screenshot_regressions.py
python -m pytest -p no:cacheprovider -q tests/regression/test_patch11_remaining_contracts.py tests/regression/test_backlog_14_contracts.py tests/regression/test_patch10_meal_status.py tests/regression/test_re13_health_quality.py tests/regression/test_learned_foods_personalization.py tests/regression/test_goal_weight_validation.py tests/test_availability_parser.py tests/test_next_meal_callbacks.py
python -m pytest -p no:cacheprovider -q
```
