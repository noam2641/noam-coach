# חיבור ה־Mini App בשלב המקומי

ה־Mini App הוא אתר FastAPI שמוגש מתוך אותו תהליך של הבוט. אין תשלום ל־Telegram עבור עצם השימוש ב־Mini App. כדי לפתוח אותו מהטלפון, Telegram חייב להגיע לכתובת HTTPS ציבורית.

## האפשרות המומלצת כרגע — הבוט נשאר על המחשב שלך

גישה זו שומרת גם על ייבוא קובץ Apple Health לפי נתיב מקומי. שרת מרוחק אינו יכול לקרוא נתיב כמו `C:\Users\...\Desktop` מהמחשב שלך.

### 1. הגדר `.env`

```env
APP_ENV=dev
HOST=127.0.0.1
PORT=8000

ENABLE_HEALTHKIT_API=false
ENABLE_LOCAL_HEALTH_PATH_IMPORT=true
LOCAL_HEALTH_IMPORT_ALLOWED_ROOTS=C:\Users\YOUR_USER\Desktop;C:\Users\YOUR_USER\Downloads
HEALTH_IMPORT_MAX_LOCAL_MB=4096

PUBLIC_BASE_URL=https://YOUR-TUNNEL-ADDRESS.trycloudflare.com
MINI_APP_SECRET=PUT_A_RANDOM_SECRET_OF_AT_LEAST_32_CHARACTERS_HERE
```

את `MINI_APP_SECRET` ניתן ליצור באמצעות:

```powershell
.\.venv\Scripts\python.exe .\scripts\generate_secrets.py
```

### 2. הפעל את הבוט

```powershell
.\.venv\Scripts\python.exe .\coach_bot.py
```

בדוק במחשב:

- `http://127.0.0.1:8000/healthz`
- `http://127.0.0.1:8000/readyz`

### 3. פתח Tunnel זמני

התקן `cloudflared`, ואז בחלון PowerShell נוסף:

```powershell
.\scripts\start_miniapp_tunnel.ps1
```

אפשר גם להריץ ישירות:

```powershell
cloudflared tunnel --url http://127.0.0.1:8000
```

העתק את כתובת ה־HTTPS שמסתיימת ב־`trycloudflare.com`, הצב אותה ב־`PUBLIC_BASE_URL`, והפעל מחדש את הבוט. כתובת Quick Tunnel משתנה בכל הפעלה.

### 4. פתח מתוך Telegram

שלח לבוט:

```text
/app
```

הבוט ייצור קישור כניסה חתום וקצר־חיים ויציג כפתור `פתח Mini App`.

## כפתור קבוע דרך BotFather

הקוד הנוכחי משתמש בקישור כניסה אישי וחד־פעמי שנוצר בפקודה `/app`. לכן אין חובה להגדיר Menu Button ב־BotFather בשלב הזה.

כדי להוסיף בעתיד כפתור קבוע בפרופיל או בתפריט הבוט, צריך להוסיף כניסה המבוססת על Telegram `initData` בכתובת סטטית. לאחר מכן ניתן להגדיר ב־BotFather:

- `/mybots`
- בחירת הבוט
- `Bot Settings`
- `Menu Button` או `Configure Mini App`
- הזנת כתובת HTTPS קבועה

## עלויות

### פיתוח אישי

- Telegram Mini App: ללא חיוב מצד Telegram.
- Cloudflare Quick Tunnel: ניתן להפעיל ללא שרת בתשלום, אך הכתובת זמנית והמחשב חייב להישאר דולק.
- מסד הנתונים נשאר מקומית על המחשב.

### כתובת קבועה

אפשרויות נפוצות:

1. Cloudflare Named Tunnel עם דומיין שלך — ה־Tunnel עצמו יכול להישאר ללא עלות נוספת, אך דומיין עשוי לעלות כסף.
2. שרת ציבורי — קיימות שכבות חינמיות מוגבלות, אך בשרת מרוחק לא ניתן לקרוא נתיב קובץ מהמחשב האישי שלך.
3. שרת בתשלום — מתאים רק כאשר עוברים לאחסון קבצים דרך העלאה ולא דרך נתיב מקומי.

לשלב הנוכחי, השילוב המתאים ביותר הוא: **בוט מקומי + Cloudflare Tunnel + פתיחה עם `/app`**.
