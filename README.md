# Noam Coach 2.0.0-rc5 — Nutrition, Training and Health Coach

מערכת אישית בעברית המשלבת Telegram Bot, Mini App מקומי, תכנון תזונה ואימונים, מעקב Apple Health, דיווחי אוכל בתמונה ומאמן אימון בזמן אמת.

הגרסה הזאת נבנתה מחדש סביב שלושה עקרונות:

1. **מקור אמת אחד** למצב השיחה, ליעד ולתוכניות הפעילות.
2. **כללים דטרמיניסטיים לפני AI** — המודל מציע ומנסח, אך הקוד מאמת אילוצים, בטיחות ואיכות נתונים.
3. **תוכנית יציבה ומאושרת** — שלוש חלופות תזונה ושלוש חלופות אימון, בחירה מפורשת וגרסאות מלאות.

> החבילה אינה כוללת שום קובץ `.env`, מסד נתונים אישי, תמונות, גיבויים, `.venv` או `.git`.

## מבנה הקוד

`coach_bot.py` הוא כעת נקודת כניסה ו־compatibility facade בלבד (550 שורות). המימוש מחולק תחת `noam_coach/` ל־API routers, Telegram handlers, services, jobs ו־runtime. `handle_callback` ו־`handle_session_action_callback` הם dispatchers קצרים, וה־Mini App נשמר בקבצי HTML/JS/CSS נפרדים. מפת המודולים והוראות הרחבה נמצאות ב־`docs/ARCHITECTURE.md`.

מצב האימות של rc5: **351 בדיקות**, **33/33 evaluations ב־11 קטגוריות**, Ruff/compileall/preflight נקיים ו־coverage כולל של **67%**.

## היכולות המרכזיות

### שיחה ומצב

- `ConversationRouter` משמש כשער ניתוב מרכזי לטקסט, תמונה, מסמך וכפתור.
- flow ראשי ו־microflow זמני עם השהיה וחזרה.
- state נשמר ב־SQLite ונשאר לאחר restart.
- `flow_id` ו־version לכפתורים חדשים, כדי למנוע פעולה מכפתור ישן.
- expiry, ביטול, חזרה ושחזור תהליך.
- Event Log ו־Replay לצורך איתור תקלות.

### פרופיל ו־Onboarding

- Readiness נפרד לתזונה, אימון, בטיחות ומעקב.
- `gap` אינו נחשב תשובה.
- estimate שלא אושר אינו יכול להפעיל תוכנית.
- מקור, confidence, confirmation, freshness, visibility ו־affects לכל fact.
- תיאור חופשי של יום רגיל וחילוץ שעות עבודה, נסיעה, הפסקות, בישול וזמני אימון.
- מסך “כך הבנתי אותך” לפני בניית תוכנית.
- עריכת פרופיל ולוח שבועי ב־Mini App.

### יעדים

- מקור אמת יחיד: `goal_versions`.
- סטטוסים והיסטוריה: provisional, proposed, active, superseded.
- יעד ידני יוצר גרסה חדשה ואינו דורס היסטוריה.
- חישוב מוסבר של קלוריות, חלבון וצעדים.
- interpolation לפעילות, workout bonus שמרני וחישוב חלבון לפי משקל יעד/מסת גוף רזה כאשר המידע קיים.
- יעד זמני מסומן ככזה ואינו משמש להתראות חזקות.

### תוכניות תזונה ואימונים

- שלוש הצעות תזונה:
  - מסודרת.
  - גמישה.
  - מינימום התעסקות.
- שלוש הצעות אימון:
  - מקסימום עקביות.
  - מאוזנת.
  - ביצועים.
- Fit Score, הסבר, יתרונות, trade-offs והנחות לכל חלופה.
- Constraint Engine לזמנים, ציוד, ניסיון, כאב, אלרגיות ומגבלות.
- תוכנית אינה הופכת לפעילה ללא readiness מלא ואישור המשתמש.
- תוכנית שבועית מאוחדת של אוכל ואימונים.
- Plan Freeze ומנגנון “הפעולה הבאה הטובה ביותר”.

### אוכל ותמונות

- ניתוח תמונות אוכל באמצעות OpenAI.
- confidence ברמת הארוחה והפריט.
- בדיקת עקביות מאקרו–קלוריות.
- כמות מפורשת של המשתמש היא `locked field` ואינה נדרסת.
- בירור משקל יבש/מבושל במזונות רגישים.
- שאלות הבהרה מקושרות לפריט הנכון.
- מניעת כפילות לפי Telegram file ID, SHA-256 ו־perceptual hash.
- עריכה לאחר שמירה, Undo וחזרה לטיוטה.
- Data Quality Gate לפני סיכום או התראה.

### אימון

- תוכנית לפי זמינות יומית, זמן, ציוד, ניסיון וכאב.
- סינון לפי movement pattern ו־joint load, לא רק לפי שם התרגיל.
- חלופות לציוד חסר או מכשיר תפוס.
- Warm-up sets לכל תרגיל.
- גרסת אימון מהירה.
- RIR, סטים מפוצלים, מנוחות והתקדמות לכל תרגיל.
- readiness score, plateau detection, fatigue ו־deload recommendation.

### Apple Health — ייבוא תקופתי

- ייבוא ZIP/XML מאובטח ללא סנכרון HealthKit שוטף כברירת מחדל.
- אפשר לשלוח בצ'אט נתיב מלא לקובץ או לתיקייה כאשר הבוט רץ על אותו מחשב Windows.
- תיקייה עם כמה exports נפתרת לקובץ החדש ביותר, תחת allowlist של תיקיות מורשות.
- מניעת path traversal, symlinks, zip bombs וחריגות גודל.
- חלוקה לפי אזור הזמן המקומי.
- dedup לפי source לצעדים וקלוריות פעילות.
- איחוד intervals בשינה כדי למנוע ספירה כפולה.
- Shortcuts/HealthKit/Watch endpoints נשמרו כאפשרות אך כבויים כברירת מחדל באמצעות `ENABLE_HEALTHKIT_API=false`.
- סנכרון מיידי לפרופיל לאחר קליטת נתונים.

