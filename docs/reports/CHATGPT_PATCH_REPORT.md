# ChatGPT patch report — Noam Coach

## מה עודכן בפאץ׳ הזה

### 1. למידה של מאכלים/מוצרים שהמשתמש מעלה
נוסף מנגנון שמפיק “מאכלים נלמדים” מתוך ארוחות שאושרו ונשמרו בפועל בטבלאות `meals` ו־`meal_items`.

היישום משתמש רק בארוחות מאושרות/נאכלות, ולכן לא לומד מהצעה בלבד או מתמונה שלא אושרה.

קבצים:
- `noam_coach/services/learned_foods.py` — שירות חדש שמזהה פריטים שחוזרים בהיסטוריית הארוחות, מחשב ממוצע קלוריות/חלבון/כמות למנה, ומחזיר אותם כ-context.
- `noam_coach/services/profile.py` — ניתוח תמונת אוכל/טקסט מקבל עכשיו context של מוצרים שהמשתמש כבר העלה ואישר בעבר, כדי לשפר זיהוי וכיול קלורי כאשר זה מתאים לתמונה/טקסט.
- `noam_coach/services/nutrition_context.py` — ה-context המשותף לתפריט/AI כולל עכשיו `learned_foods`.
- `noam_coach/services/next_meal.py` — דירוג “מה לאכול עכשיו” מקבל בונוס קטן אם ההצעה כוללת פריט שהמשתמש אוכל לעיתים קרובות, אחרי שעברה את בדיקות התקציב וההגבלות.
- `recommendations.py` — תפריט יומי fallback ללא AI משלב שמות פריטים שחוזרים אצל המשתמש ומציין זאת בסגירה.
- `tests/regression/test_learned_foods_personalization.py` — בדיקות רגרסיה חדשות למנגנון הלמידה.

## בדיקת התאמה ממוקדת לתיקוני Claude מול הדרישות מהתמונות

הבדיקה כאן היא קריאת קוד ממוקדת, לא בדיקת Telegram חי.

| משימה | סטטוס שנראה בקוד | הערה |
|---|---|---|
| TASK-01 — Flow תזונה לא קופץ לכושר | נראה מיושם | `_plan_completion_profile_order("nutrition")` מחזיר רק `("nutrition",)` ולכן לא אמור להמשיך לשאלות אימון. |
| TASK-03 — “מה לאכול עכשיו” כהמלצה אחת | נראה מיושם | `generate_next_meal_recommendation` חותך ל־`[:1]`; הכפתורים הם אשר/רענן/שנה כמויות/חזור. |
| TASK-04 — לא לספור planned/recommended כ־consumed | נראה מיושם ברמת החישוב היומי | `daily_state.consumed_totals` ו־`consumed_meals` קוראים רק מטבלת `meals`, כלומר רק ארוחות שנשמרו. |
| TASK-14 — הודעה קצרה אחרי אישור תמונת אוכל | נראה מיושם | `render_post_meal_confirmation_day_status` קיים ומשמש אחרי `approve_meal`/`force_approve_meal` עם 4 כפתורים. |
| TASK-11 — HealthKit לא דורס ידני | חלקי/דורש המשך בדיקה | בקוד לא נמצאו שדות מפורשים `detected_training_days/preferred_training_days/active_training_days`. ייתכן שיש פתרון אחר, אבל לא סימנתי כגמור. |
| TASK-07/08 — תוכנית אימונים מקצועית + ExerciseCatalog/Pain | חלקי | יש `training_intelligence.CATALOG`, `joint_load`, pain-aware ו-substitution, אבל זה לא בהכרח קטלוג מלא לפי כל דרישת backlog. |
| TASK-05/06/10/13 | לא סומן כגמור | דורש המשך בדיקה/יישום ייעודי. |

## אימות שבוצע כאן

- `python -m compileall` על הקבצים ששונו — עבר.
- לא הצלחתי להריץ `pytest`/`ruff` מלא בסביבה הזו כי חסרות תלויות של הפרויקט, למשל `openai`, ו־`ruff` לא מותקן כאן.

אחרי חילוץ הפאץ׳ אצלך, להריץ:

```powershell
python -m ruff check noam_coach/services/learned_foods.py noam_coach/services/profile.py noam_coach/services/next_meal.py noam_coach/services/nutrition_context.py recommendations.py tests/regression/test_learned_foods_personalization.py
python -m pytest -p no:cacheprovider -q tests/regression/test_learned_foods_personalization.py
```

ואז מומלץ להריץ גם את הבדיקות שכבר היו ירוקות אצל Claude/Codex.

## הערת מוצר

“תיקונים קריטיים” לא אומר רק קריסות. בהקשר שלך זה אומר:
1. תיקון שמונע שבירת Flow מרכזי.
2. תיקון שמונע חישוב קלורי/חלבון שגוי.
3. תיקון שמונע שמירת state שגוי או דריסת מידע ידני.
4. תיקון שמיישר את ההתנהגות עם ההערות שבתמונות.
5. תיקון שמאפשר להמשיך לפתח בלי להכניס עוד בלגן.

הפיצ׳ר החדש של `learned_foods` נחשב שיפור מוצר חשוב, אבל לא מחליף את הצורך להמשיך audit מלא לכל ה־TASKים הפתוחים.
