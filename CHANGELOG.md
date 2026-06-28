# Changelog

## 2.0.0-rc7 — 2026-06-28

### Post-Claude audit hardening

- Fixed onboarding approval feedback so the basics summary and detected-pattern screens visibly refresh after each approval/correction/defer action.
- Fixed Health import inserted/duplicate counts for batched imports; the stored rows were correct, and the user-facing import summary now is too.
- Verified baseline `678a160` in an isolated worktree; reproduced the Sunday pytest failure (`1 failed, 626 passed, 3 warnings`).
- Fixed nutrition context test determinism with an injected clock and matching weekday.
- Added typed next-meal ingredients with canonical nutrition facts; displayed calories/protein are now computed from ingredient totals.
- Added validation that meal totals match ingredient totals and that protein values remain plausible.
- Implemented `nextmeal:editqty` as a real Telegram callback flow with persisted quantity scaling and active recommendation snapshots for stable choose/save continuation.
- Fixed Hebrew availability parsing so per-day durations are preserved (`שישי 10:00 שעה` -> 60 minutes).
- Narrowed ZIP ignore behavior to allow intentional `tests/fixtures/*.zip`.
- Added post-Claude regression coverage; full suite now reports `630 passed, 3 warnings`.

## 2.0.0-rc6 — 2026-06-27

### REC-PLAN-MEAL-03: Plan, meal and conversation fixes (18 issues)

- תיקון קריסה בעיבוד תשובת הגבלה תזונתית — מנתח מובנה עם סיווג סוג הגבלה.
- שחזור שגיאה שומר על שאלה פעילה — כפתורי ניסוי חוזר/דילוג/תפריט.
- בחירת תוכנית לא תופסת הודעות לא רלוונטיות — רק "1"/"2"/"3" נקלטות.
- שאלת מצב ארוחות מנותבת ל-handler ייעודי עם נתוני ארוחות אמיתיים.
- בדיקת נתונים קיימים לפני שאלה — הצגת ערך קיים עם אישור/עדכון.
- זיהוי סתירות תדירות אימונים בין הצהרה, תוכנית, וביצוע בפועל.
- הצגת פרופיל ללא ערכים פנימיים — מיפוי enum מרכזי לעברית.
- אחוזי מוכנות מוסברים — הצגת שדות חסרים כשלא מוכן.
- הצעות תוכנית מסמנות כשמבוססות על הערכות לא מאושרות.
- תיאור משתמש גובר על ניתוח תמונה בארוחות.
- הסרת שמן מתמידה בין גרסאות תיקון (locked_corrections).
- זיהוי סתירות הגבלה תזונתית בארוחה מנותחת.
- אישור שמירת ארוחה עם פרטי ארוחה וכפתור ביטול.
- כפתור פעולה הבא בתפריט הראשי תואם ל-next_best_action.
- טיפול בתסכול משתמש ("כבר אמרתי לך") — הצגת ערך קיים.
- רישום אירועי שגיאה מובנים בנקודות כשל עיקריות.
- אומת שסכומי ארוחות = סכום פריטים (כבר נכון).
- אומת שסכומים יומיים מארוחות שמורות בלבד (כבר נכון).
- נוספו 55 בדיקות רגרסיה; מספר הבדיקות עלה ל-477.
- ראו `docs/RECORDING_ISSUES.md` לפירוט כל 18 הנושאים.

## 2.0.0-rc5 — 2026-06-25

### REC-ONBOARD-02: Onboarding flow fixes (14 issues)

- ייבוא בריאות מחזיר תוצאה מובנית עם ספירת משקל/פעילות/שגויים, שם קובץ וסה"כ מאוחסן.
- מערכת סיווג טריות נתוני בריאות (5 רמות) עם אזהרה בסטטוס יומי כשנתונים ישנים.
- הוסר חשיפת מזהים פנימיים בכפתורים — 30+ תוויות עבריות ב-`display_label()`/`display_value()`.
- תקנון סטטוסי אישור עובדות: inferred/confirmed/corrected/deferred/not_applicable/stale/invalid.
- אונבורדינג שורד restart — `resume_onboarding_after_restart()` מזהה שלב ומרנדר מחדש.
- שחזור callback ישן: במקום תפריט ראשי גנרי, הבוט חוזר לזרימה הפעילה או לאונבורדינג.
- `question_by_fact_key()` מאפשר ניתוב ישיר ממסך מידע חסר לשאלה הרלוונטית.
- הערת תרופות/תיאבון ב-`RoutineExtraction` ללא הסקת אבחנה.
- `compute_readiness()` מחזיר deferred, stale, not_applicable, missing_labels ו-label.
- תווית פרופיל בטיחות שונתה ל-"שאלון בטיחות"; מרכז התוכנית מציג "הושלם"/"חסר מידע".
- `targets.py` משתמש ב-`display_label()` מרכזי לתוויות יעד זמני.
- מסך מידע חסר לתוכנית מקובץ לפי קטגוריה עם כפתור "השלם עכשיו".
- סטטוס יומי כולל מקטע טריות בריאות והסבר יעד זמני.
- רמז בתפריט הראשי מציע את הפעולה הבאה לפי שלב אונבורדינג ושלמות פרופיל.
- נוספו 41 בדיקות רגרסיה; מספר הבדיקות עלה ל-413.
- ראו `docs/RECORDING_ISSUES.md` לפירוט כל 14 הנושאים.

