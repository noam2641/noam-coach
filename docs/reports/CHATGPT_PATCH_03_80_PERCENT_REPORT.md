# CHATGPT PATCH-03 — 80% Task Coverage Pass

מטרה: להמשיך 5 שלבים קדימה על בסיס העבודה של Claude והפאצ׳ים הקודמים, בלי לפתוח Refactor ענק או לשבור את קווי ה־Flow המרכזיים.

## מה נכנס בפועל ב-PATCH-03

### STEP-01 — TASK-03 סגירה חזקה יותר: “מה לאכול עכשיו” כהמלצה אחת
- `next_meal_action_rows()` כבר לא מוסיף כפתורי סטטוס אימון למסך הראשי, כדי לא לעבור 4 פעולות.
- המסך נשאר עם הפעולות: אשר שאכלתי, רענן הצעה, שנה כמויות, חזור לסיכום היום.
- מסך בחירה ישן `nextmeal:choose` הוסר ממנו כפתור “תכנן להמשך”, כדי שגם callback ישן לא יחזיר את ה־UX הישן.
- נוסח הרענון שונה מ“הצעות” ל“הצעה”.

### STEP-02 — TASK-05 בסיס מעשי: תפריט יומי כהודעה עצמאית
- `build_morning_menu_text()` הותאם כך שיחזיר הודעת “תפריט להיום” עצמאית, Pin-friendly, ולא הודעת בוקר ארוכה שמשלבת מצב/Brief/תפריט יחד.
- הודעת הצ׳ק־אין בבוקר כבר נשלחת בנפרד ב־`job_morning`; לכן התפריט עצמו כעת נקי יותר.
- נוספו כפתורים רלוונטיים לתפריט: רענן תפריט, מה לאכול עכשיו, החלף ארוחה, מצב היום, תפריט.

### STEP-03 — TASK-06 בסיס מעשי: איחוד UX של תפריט
- `menu:daily_menu` נוסף ככיוון מוצר ברור ל“תפריט להיום”.
- `menu:today` ו־`menu:morning` נשארו alias כדי שכפתורים ישנים לא ימותו.
- כפתור הבית עודכן ל־`menu:daily_menu` במקום `menu:morning`.
- `menu:replace_daily_meal` לא מת יותר בשקט; הוא פותח מסך הנחיה ומפנה למסלולי פעולה קיימים.

### STEP-04 — TASK-07/TASK-11: תוכנית אימון משתמשת בימים הפעילים ולא מוחקת יום ידני
- `build_weekly_plan()` משתמש כעת ב־`resolve_availability()` במקום להסתמך רק על זיהוי HealthKit/שגרה.
- אם המשתמש הגדיר ידנית ראשון/שני/רביעי/שישי, אלו הימים שמשמשים לבניית התוכנית.
- HealthKit נשאר מקור הצעה/זיהוי, לא מקור שדורס את המשתמש.
- התוכנית שומרת `structure`, `days_source`, ו־`availability_confirmed` לצורך שקיפות.
- תצוגת התוכנית מציגה את מבנה התוכנית, למשל Full Body / A/B/C / A/B/C + Full Body.

### STEP-05 — TASK-08/TASK-09: קטלוג תרגילים ועריכת פרמטרים רחבה
- `ExerciseProfile` הורחב עם metadata: movement pattern, muscles, equipment, joint_load, regressions, progressions, cues.
- נוספו פונקציות:
  - `exercise_catalog_entry(exercise_id)`
  - `substitutions_for_exercise(...)`
- חלופות מסוננות לפי ציוד וכאב פעיל.
- עריכת פרמטרים בטקסט תומכת עכשיו גם ב־“לכל התוכנית”, בנוסף ל־“לכל התרגילים” ולתרגיל הנוכחי.
- שינוי גלובלי מציג כמה תרגילים הושפעו אחרי האישור.

## סטטוס 14 המשימות אחרי PATCH-03

