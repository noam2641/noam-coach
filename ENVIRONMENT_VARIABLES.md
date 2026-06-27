# Environment Variables

החבילה אינה כוללת `.env`. צור קובץ פרטי בשם `.env` בשורש הפרויקט.

## מינימום להפעלת Telegram מקומית

```env
APP_ENV=dev
HOST=127.0.0.1
PORT=8000
TIMEZONE_NAME=Asia/Jerusalem

DATABASE_PATH=./noam_coach.db
STORAGE_DIR=./storage

TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_ID=
ADMIN_CHAT_ID=
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini

ENABLE_HEALTHKIT_API=false
HEALTHKIT_API_TOKEN=

ENABLE_LOCAL_HEALTH_PATH_IMPORT=true
LOCAL_HEALTH_IMPORT_ALLOWED_ROOTS=C:\Users\YOUR_USER\Desktop;C:\Users\YOUR_USER\Downloads
LOCAL_HEALTH_IMPORT_RECURSIVE=false
HEALTH_IMPORT_MAX_LOCAL_MB=4096

MINI_APP_SECRET=
PUBLIC_BASE_URL=http://127.0.0.1:8000
```

## Telegram Local Bot API אופציונלי

```env
TELEGRAM_BASE_URL=
TELEGRAM_BASE_FILE_URL=
```

## מגבלות קבצים ו־API

```env
PHOTO_RETENTION_DAYS=30
HEALTH_RETENTION_DAYS=548
API_MAX_BODY_BYTES=5242880
API_RATE_LIMIT_REQUESTS_PER_MINUTE=120
HEALTH_IMPORT_MAX_UPLOAD_MB=300
HEALTH_IMPORT_MAX_LOCAL_MB=4096
HEALTH_IMPORT_MAX_XML_MB=4096
HEALTH_IMPORT_MAX_ZIP_MEMBERS=100000
HEALTH_IMPORT_MAX_COMPRESSION_RATIO=250
```

## הודעות יזומות

```env
PROACTIVE_DAILY_LIMIT=4
PROACTIVE_MIN_GAP_MINUTES=75
PROACTIVE_QUIET_START=22:30
PROACTIVE_QUIET_END=07:00
PROACTIVE_MAX_ATTEMPTS=3
PROACTIVE_CLAIM_TTL_MINUTES=20
```

## Feature flags

```env
FEATURE_CONVERSATION_ROUTER_V2=true
FEATURE_SMART_PLANS_V2=true
FEATURE_MEAL_DUPLICATE_V2=true
FEATURE_WORKOUT_INTELLIGENCE_V2=true
FEATURE_NEXT_BEST_ACTION=true
FEATURE_MINI_APP_PLANS=true
```

## יצירת secrets

```powershell
.\.venv\Scripts\python.exe .\scripts\generate_secrets.py
```

אין לשתף את הפלט, לשמור אותו בצילום מסך או להעלות אותו ל־Git.

## מצב עבודה מומלץ כעת

- `ENABLE_HEALTHKIT_API=false` — מבטל עדכוני HealthKit/Shortcuts/Watch שוטפים.
- `ENABLE_LOCAL_HEALTH_PATH_IMPORT=true` — מאפשר לשלוח בצ'אט נתיב מלא לקובץ או לתיקייה מקומית.
- הנתיב עובד רק אם הבוט רץ על אותו מחשב Windows שבו הקובץ נשמר.
- כאשר `LOCAL_HEALTH_IMPORT_ALLOWED_ROOTS` ריק, ברירת המחדל היא תיקיית הבית של משתמש Windows שמריץ את הבוט.
- מומלץ להגדיר במפורש רק Desktop/Downloads או תיקייה ייעודית.
