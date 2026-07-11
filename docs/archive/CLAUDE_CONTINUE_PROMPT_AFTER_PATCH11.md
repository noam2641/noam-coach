אתה נכנס לפרויקט `noam_coach_complete_release` אחרי סדרת PATCH-01 עד PATCH-11 שבוצעה על ידי ChatGPT. המטרה שלך: לא להניח שהכול תקין בגלל דוחות, אלא לאמת מול הקוד, להריץ בדיקות, ולסגור את הפערים שנותרו מהרשימה המקורית של 14 המשימות.

כללי עבודה מחייבים:
1. אל תעשה `git reset`, `git stash`, `git revert`, `git push`, או מחיקה של קבצים בלי אישור מפורש.
2. אל תעשה `git add .`. מותר להוסיף רק קבצים רלוונטיים אחרי `git status --short` ו־`git diff --stat`.
3. אל תפתח refactor רחב לפני שהוכחת שה־flows המרכזיים עובדים.
4. כל טענה “בוצע” חייבת להיות מגובה באחד מאלה: קוד production מחובר, regression test, או בדיקת Telegram ידנית מתועדת.
5. אל תסמן ExerciseCatalog כ־100% אם לא הרחבת אותו בפועל לכל קבוצות התרגילים הרלוונטיות.

שלב 1 — בדיקת מצב:
הרץ:
```powershell
git status --short
git diff --stat
python -m pytest -p no:cacheprovider -q tests/regression/test_patch11_remaining_contracts.py
python -m pytest -p no:cacheprovider -q tests/regression/test_backlog_14_contracts.py tests/regression/test_patch10_meal_status.py tests/regression/test_re13_health_quality.py tests/regression/test_learned_foods_personalization.py tests/regression/test_goal_weight_validation.py tests/test_availability_parser.py tests/test_next_meal_callbacks.py
python -m pytest -p no:cacheprovider -q
```
אם יש כשל — עצור, נתח root cause, תקן מינימלית, והריץ שוב את הבדיקה שנכשלה ואז full suite.

שלב 2 — אמת מול המשימות המקוריות:
פתח את `noam_coach_backlog_from_screenshots_updated.md` ואת דוחות `CHATGPT_PATCH_*`. עבור על TASK-01 עד TASK-14 וסמן לכל אחד:
- Done in production code
- Has regression test
- Needs Telegram live test
- Partial / not done

שלב 3 — בדיקות Telegram ידניות שחייבות לעבור:
1. `/profile` — לוודא שהפרופיל לא נראה כמו debug log, מציג יעד, זמן יעד, ימי אימון פעילים ומקור קצר.
2. HealthKit import — אם יש ימים ידניים, הבוט לא שואל שוב 2/3/4 ולא מציע לדרוס ימים מהשעון.
3. “מה לאכול עכשיו” — הצעה אחת בלבד, עד 4 פעולות, בלי “אפשרות 1/2”, בלי כפתורי אימון מיותרים.
4. תיקון טקסט להמלצה — למשל “בלי טורטייה” — עדיין שומר על מסך ממוקד בלי כפתורים נוספים.
5. אישור תמונת אוכל — אחרי אישור מוצג post-meal קצר: מה נשמר, קלוריות/חלבון נוספו, נאכל עד עכשיו, נשאר, שעה, המשך היום, עד 4 כפתורים.
6. “תפריט להיום” — נשלח כהודעה עצמאית שאפשר לנעוץ. כפתור “החלף ארוחה” ואז טקסט “תחליף לי את ארוחת הבוקר לחלבון אחר” מקבל תשובה ייעודית ולא נופל ל־router כללי.
7. בניית תוכנית אימון — אם המשתמש הגדיר ראשון/שני/רביעי/שישי, כל 4 הימים מופיעים. מוצעות Full Body / Upper-Lower / ABC+Full Body.
8. כאב/טניס אלבו/ברך — התוכנית לא מעלה עומס אוטומטית בתרגילים שמעמיסים על אזור כאב, ומציעה חלופות מאותו movement pattern.
9. עריכת פרמטרים — “מנוחה 1:30 לכל התרגילים” מציגה אישור, רשימת/מספר תרגילים מושפעים, ולא נשמרת בטעות כחזרות.
10. יעד 50 ק״ג — דורש אישור/תיקון ולא נשמר מיידית.

שלב 4 — פערים שעדיין ראוי לסגור:
A. Daily menu pinned-message lifecycle: אם קיים `daily_menu_message_id`, נסה לערוך את הודעת התפריט הקיימת במקום לשלוח עוד הודעה, עם fallback בטוח אם Telegram edit נכשל.
B. ExerciseCatalog: הרחב metadata לתרגילים המרכזיים, כולל movement_pattern, primary/secondary muscles, equipment, joint_load, contraindications, regressions, progressions, cues.
C. Workout plan quality gate: אל תאפשר הפעלת מועמד אם יש `workout_quality_issues`; הצג למשתמש הסבר ותיקון.
D. Profile renderer: ודא שאין “דיווח שלך” בכל שורה, אין ימים סותרים, ומוצג ערך פעיל אחד בלבד.
E. Planned meal lifecycle: ודא ש־planned meal יכול להפוך ל־consumed meal רק באישור.

שלב 5 — Commit רק אם הכול ירוק:
אחרי תיקונים ובדיקות:
```powershell
git status --short
git diff --check
python -m pytest -p no:cacheprovider -q
```
אם ירוק, בצע commit אחד מסודר בלבד:
```powershell
git add <רק הקבצים ששינית בפועל>
git commit -m "feat(coach): complete backlog flow stabilization and verification"
```
בסוף החזר דוח קצר:
- אילו tests רצו ומה התוצאה.
- אילו TASKs סגורים בוודאות.
- אילו TASKs עדיין Partial ולמה.
- האם נעשה commit ומה ה־hash.
