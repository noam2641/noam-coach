# Security Notes — 2.0.0-rc5 (updated 2026-06-25)

## תיקוני אבטחה — 2026-06-25

- **SQL Injection**: תוקן שימוש ב-f-string במיגרציה 5 (`db.py`); כעת משתמש ב-parameterized query.
- **Content-Length bypass**: תוקן `int(bytes)` ב-`APIBodyLimitMiddleware` שגרם ל-`TypeError` — מגבלת גוף בקשה לא נאכפה.
- **Stored XSS**: תוקן `_safe_html_block` שהחזיר תגיות HTML עם attributes מקלט משתמש; כעת regex מאפשר רק `<b>`, `</b>`, `<i>`, `</i>` ללא attributes.
- **Rate limit coverage**: הורחבה הגנת body limit ו-rate limit לנתיבי `/mini/` בנוסף ל-`/api/`.
- **FoodItem validation**: הופעל `validate_assignment=True` ב-Pydantic כך שמוטציה ישירה של שדות עוברת ולידציה מלאה (NaN, infinity, שליליים, טווח, עקביות מאקרו).
- **Transaction atomicity**: שתי פונקציות `activate_goal_version` עטופות כעת ב-`DB.transaction()`.
- **PRAGMA foreign_keys**: הועבר ל-`finally` block במיגרציה כך שחוזר ל-ON תמיד.

## הערות כלליות

- release נבנה מ־allowlist ונבדק מול forbidden patterns; secrets, DB, storage, backups, caches ו־virtualenv אינם נארזים.
- נתיבי HealthKit/Shortcuts/Watch משתמשים ב־auth, validation, מגבלות גוף ו־idempotency, וכבויים כברירת מחדל.
- ייבוא ZIP בודק path traversal, symlinks, מספר קבצים וגודל מפוענח כדי לצמצם ZIP bombs.
- ייבוא מנתיב מקומי מוגבל לשורשים מורשים, חוסם UNC, אינו סורק רקורסיבית כברירת מחדל ואינו מוחק את קובץ המקור.
- Mini App משתמש ב־login token קצר־חיים וב־HttpOnly session cookie; production חייב HTTPS ו־secret נפרד.
- אין לרשום tokens, headers מלאים, image bytes או מידע רפואי מלא בלוגים.
- retention מוחק תמונות ונתונים תפעוליים לפי המדיניות; מחיקה וייצוא משתמש זמינים בסקריפטים.
- rate limiter הנוכחי הוא מקומי לתהליך. לפני multi-worker יש להעבירו ל־store משותף.
- SQLite מתאים למופע אישי; לפני multi-user production נדרשים tenant isolation, device pairing, PostgreSQL, queue, object storage מוצפן ובדיקת חדירות.
- אין להשתמש בתוצאות תזונה/אימון כאבחנה רפואית.
## REC-PROGRAM-04 Security Notes — 2026-06-27

- Mini App inline event handlers were removed to comply with `script-src 'self'`.
- Mini App dynamic HTML now escapes server-provided text before rendering.
- `/mini/upload` uses the Health upload size limit rather than the generic API JSON body limit.
- Release ZIP was independently inspected: 0 forbidden entries, no `.env`, DB, Health export, storage, cache, `.git`, `.claude`, or IDE entries.
