# Implementation Status — 2.0.0-rc5 (updated 2026-06-25)

## סקירת אבטחה ותיקוני באגים — 2026-06-25

- תוקנו 36 ממצאים (6 קריטיים, 12 גבוהים, 18 בינוניים/נמוכים).
- נוספו 21 בדיקות רגרסיה חדשות; סה"כ 372 בדיקות.
- כל שערי האיכות ירוקים: compileall, ruff, pytest, evaluations, preflight, build.
- ראה `VALIDATION_REPORT.md` לפירוט מלא ו-`SECURITY.md` לתיקוני אבטחה.

## ארכיטקטורה

- `coach_bot.py` הוא composition root ו־compatibility facade דק.
- המימוש מחולק לחבילת `noam_coach` בשכבות app, api, bot, services ו־jobs.
- callback router מרכזי קצר ומודולים נפרדים לתפריט, תוכניות, ארוחות וסשן אימון.
- system, HealthKit, Shortcuts ו־Watch APIs מופרדים ל־FastAPI routers.
- Mini App מופרד ל־HTML/JS/CSS ול־API עסקי.
- compatibility bridge שומר על imports ו־monkeypatching היסטוריים בזמן המעבר.

## יכולות מיושמות ומחוברות

- Conversation Router, state מתמשך, microflows, expiry ו־versioned callbacks.
- onboarding, readiness, facts, goals וגרסאות תוכנית.
- שלוש הצעות תזונה ושלוש הצעות אימון עם constraints ו־Fit Score.
- locked quantities, cooked/raw, macro validation וזיהוי כפילויות בארוחות.
- אימון פעיל, RIR, סטים מפוצלים, מנוחה, חלופות, fatigue ו־deload.
- `workout_status` כמקור אמת לסיום completed/partial/cancelled.
- Apple Health ZIP/XML בהעלאה או מנתיב מקומי מאובטח; Shortcuts/HealthKit/Watch נשמרו כאפשרות אך כבויים כברירת מחדל.
- Mini App dashboard, פרופיל, תוכניות, שבוע מאוחד וייבוא Health.
- retention לתמונות ונתונים תפעוליים.
- Event Log, replay, audit, admin notifications ועבודות מתוזמנות.

## איכות ואימות

- 351 בדיקות אוטומטיות.
- 33 evaluation cases ב־11 קטגוריות.
- coverage כולל 67%.
- Ruff, compileall, preflight ו־release build נקיים.
- ארכיון release כולל 160 קבצים, נבנה מ־allowlist ואינו כולל secrets, DB, storage, caches או virtualenv.

## תלות חיצונית

השרת וה־contracts מוכנים, אך בדיקות production של Telegram, OpenAI, HTTPS, OAuth, Sentry וספקי מידע דורשות חשבונות, credentials או מכשירים חיצוניים. ראו `docs/WHAT_REMAINS.md`.

## החלטת מוצר ב־rc5

- אין תלות בסנכרון HealthKit שוטף בשלב הנוכחי.
- מקור הנתונים הוא export תקופתי של Apple Health.
- כאשר הבוט רץ מקומית, המשתמש יכול לשלוח נתיב קובץ/תיקייה בצ'אט.
- Mini App נפתח באמצעות `/app` וכתובת HTTPS של Tunnel או שרת ציבורי.
## REC-PROGRAM-04 checkpoint (2026-06-27)

- Completed current recording/program batch without starting `REC-NEXT-MEAL-05`.
- Added canonical dietary restriction, availability, and body-fat services to production paths and tests.
- Fixed nutrition-plan restriction filtering, dietary alias negation, Mini App CSP/XSS issues, Mini App weekday alignment, and Mini App Health upload body limit handling.
- Verification gates are green: compileall, Ruff, pytest, evaluations, preflight, bounded startup smoke, and release inspection.
- Detailed classifications and limitations: `docs/CODEX_AUDIT_REC_PROGRAM_04.md`.