| Task | סטטוס | הערה |
|---|---|---|
| TASK-01 | Done | Flow תזונה נשאר בתחום תזונה. |
| TASK-02 | Done/Needs E2E | מנגנון שאלות חסרות לפי תחום + skipped state. |
| TASK-03 | Done | הצעה אחת, עד 4 פעולות, בלי plan button במסך המיידי. |
| TASK-04 | Done | consumed בלבד נספר; recommended/planned לא נספרות. |
| TASK-05 | Functional baseline | תפריט יומי כהודעה עצמאית + כפתורים. עריכה חכמה של ארוחה ספציפית עדיין בסיסית. |
| TASK-06 | Functional baseline | menu:daily_menu ו־aliases. נדרש בהמשך מיפוי מלא לכל callback ישן. |
| TASK-07 | Functional baseline | הימים הידניים נשמרים, מבנה מוצג. Plan generator מקצועי עמוק עדיין דורש שיפור. |
| TASK-08 | Functional baseline | קטלוג metadata וחלופות קיימים. קטלוג מלא לכל תרגיל/contraindications עדיין עבודה כבדה. |
| TASK-09 | Done/Needs tests | תרגיל/כל האימון/כל התוכנית נתמכים עם אישור. |
| TASK-10 | Partial | פרופיל מציג זמינות פעילה ומקור. ניקוי מלא של כל הפרופיל עדיין פתוח. |
| TASK-11 | Done | active/preferred/detected training days + שימוש ב־resolve_availability בתוכנית. |
| TASK-12 | Done | Claude כבר סגר stale/network callbacks. |
| TASK-13 | Partial/Functional | ולידציית יעד חריג קיימת באונבורדינג. צריך לחבר לכל מסלולי goal העתידיים. |
| TASK-14 | Done | Claude סגר post-meal status קצר. |
| Learned Foods | Functional baseline | הבוט לומד מארוחות שאושרו ומשתמש בזה ב-analysis/next meal/menu. |

Coverage מוצרית: 12 מתוך 14 משימות עם מימוש מלא או baseline פונקציונלי. המשימות היחידות שלא הייתי מסמן כ“סגורות עמוק” הן TASK-08 ו־TASK-10, ובמידה מסוימת TASK-07/TASK-13 דורשות הרחבה עתידית.

## בדיקות שבוצעו כאן

עבר:
- `py_compile` לקבצי הקוד ששונו.

לא עבר בגלל סביבת הכלי ולא בגלל קוד:
- `pytest` נעצר מיד בגלל חוסר תלות `aiosqlite` בסביבה הזו.
- לכן חובה להריץ בסביבת הפרויקט שלך את הבדיקות למטה.

## פקודות בדיקה מומלצות אחרי חילוץ

```powershell
python -m ruff check noam_coach/services/learned_foods.py noam_coach/services/goal_validation.py noam_coach/services/availability.py noam_coach/services/health_jobs.py noam_coach/bot/onboarding.py noam_coach/bot/assistant.py noam_coach/bot/callback_menu.py noam_coach/bot/callback_plans.py noam_coach/bot/meal_text.py noam_coach/bot/ui.py noam_coach/services/next_meal.py noam_coach/services/nutrition_context.py noam_coach/services/profile.py recommendations.py user_model.py training_intelligence.py

python -m pytest -p no:cacheprovider -q tests/acceptance/test_rec_next_meal_05.py tests/regression/test_recording_20260628_re8.py tests/test_next_meal_callbacks.py tests/test_availability_parser.py tests/regression/test_goal_weight_validation.py tests/regression/test_learned_foods_personalization.py tests/test_exercise_plan_quality.py
```

ואז:

```powershell
python -m pytest -p no:cacheprovider -q
```

## מה עדיין לא לסגור בכוח

1. TASK-08 כקטלוג תרגילים מלא לכל התרגילים + contraindications מפורטות — יש baseline, לא מוצר מושלם.
2. TASK-10 ניקוי מלא של כל הפרופיל — יש שיפור זמינות/מקור, לא מעבר עיצוב מלא.
3. תפריט יומי עם עריכת ארוחה ספציפית טבעית מלאה — כרגע יש מסך הנחיה/רענון, לא מנגנון עריכה מלא לכל ארוחה.
