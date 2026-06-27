# Checklist לפני העלאה

## קוד

- [ ] `python -m compileall -q .`
- [ ] `ruff check .`
- [ ] `pytest`
- [ ] אין קבצים בשם `(1)` או `(4)`.
- [ ] אין `.env`, DB או תמונות ב-Git.

## סודות

- [ ] Telegram token תקין.
- [ ] user id נכון.
- [ ] OpenAI key תקין.
- [ ] Health API token באורך 32+.
- [ ] Mini App secret שונה מכל token אחר.
- [ ] PUBLIC_BASE_URL הוא HTTPS.

## נתונים

- [ ] גיבוי נוצר ונפתח.
- [ ] restore נוסה על עותק.
- [ ] migration נוסה על עותק DB אמיתי.
- [ ] `PRAGMA quick_check` ו-foreign key check תקינים.

## שרת

- [ ] דומיין מצביע לשרת.
- [ ] 80/443 פתוחים ורק 8000 קשור ל-localhost.
- [ ] Docker restart מופעל.
- [ ] `/healthz` מחזיר 200.
- [ ] `/readyz` מחזיר 200.
- [ ] נפח דיסק והתראות מנוטרים.

## Smoke Test

- [ ] `/start`.
- [ ] onboarding.
- [ ] תמונת אוכל + אישור/תיקון/ביטול.
- [ ] אימון + סט + double tap.
- [ ] callback ישן נדחה.
- [ ] ייבוא ZIP.
- [ ] Shortcuts endpoint.
- [ ] Mini App login/cookie.
- [ ] job יזום אחד.
- [ ] backup לאחר פעילות.