### סקירת אבטחה ותיקוני באגים

- תוקנה קריסת startup — `NameError: ensure_user_record` כאשר הבוט רץ כ-`__main__`.
- תוקן SQL injection במיגרציה 5 — f-string הוחלף בפרמטריזציה.
- תוקן טיפול שגוי ב-Content-Length מסוג bytes ב-ASGI middleware.
- תוקנו שתי פונקציות `activate_goal_version` שחסרה בהן transaction.
- תוקן Stored XSS ב-`_safe_html_block` — regex מדויק מחליף string replacement.
- תוקנה ולידציה חסרה ב-FoodItem — `validate_assignment=True` מונע NaN/infinity/שליליים.
- הורחבה הגנת rate limit ו-body limit לנתיבי `/mini/`.
- תוקן IndexError ב-Watch routes ע"י בדיקת bounds.
- תוקנה קריאה לא-טרנזקציונלית ב-workout summary.
- תוקנה ספירה שגויה של cumulative health samples.
- תוקן I/O סינכרוני ב-Mini App upload handler.
- תוקנו 5 מקומות עם naive datetime שגורמים ל-TypeError שקט.
- תוקנו 19 באגי לוגיקה נוספים (routine, planning, conversation, targets, reconcile ועוד).
- נוספו 21 בדיקות רגרסיה חדשות; מספר הבדיקות עלה ל-372.

### 2.0.0-rc5 — 2026-06-24 (original)

### Local Apple Health export + Mini App setup

- סנכרון HealthKit/Shortcuts/Watch שוטף כבוי כברירת מחדל באמצעות `ENABLE_HEALTHKIT_API=false`; token נדרש רק אם מפעילים אותו מחדש.
- נוספה אפשרות לשלוח בצ'אט נתיב מלא לקובץ ZIP/XML או לתיקייה מקומית כאשר הבוט רץ על אותו מחשב Windows.
- נוספו allowlist לתיקיות, הגבלת גודל מקומית, חסימת UNC, בחירת export חדש ביותר והגנה מפני נתיב מחוץ לשורשים המורשים.
- נוספה הפקודה `/importpath`, והפקודה `/import` מסבירה גם העלאה וגם נתיב מקומי.
- לוגיקת ייבוא ZIP/XML אוחדה לפונקציה משותפת; קובץ המקור המקומי לעולם אינו נמחק.
- נוספו `docs/MINIAPP_SETUP.md` ו־`scripts/start_miniapp_tunnel.ps1` להפעלה מקומית עם Cloudflare Tunnel.
- מספר הבדיקות עלה ל־351.

## 2.0.0-rc4 — 2026-06-24

### Modular rebuild

- `coach_bot.py` צומצם מכ־9,000 ל־550 שורות והפך ל־composition root ו־compatibility facade ללא לוגיקה עסקית חדשה.
- נוצרה חבילת `noam_coach` עם שכבות `app`, `api`, `bot`, `services` ו־`jobs`.
- callbacks פוצלו לפי menu, plans, meals ו־session; `handle_callback` הוא dispatcher של 66 שורות ו־`handle_session_action_callback` של 75 שורות.
- נתיבי system, HealthKit, Shortcuts ו־Watch חולצו ל־FastAPI routers נפרדים.
- Mini App נשאר בקבצי HTML/JavaScript/CSS נפרדים ומחובר דרך API ייעודי.
- תוקנה תאימות DB ב־`health_service.py` לאחר חילוץ השירותים.
- נוספו בדיקות ארכיטקטורה ו־device APIs; הסך עלה ל־340 בדיקות.
- coverage הכולל עלה ל־67%; evaluations נשארו 33/33 ב־11 קטגוריות.
- build_release כולל את החבילה המודולרית ומוודא 0 forbidden entries.
- נוספו `docs/ARCHITECTURE.md` ו־`REBUILD_REPORT.md`, וכל מסמכי הסטטוס סונכרנו לריצה בפועל.

## 2.0.0-rc3 — 2026-06-24

### Evaluation & regression hardening

- מנוע ה-evaluation הורחב מ-2 ל-11 קטגוריות דטרמיניסטיות, וקובץ המקרים מ-3 ל-33 מקרים
  הנגזרים מכשלי ההקלטה (locked_quantity, cooked_raw, meaningful_meal, macro_consistency,
  duplicate_detected, image_distance, workout_status, fatigue, readiness, goal_provisional, epley_1rm).
- כל מקרה בודק תכונה קונקרטית של פונקציית domain אמיתית, לא התאמת טקסט שבירה.
- ה-evaluation suite מחובר כעת ל-pytest (`tests/test_evaluation_suite.py`) כך ש-CI נכשל אם הוא מצטמצם או נסוג.
- נוסף `training_intelligence.workout_status` כמקור אמת יחיד לסטטוס סשן: סט יחיד מתוך רבים,
  משך אפס/שלילי או בחירת `partial`/`cancelled` לעולם אינם נקראים `completed`. שני אתרי
  הסיום ב-`coach_bot.py` (`wdone`, `skip`-to-end) מחושבים דרכו עם משך מבוסס timestamp אמיתי.
