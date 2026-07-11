# CHATGPT PATCH-10 — 80%+ backlog implementation hardening

מטרה: להפסיק פאצ׳ים נקודתיים ולחזק את הדרישות המרכזיות מהרשימה כ-contract מחובר: תזונה, תפריט יומי, ארוחות מתוכננות/נאכלות, HealthKit מול דיווח ידני, פרופיל, אימונים, ו-error handling.

## מה נוסף מעבר ל-PATCH-09

### 1. TASK-04 — סטטוס ארוחה אמיתי ב-DB
נוסף `status` לטבלת `meals` עם ברירת מחדל `consumed`, ומיגרציה `11 meal_status`.

משמעות:
- שורות קיימות עוברות ל-`consumed` כי הן נוצרו רק אחרי אישור משתמש.
- אפשר לשמור בעתיד `planned` / `recommended` בלי שייספרו בקלוריות.
- חישובי `daily_state`, `nutrition_context`, `planning.adherence_snapshot`, `assistant`, `mini_api`, `data_quality` מסננים ל-`consumed` בלבד.

### 2. TASK-05 — תפריט יומי כהודעה עצמאית שניתן לנעוץ
נוסף שירות `noam_coach/services/daily_menu_state.py` ששומר את `daily_menu_message_id` ב-`daily_flags`.

הבוט כעת:
- שולח את תפריט היום כהודעה עצמאית ב-job הבוקר.
- שומר את message_id של תפריט היום.
- בלחיצה על `menu:daily_menu` שולח הודעה עצמאית חדשה שאפשר לנעוץ, ולא רק מחליף את מסך התפריט.

### 3. TASK-03/05/06 — היררכיית תזונה נקייה יותר
`menu:daily_menu` הוא כניסת התפריט היומי המפורשת. `menu:morning` נשאר כתאימות לאחור, אך משמעות המוצר היא “תפריט להיום”.

### 4. חוזי בדיקה חזקים יותר
עודכן `tests/regression/test_backlog_14_contracts.py` כך שיבדוק גם:
- מיגרציית meal status.
- סינון consumed-only.
- שמירת daily_menu_message.
- שליחת תפריט עצמאי בלחיצה.

נוספה בדיקה חדשה:
- `tests/regression/test_patch10_meal_status.py`

היא בודקת שמסד הנתונים כולל `status`, ושארוחה `planned` לא נספרת בסך הקלוריות/חלבון היומי.

## בדיקות שבוצעו כאן

עבר:
```text
python -m compileall -q db.py noam_coach/services/daily_menu_state.py noam_coach/services/health_jobs.py noam_coach/bot/callback_menu.py noam_coach/services/daily_state.py noam_coach/services/nutrition_context.py planning.py noam_coach/bot/assistant.py mini_api.py data_quality.py
python -m pytest -p no:cacheprovider -q tests/regression/test_backlog_14_contracts.py
```

לא הורץ כאן:
```text
full pytest
ruff
```
בגלל תלות חסרה בסביבה (`openai`, `ruff`). אצל המשתמש הסביבה כבר הריצה full pytest קודם, ולכן צריך להריץ שוב אחרי החילוץ.

## סטטוס לפי 14 המשימות אחרי PATCH-10

סגור/מיושם ברמת קוד + contract:
- TASK-01 — תזונה לא קופצת לכושר.
- TASK-02 — dedupe שאלות קיימות, עדיין דורש בדיקת Telegram חיה מקצה לקצה.
- TASK-03 — “מה לאכול עכשיו” הצעה אחת ועד 4 כפתורים.
- TASK-04 — הפרדה חזקה יותר ב-DB בין consumed/planned/recommended.
- TASK-05 — תפריט יומי עצמאי עם message_id.
- TASK-06 — היררכיית תפריטים ברורה יותר.
- TASK-10 — פרופיל נקי יותר, ערך זמינות פעיל אחד.
- TASK-11 — HealthKit לא גובר על ימים ידניים.
- TASK-12 — callback/network errors עטופים.
- TASK-13 — ולידציית יעד חריג.
- TASK-14 — post-meal קצר וממוקד.

מיושם בסיסית אבל עדיין לא “100% מוצר סופי”:
- TASK-07 — תוכנית אימונים 4 ימים + splits, אך צריך בדיקת Telegram חיה על תוכנית אמיתית.
- TASK-08 — ExerciseCatalog/Pain/Substitution בסיסי, לא קטלוג מלא לכל תרגיל.
- TASK-09 — עריכת פרמטרים עם scope ואישור, צריך בדיקת אימון חי.

## המלצת המשך

אחרי חילוץ והרצת full pytest, צריך לעשות בדיקת Telegram חיה לפי script קצר:
1. `/profile` — לוודא שאין סתירות ימים ואין “דיווח שלך” בכל שורה.
2. HealthKit ישן — אם יש ימים ידניים, לא לשאול שוב 2/3/4 ולא לדרוס.
3. `מה לאכול עכשיו` — הצעה אחת בלבד, 4 כפתורים.
4. אישור תמונת אוכל — הודעה קצרה.
5. `תפריט להיום` — הודעה עצמאית שאפשר לנעוץ.
6. בניית תוכנית אימון — 4 ימים נשמרים.
7. עריכת מנוחה “לכל התרגילים” — דורשת אישור ומראה כמה הושפעו.
