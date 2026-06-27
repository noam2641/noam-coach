# מימוש iPhone + Apple Watch

> **סטטוס: נדחה / אופציונלי לעתיד.** ב־rc5 המוצר משתמש בייבוא ZIP/XML תקופתי ואין צורך ב־HealthKit או Watch כדי להפעיל את הבוט. המסמך נשמר כרעיון עתידי בלבד.

השרת מוכן לקבל נתונים, אך אפליקציית Swift אינה כלולה כיישום עובד משום שהיא דורשת Xcode, חתימת Apple ו-HealthKit entitlements.

## יעד גרסת iPhone ראשונה

1. פרויקט iOS ב-SwiftUI.
2. הוספת HealthKit capability.
3. בקשת הרשאות קריאה ל:
   - bodyMass
   - bodyFatPercentage
   - stepCount
   - activeEnergyBurned
   - appleExerciseTime
   - sleepAnalysis
   - restingHeartRate
   - heartRateVariabilitySDNN
   - workouts
4. `HKAnchoredObjectQuery` לכל סוג נתונים ושמירת anchor ב-Keychain/UserDefaults.
5. מיפוי ל-`HealthSample` ושליחת batch של עד 1,000 רשומות.
6. token נשמר ב-Keychain בלבד.
7. retry עם exponential backoff ו-idempotency באמצעות UUID קבוע לכל sample.
8. מסך Sync Status: הרשאות, last sync, כמות שנשלחה ושגיאה אחרונה.
9. Background delivery כאשר iOS מאפשר זאת.
10. הפצה ב-TestFlight.

## Pairing נכון

בגרסה האישית אפשר להזין ידנית את token. לפני משתמשים נוספים נדרש:

- קוד pairing חד-פעמי מהבוט.
- endpoint שמחליף pairing code ב-device token.
- token hash במסד, revoke ו-last_used_at.
- token נפרד לכל מכשיר ולא secret גלובלי.

## Apple Watch

1. יעד watchOS ב-Xcode.
2. מסך תרגיל נוכחי, משקל, חזרות, RIR ומספר סט.
3. כפתור שמירת סט עם `client_event_id` UUID.
4. queue מקומי כאשר אין רשת.
5. טיימר מנוחה מקומי.
6. WatchConnectivity להעברת מצב מה-iPhone.
7. חלופות ודיווח כאב.
8. סנכרון כפול בטוח: אותו event ID ב-retry.

## Definition of Done

- סנכרון incremental לאחר restart.
- אין כפילויות ב-retry/offline.
- token אינו נשמר ב-UserDefaults או בלוגים.
- משתמש יכול לבטל הרשאות ולמחוק pairing.
- last sync מוצג בבוט ובאפליקציה.
- נבדק על מכשיר פיזי, לא רק simulator.
