# Apple Shortcuts — פתרון אופציונלי

> **סטטוס: כבוי כברירת מחדל.** `ENABLE_HEALTHKIT_API=false` מבטל את endpoint הסנכרון. בשלב הנוכחי השתמש בייבוא ZIP/XML תקופתי או בנתיב מקומי.

ה-Shortcut אינו מחליף אפליקציית HealthKit מלאה, אך מאפשר סנכרון יומי אוטומטי יחסית.

## נתונים לשליחה

- תאריך ושעת המדידה.
- משקל אחרון, כאשר קיים.
- צעדים היום.
- קלוריות פעילות היום.
- משך שינה בלילה האחרון בדקות.
- דופק מנוחה אחרון.
- HRV אחרון.

## בניית הקיצור

1. צור Shortcut חדש בשם `Noam Coach Sync`.
2. עבור כל מדד, השתמש ב-`Find Health Samples` או `Get Details of Health Samples`.
3. בנה Dictionary לפי `examples/shortcut_health.json`.
4. הוסף `Get Contents of URL`:
   - URL: `https://YOUR_DOMAIN/api/shortcut/health`
   - Method: POST
   - Request Body: JSON
   - Header: `Authorization` = `Bearer YOUR_HEALTHKIT_API_TOKEN`
5. אל תציג או תעתיק את token ללוג/הודעה.
6. בדוק שהתגובה מחזירה inserted/duplicated.
7. הוסף Automation בשעה קבועה, לדוגמה בבוקר ובערב. iOS עשוי לבקש אישור בהתאם להגדרות המכשיר.

## כללי נתונים

- כשאין ערך למדד, השמט אותו במקום לשלוח 0.
- `measured_at` חייב לכלול timezone.
- אל תשלח אותו אירוע שוב עם external ID משתנה דרך endpoint ה-batch; ב-Shortcut endpoint השרת יוצר מזהים לפי זמן המדידה.
- שמור את זמן הסנכרון האחרון כדי לזהות שהקיצור הפסיק לעבוד.
