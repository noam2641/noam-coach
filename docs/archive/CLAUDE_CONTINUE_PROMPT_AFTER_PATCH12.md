אתה נכנס לפרויקט noam_coach_complete_release אחרי PATCH-01 עד PATCH-12 שבוצעו על ידי ChatGPT. המשתמש כועס ובצדק כי חלק מהתיקונים הקודמים היו נקודתיים מדי. עכשיו אתה חייב לבצע בקרה אמיתית מול צילומי המסך וה־Backlog, לא להאמין לדוחות.

כללי ברזל:
1. אל תעשה git reset / stash / revert / push / מחיקה בלי אישור.
2. אל תעשה git add .
3. אל תסמן שום TASK כ־Done בלי קוד production מחובר + test או בדיקת Telegram חיה מתועדת.
4. אם full pytest נכשל — תקן את הכשל לפני המשך פיצ׳רים.
5. אל תחשוף למשתמש stacktrace, Telegram errors, safe_error, dict/raw JSON, או state פנימי.
6. כל שינוי חייב לשמור עברית ברורה, פעולה ראשית אחת, ומקסימום 4 כפתורים במסכי פעולה.

שלב 1 — אימות מידי:

```powershell
git status --short
git diff --stat
python -m pytest -p no:cacheprovider -q tests/regression/test_patch12_screenshot_regressions.py
python -m pytest -p no:cacheprovider -q tests/regression/test_patch11_remaining_contracts.py tests/regression/test_backlog_14_contracts.py tests/regression/test_patch10_meal_status.py tests/regression/test_re13_health_quality.py tests/regression/test_learned_foods_personalization.py tests/regression/test_goal_weight_validation.py tests/test_availability_parser.py tests/test_next_meal_callbacks.py
python -m pytest -p no:cacheprovider -q
```

אם יש כשל — עצור ותקן מינימלית.

שלב 2 — בדיקת Telegram חיה לפי התמונות:

1. לחץ על כפתור ישן בכוונה. אסור להופיע `[id] Telegram error` או `BadRequest`. אמורה להופיע הודעה ידידותית אחת בלבד.
2. `/profile`: צריך להיראות כמו מסך מוצר, לא debug. חייב להציג יעד, משקל, גובה אם קיים, זמן יעד, ימי אימון פעילים, שעה/משך, מגבלות ומוכנות. אסור להציג זמינות סותרת.
3. HealthKit import: אם יש ימים ידניים, אסור לשאול 2/3/4 ואסור להציע לדרוס אותם עם זיהוי מהשעון.
4. Nutrition completion: אחרי רגישויות/איסורים אסור לקפוץ לשאלה על זמן אימון. צריך להציג המשך תזונה.
5. ״מה לאכול עכשיו״: הצעה אחת בלבד, עד 4 כפתורים, בלי אפשרות 1/2, בלי כפתורי אימון מיותרים.
6. תיקון המלצה בטקסט חופשי: ״בלי טורטייה״ או ״יותר חלבון״ חייב להישאר במסך ממוקד ולא להוסיף כפתורים מיותרים.
7. תפריט להיום: נשלח כהודעה עצמאית, לא כחלק מהודעת בוקר ארוכה.
8. בחירת סגנון תזונה: צריך להיות ברור שזה סגנון/אסטרטגיה חד־פעמית, לא תפריט יומי.
9. בניית אימון: אם המשתמש הזין ראשון/שני/רביעי/שישי — כל 4 הימים חייבים להופיע.
10. בקשה חופשית ״אני רוצה ABC״ לא מוחקת את יום ראשון. אם יש 4 ימים — לבנות ABC + Full Body או להציע בחירה ברורה.
11. תוכנית שבועית חייבת להציג תרגילים, סטים, חזרות, מנוחה, RIR ודגש, לא רק שם האימון.
12. ״מנוחה 1:30 לכל התרגילים״ חייבת להציג אישור שינוי גלובלי ולא להישמר כ־9-9 חזרות.
13. יעד 50 ק״ג מול 101.8 ק״ג חייב להיעצר באזהרה ולא להישמר אוטומטית.
14. אחרי אישור צילום אוכל — רק הודעת מצב יום קצרה: נשמר, קלוריות/חלבון בארוחה, נאכל עד עכשיו, נשאר, שעה, המשך היום, עד 4 כפתורים.

שלב 3 — הפערים המקצועיים שנותרו לסגור:

A. ExerciseCatalog מלא יותר: להרחיב קטלוג תרגילים עם movement pattern, שרירים, ציוד, joint_load, contraindications, regressions, progressions, cues.
B. Progression Engine אמיתי: לפי performed sets, RIR, כאב, פספוס אימונים, ירידה בביצועים.
C. Planned vs actual באימונים: לוודא שה־DB וה־UI מפרידים בין planned workout/performed workout/performed set.
D. Daily menu lifecycle: עריכת הודעת תפריט נעוצה בפועל עם fallback כש־Telegram edit נכשל.
E. Profile stale-state cleanup: אם DB ישן כבר מכיל facts סותרים, להציג מנגנון תיקון/איחוד ולא רק renderer יפה.

בסוף תחזיר דוח עם:
- tests שרצו ותוצאות.
- סטטוס TASK-01 עד TASK-14: Done / Partial / Needs live test.
- פערים שנותרו שלא ניתן לסגור בלי שינוי ארכיטקטורה.
- קבצים ששונו.
- האם נעשה commit ומה ה-hash.
