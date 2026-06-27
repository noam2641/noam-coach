# Continuation State — 2.0.0-rc5

עודכן: 2026-06-24

## הושלם

- סנכרון HealthKit/Shortcuts/Watch שוטף כבוי כברירת מחדל.
- ייבוא Apple Health תקופתי נשאר דרך ZIP/XML.
- נוסף ייבוא מנתיב קובץ או תיקייה מקומית בצ'אט ובפקודה `/importpath`.
- הנתיב מוגבל לשורשים מורשים ואינו יכול להיות UNC או נתיב חיצוני.
- תיקייה בוחרת את export החדש ביותר; קובץ המקור אינו נמחק.
- Telegram upload, Mini App upload ונתיב מקומי משתמשים בליבת ייבוא משותפת.
- נוסף מדריך Mini App ו־PowerShell helper ל־Cloudflare Tunnel.

## אימות

- compileall: עבר.
- Ruff: עבר.
- pytest: 351 בדיקות עברו.
- evaluations: 33/33 ב־11 קטגוריות.
- preflight: עבר.
- coverage כולל: 67%.
- build release: 160 קבצים, 0 forbidden entries.

## תלות חיצונית

- Telegram token ו־OpenAI key אמיתיים.
- Mini App בטלפון דורש HTTPS ציבורי; לפיתוח ניתן להשתמש ב־Quick Tunnel.
- נתיב מקומי עובד רק כשהבוט רץ על אותו מחשב שמכיל את הקובץ.