- מספר הבדיקות עלה ל-310; Ruff, compileall, preflight ו-build_release נקיים.

## 2.0.0-rc2 — 2026-06-24

### Correctness fixes (P0)

- תוקן `NameError` ב-`build_fatigue_assessment`: `training_intelligence` לא היה מיובא ב-`coach_bot.py`.
- תוקנה קריסת מסך "כך הבנתי אותך": `_format_fact_value` נקרא עם ארגומנט אחד; הפך defensive ולא קורס על ערך חסר/פגום.
- "לא אלכוהול" וטקסט שלילה/העדפה אינם נשמרים עוד כארוחה — נוסף intent `set_dietary_pref` + זיהוי שלילה לפני ניתוח ארוחה.
- ארוחה ריקה/אפס-קלוריות/אפס-מאקרו נחסמת בשלוש שכבות (טקסט, תמונה, persist).
- RIR אינו מומצא: לחיצה אחת שומרת RIR לא-ידוע (sentinel), וה-progression מבדיל ידוע מלא-ידוע.
- אימון חלקי/0-דקות אינו מוצג כ"הושלם"; הסיכום מציג מתוכנן מול בוצע וסטטוס אמיתי.
- יעד מחושב עם נתוני חובה חסרים (משקל/מטרה/מין/גיל) אינו הופך active; יעד זמני מסומן `active_provisional`.
- חסימת readiness נמוך בהפעלת תוכנית מוסברת למשתמש במקום קריסה גנרית.
- חשיפת exceptions למשתמש הוחלפה בהודעה ידידותית עם correlation ID; redaction של tokens/keys מהלוגים.

### Engineering / packaging

- מנוע fatigue/deload חובר לזרימת האימון החיה (היה מנותק).
- `scripts/build_release.py`: בניית ארכיון נקי מ-allowlist + בדיקת קבצים אסורים, עם בדיקות.
- `make validate` (compile, ruff, pytest, evaluations, preflight, package) ו-`make release`.
- `.gitignore`/`.dockerignore`/`pyproject.toml` מחריגים `.claude`, `.venv`, `dist`, caches.
- CI מריץ evaluations + package inspection.
- תיעוד עודכן למספר הבדיקות האמיתי (303).

## 2.0.0-rc1 — 2026-06-22

### Conversation foundation

- ConversationRouter מרכזי.
- state יחיד ב־SQLite.
- microflow suspension/resume.
- flow expiry ו־restart recovery.
- versioned callbacks.
- persistent Event Log ו־Replay CLI.

### Profile, readiness and goals

- ארבעה readiness profiles.
- gap/estimate/freshness semantics.
- expanded lifestyle facts and Mini App editor.
- single-source goal versions and manual goal history.
- improved target computation and provisional labeling.

### Planning

- constraint-driven three nutrition candidates.
- constraint-driven three workout candidates.
- Fit Score, rationale, trade-offs and assumptions.
- active plan versioning and unified weekly plan.
- next-best-action, plan freeze and profile conflict detection.

### Meals

- explicit quantities locked.
- cooked/raw clarification.
- per-item clarification targeting.
- macro/calorie quality checks.
- exact and perceptual duplicate detection.
- edit saved meal and undo to draft.
- quality gates before proactive advice.

### Training

- exercise metadata and joint-load safety.
- balanced Full Body template covering squat, hinge, horizontal push, vertical pull and vertical push.
- equipment-aware replacements.
- warm-up sets and quick sessions.
- fatigue, plateau, readiness and deload logic.

### Health and Mini App

- local-day grouping, source dedup and sleep interval union.
- transactional health import and immediate profile refresh.
- Health document import as a resumable conversation microflow.
- persistent Mini App login token consumption.
- profile, dashboard, plan and unified-week APIs.

### Engineering

- migrations through version 8.
- new tests and evaluation harness.
- Ruff/compile/preflight clean.
- clean-environment dependency validation, including `python-multipart` for Mini App uploads.
- no `.env`, DB, images, backups, `.venv` or `.git` in release archive.
## REC-PROGRAM-04 — 2026-06-27

- Completed and audited REC-PROGRAM-04 production integration.
- Added canonical training availability into workout planning, Telegram profile/program views, and Mini App profile/dashboard data.
- Hardened dietary restriction matching for `nut`, `nutmeg`, `coconut milk`, plant milk, and explicit negation phrases.
- Wired canonical dietary IDs into nutrition plan protein-option filtering.
- Added source-aware body-fat normalization for Apple Health and profile display.
- Added deterministic replacement meal corrections before AI fallback.
- Hardened Mini App CSP/XSS posture by removing inline event handlers and escaping server text.
- Fixed Mini App upload body limit handling for Apple Health imports.
- Verified release ZIP: 169 files, 0 forbidden entries.