### Mini App

- Dashboard עם הפעולה הבאה, readiness, יעד ותוכניות פעילות.
- עריכת שעות עבודה, נסיעה, הפסקת אוכל, בישול, ציוד, מגבלות וחלונות אימון.
- יצירת שלוש הצעות תזונה/אימון ובחירת תוכנית ראשית.
- בניית שבוע מאוחד.
- ייבוא Apple Health.
- session cookie מאובטח ו־login token חד־פעמי הנשמר במסד.

## התקנה מקומית ב־Windows

### 1. יצירת סביבה

```powershell
cd C:\coach_bot\noam_coach_complete
py -3.12 -m venv .venv
```

אם PowerShell חוסם activation, אין צורך לשנות מדיניות קבועה. אפשר להריץ ישירות:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
```

### 2. יצירת `.env` פרטי

החבילה אינה כוללת `.env`. צור קובץ חדש בשם `.env` לפי `ENVIRONMENT_VARIABLES.md`.

יצירת secrets:

```powershell
.\.venv\Scripts\python.exe .\scripts\generate_secrets.py
```

לשימוש מקומי ללא שרת:

```env
APP_ENV=dev
HOST=127.0.0.1
PORT=8000
PUBLIC_BASE_URL=http://127.0.0.1:8000
```

### 3. בדיקה

```powershell
.\.venv\Scripts\python.exe .\scripts\preflight.py
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe .\scripts\run_evaluations.py
```

### 4. הפעלה

```powershell
.\.venv\Scripts\python.exe .\coach_bot.py
```

בדפדפן המקומי:

- `http://127.0.0.1:8000/healthz`
- `http://127.0.0.1:8000/readyz`

ה־Mini App נפתח מהקישור החתום שהבוט יוצר בפקודה `/app`. כדי לפתוח אותו באייפון בלי שרת בתשלום, ניתן להשתמש ב־Cloudflare Quick Tunnel ולהגדיר את הכתובת הזמנית ב־`PUBLIC_BASE_URL`. הוראות מלאות נמצאות ב־`docs/MINIAPP_SETUP.md`.

## מעבר מ־DB קיים

עצור את הבוט ובצע גיבוי לפני migration:

```powershell
.\.venv\Scripts\python.exe .\scripts\backup.py
.\.venv\Scripts\python.exe .\scripts\migrate_db.py
.\.venv\Scripts\python.exe .\scripts\preflight.py --skip-runtime-secrets
```

המיגרציות יוצרות בין השאר:

- goal versions.
- plan versions ו־active plans.
- product events.
- meal fingerprints.
- persistent Mini App tokens.
- active flow version/expiry.
- meal approval linkage.

## בדיקות ורגרסיה

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=. --cov-report=term-missing
.\.venv\Scripts\python.exe .\scripts\run_evaluations.py
```

Evaluation cases נמצאים ב־`evaluations/core_cases.jsonl`. ניתן להוסיף מקרי אמת חדשים בלי לשנות קוד.

## Replay לתקלה

```powershell
.\.venv\Scripts\python.exe .\scripts\replay_session.py --user-id YOUR_TELEGRAM_ID --limit 200
```

הפלט מסתיר token, secret ו־image bytes.

## פרטיות ואבטחה

- אין להעלות `.env`, DB, תמונות או גיבויים ל־Git.
- `MINI_APP_SECRET` ו־Telegram token חייבים להיות שונים. `HEALTHKIT_API_TOKEN` נדרש רק אם מפעילים מחדש סנכרון שוטף.
- במצב production כתובת Mini App חייבת להיות HTTPS.
- הנתונים הרפואיים אינם משמשים לאבחון.
- ניתן לייצא ולמחוק נתוני משתמש באמצעות הסקריפטים המצורפים.

## מה דורש רכיב חיצוני ואינו יכול להיות “מוכן” רק מקוד Python

- Telegram Mini App ציבורי קבוע: דורש HTTPS נגיש. לפיתוח ניתן להשתמש ב־Tunnel.
- Google Calendar אמיתי: דורש OAuth והסכמת המשתמש.
- מאגר ברקודים/מחירי סופר/תפריטי מסעדות: דורש ספק נתונים חיצוני.
- מאמן אנושי: דורש תהליך הרשאה, פרטיות ותפעול.

ראו גם:

- `IMPLEMENTATION_STATUS.md`
- `VALIDATION_REPORT.md`
- `ENVIRONMENT_VARIABLES.md`
- `docs/PRODUCT_ROADMAP.md`
- `docs/IOS_WATCH_IMPLEMENTATION.md`
- `docs/ARCHITECTURE.md`
- `REBUILD_REPORT.md`

## ייבוא Apple Health מנתיב מקומי

כאשר הבוט פועל על אותו מחשב שבו נמצא הייצוא, אפשר לשלוח בצ'אט נתיב מלא, לדוגמה:

```text
C:\Users\user\Desktop\Apple Health Export
```

אפשר גם להשתמש בפקודה:

```text
/importpath C:\Users\user\Desktop\Apple Health Export
```

הבוט מחפש ZIP/XML בתיקייה, בוחר את החדש ביותר, מייבא אותו ואינו מוחק את קובץ המקור. נתיב ממחשב אחר או משרת מרוחק לא יעבוד.
