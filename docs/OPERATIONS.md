# תפעול Production

## סדר פריסה

1. גיבוי DB ו-storage.
2. `docker compose build`.
3. `docker compose run --rm coach python scripts/preflight.py --skip-runtime-secrets` לבדיקת image.
4. `docker compose run --rm coach python scripts/migrate_db.py`.
5. `docker compose up -d`.
6. בדיקת `/healthz` ו-`/readyz`.
7. בדיקת `/start`, תמונת אוכל, התחלת אימון, שמירת סט ו-Mini App.

## ניטור מינימלי

- בדוק ש-`/readyz` מחזיר 200.
- עקוב אחר נפח volumes וה-DB.
- הפעל גיבוי יומי ושחזור ניסוי חודשי.
- עקוב אחר הודעות admin ושגיאות OpenAI/Telegram.
- שמור לוגים ברוטציה; אל תשמור headers של Authorization או query של Mini App.

## תקלות נפוצות

### database is locked

- ודא שרץ מופע אחד בלבד.
- אל תפתח את ה-DB בכלי עריכה שמשאיר transaction פתוח.
- בדוק מקום בדיסק והרשאות volume.

### Telegram Conflict

מופע נוסף משתמש באותו token. עצור את כל המופעים והפעל אחד.

### קובץ ZIP לא יורד

קובץ גדול עלול להיחסם על ידי Telegram cloud. כשהבוט רץ על אותו מחשב, שלח בצ'אט את הנתיב המלא לתיקייה או לקובץ והבוט יקרא אותו ישירות. אפשר גם להגדיר Local Bot API.

### readiness מחזיר 503

בדוק את שדה `checks` בתגובה: DB, storage, disk או Telegram.

## גיבוי אוטומטי

דוגמת cron יומית ב-03:15:

```cron
15 3 * * * cd /opt/noam-coach && docker compose exec -T coach python scripts/backup.py >> /var/log/noam-coach-backup.log 2>&1
```

העתק את התוצאה לאחסון חיצוני מוצפן. אין להסתמך על volume יחיד.
