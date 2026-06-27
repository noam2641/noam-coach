# Device API (אופציונלי)

ב־rc5 ה־API השוטף כבוי כברירת מחדל. כדי להפעיל אותו יש להגדיר `ENABLE_HEALTHKIT_API=true`. כאשר הוא כבוי, endpoints אלה מחזירים 404.

כאשר הוא פעיל, כל endpoints תחת `/api/` דורשים:

```http
Authorization: Bearer <HEALTHKIT_API_TOKEN>
Content-Type: application/json
```

## POST /api/healthkit/samples

Batch של עד 1,000 דגימות:

```json
{
  "telegram_user_id": 123456789,
  "samples": [
    {
      "external_id": "device-anchor-123",
      "sample_type": "steps",
      "value": 8500,
      "unit": "count",
      "start_time": "2026-06-18T00:00:00+03:00",
      "end_time": "2026-06-18T23:59:59+03:00",
      "source_device": "iPhone"
    }
  ]
}
```

## POST /api/shortcut/health

```json
{
  "telegram_user_id": 123456789,
  "measured_at": "2026-06-18T08:00:00+03:00",
  "weight_kg": 90.2,
  "steps": 3200,
  "active_calories": 410,
  "sleep_minutes": 430,
  "resting_heart_rate": 58,
  "hrv_ms": 47
}
```

## POST /api/watch/set

```json
{
  "telegram_user_id": 123456789,
  "client_event_id": "watch-uuid-v4",
  "reps": 10,
  "rir": 2,
  "weight": 50
}
```

`client_event_id` חייב להיות ייחודי כדי לאפשר retry ללא שמירת סט כפול.

## מגבלות

- rate limit לפי IP בתהליך היחיד.
- body size מוגבל.
- בגרסה מרובת workers יש להעביר rate limit ו-device auth לשכבה משותפת כגון Redis/PostgreSQL.
