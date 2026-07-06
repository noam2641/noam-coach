# CODEX_MASTER_WORK_PLAN — תוכנית עבודה הנדסית מלאה להמשך פיתוח Noam Coach

**תאריך:** 2026-07-03
**Baseline מאומת:** ענף `codex/complete-rec-program-04`. סוויטת הבדיקות המלאה הורצה בפועל על עץ העבודה הנוכחי — **כל הבדיקות עוברות (exit 0, ‏664 פונקציות בדיקה ב-72 קבצים)**.
**מסמך אח:** `CODEX_WORK_PLAN_RE10.md` — מכיל 15 משימות מפורטות (RE10-1..RE10-15) + 15 דיוקי קוד (D1–D15) מסבב הבדיקות הידני. **מסמך זה לא מחליף אותו** — הוא עוטף אותו: המשימות של RE10 משובצות כאן בתוך ה-Phases, עם הפניה לפירוט המלא שם. משימות חדשות שנמצאו בסריקה הנוכחית מסומנות **M-##**.
**שיטת האימות:** כל קביעה במסמך אומתה בקריאת הקוד בפועל. כשמשהו לא אומת עד הסוף — כתוב במפורש **"דורש אימות"**. אין ניחושים.

---

# חלק 1 — סריקת מצב קיים: מפת המערכת המלאה

## 1.0 מבנה הפרויקט בפועל

```text
noam_coach_complete_release/
├── coach_bot.py            ← composition root; re-export לתאימות לאחור
├── config.py               ← Settings (pydantic-settings), OPENAI_CLIENT, TZ, LOGGER, RUNTIME_STATE
├── db.py                   ← Database wrapper + כל ה-schema + SCHEMA_MIGRATIONS (9 מיגרציות)
├── mini_api.py             ← FastAPI router של ה-Mini App
├── conversation.py         ← FSM: FlowName, active_flow, encode_callback, version guard
├── onboarding.py (root)    ← shim/legacy — הקוד החי ב-noam_coach/bot/onboarding.py (דורש אימות אילו שאריות חיות בו)
├── assistant.py (root)     ← Intent router: 17 actions, AI + keyword fallback
├── questions.py            ← קטלוג שאלות (safety/plan/JIT/lifestyle), _is_relevant, DEFERRED_GAP_KEYS
├── user_model.py           ← facts, READINESS_PROFILES (מוכנות-נתונים), gaps, confirm/invalidate
├── targets.py              ← BMR/TDEE (Mifflin-St Jeor), compute_targets, explain_targets
├── planning.py             ← goal proposal, plan candidates (תזונה/אימונים), unified week, activate
├── exercise_plans.py       ← תבניות A/B/C/F, SPLIT_BY_FREQUENCY, exercise() builder, EXERCISE_MUSCLES
├── training_intelligence.py← קטלוג תרגילים/שרירים, adapt_exercises, fatigue/plateau, readiness_score
├── routine.py              ← למידת שגרה מנתוני Health: שינה, אימונים, חלונות אכילה
├── health_import.py        ← פרסר סטרימינג ל-export.xml (>1GB), חלון 18 חודשים, דחיסת דגימות
├── health_service.py       ← upsert health rows, sync לfacts, routine profile, freshness
├── reconcile.py            ← השוואת מדווח-מול-מיובא רטרוספקטיבית, הצעות לאישור
├── meal_intelligence.py    ← תיקוני ארוחה דטרמיניסטיים, fingerprint, זיהוי כפילויות, locked quantities
├── recommendations.py      ← AI: תפריט בוקר, הכוונה תוך-יומית, סיכום ערב, מוטיבציה
├── coach_intelligence.py   ← next_best_action, קונפליקטים בפרופיל, plan freeze, coaching brief
├── data_quality.py         ← שערי איכות לארוחות/ימים/יעדים/פרואקטיבי
├── evaluation.py           ← הרצת רגרסיה offline על מקרים JSONL (ל-CI)
├── event_log.py            ← product events + replay דטרמיניסטי
├── feature_flags.py        ← 6 דגלים (router v2, smart plans v2, mini app plans...)
├── retention.py            ← ניקוי תמונות (30 יום) ונתונים תפעוליים
├── models.py               ← Pydantic: MealAnalysis, FoodItem, HealthBatch, RoutineExtraction...
├── helpers.py              ← esc, friendly_error, today_bounds_utc, utc_now
│
├── noam_coach/
│   ├── app/runtime.py      ← בניית Application, רישום handlers, schedule_jobs
│   ├── bot/                ← onboarding.py (2,170 שורות — הקובץ החי), callback_router.py,
│   │                          callback_menu.py, callback_plans.py, callback_meals.py,
│   │                          callback_session.py, meals.py, meal_text.py, workout.py,
│   │                          workout_runtime.py, checkins.py, assistant.py, ui.py
│   ├── services/           ← next_meal.py (2,211), nutrition_context.py, planning-goals.py,
│   │                          health_jobs.py, availability.py (612), dietary_restrictions.py (813),
│   │                          meal_validation.py, food_preferences.py, body_fat.py, daily_state.py,
│   │                          decision_engine.py, explainability.py, prompt_builder.py,
│   │                          daily_coaching.py, meal_followup.py, core.py (approvals+flow state),
│   │                          profile.py (קריאות AI), local_health_path.py, telegram_errors.py, weekdays.py
│   ├── api/                ← mini_auth.py, security.py (rate limit+body cap), health_routes.py
│   │                          (HealthKit/Shortcuts ingestion), watch_routes.py, system_routes.py
│   └── jobs/proactive.py   ← claims, priorities, quiet hours, DailyContext
│
├── miniapp/                ← index.html (83), app.js (315), styles.css (24) — מינימלי
├── tests/                  ← 72 קבצים, 664 בדיקות, כולל tests/regression + tests/acceptance
├── scripts/                ← backup, restore, migrate_db, preflight, smoke_test, build_release,
│                              export_user_data, delete_user_data, generate_secrets, replay_session,
│                              run_evaluations
├── evaluations/            ← מקרי JSONL ל-evaluation.py
├── docs/, examples/, dist/, backups/
└── Dockerfile, compose.yaml, Caddyfile, Makefile
```

**אזהרה:** עשרות קבצי `" - Copy"` בשורש (כולל `.git - Copy/`!) — זבל. אסור לערוך/לייבא מהם. ניקויים — רק באישור המשתמש (משימה M-01).

## 1.1 שכבת הרצה והרכבה

| רכיב | מה עושה היום | איפה | שלם? | בעיות/חסר | תלויים בו |
|---|---|---|---|---|---|
| coach_bot.py | composition root; מייבא ומרכיב הכול; re-exports לתאימות | שורש | שלם | קובץ רגיש; אין לגעת בלי צורך | הכול |
| runtime.py | בניית Application, רישום handlers ו-jobs | `noam_coach/app/runtime.py` | שלם | אין handler ל-VOICE (ראה M-02) | כל ה-flows |
| config.py | Settings מלא: טלגרם, OpenAI (gpt-4.1-mini), מגבלות ייבוא (zip-bomb protection: upload 300MB, xml 4GB, compression ratio 250), פרואקטיבי (limit 4/יום, gap 75 דק', שקט 22:30–07:00), ברירות מחדל 2100/150/8000 | שורש | שלם | — | הכול |
| feature_flags.py | 6 דגלים מ-env, ברירת מחדל דלוקים | שורש | שלם | — | router, plans, mini app |

**פקודות רשומות:** `/start /import /importpath /flags /profile /weekly /chart /app /cancel` + PHOTO→`handle_photo`, Document→`handle_document`, TEXT→`handle_text_message`, callbacks→`handle_callback` (runtime.py:199-211).

**Jobs מתוזמנים (runtime.py:153-165):** `morning` (daily), `evening` (daily), `calorie_watch` (כל 30 דק'), `motivation` (כל 30 דק'), `workout_prompt` (כל שעה), סיכום שבועי (daily). עוברים דרך מנגנון claims של `jobs/proactive.py` (עדיפויות 10–100, retry, quiet hours, תקציב יומי).

## 1.2 שכבת Telegram (bot/)

| רכיב | מה עושה | שלם? | בעיות ידועות |
|---|---|---|---|
| callback_router.py | ניתוב לפי prefix במפל קבוע (שורות 223–251); version guard לכפתורים ישנים; debounce לחיצות כפולות | שלם | **באג RE10-1:** prefix ‏`routine:` לא מנותב → כפתורי אישור שגרה מתים |
| onboarding.py (bot) | ‏2,170 שורות: onboarding, plan-completion, פרופיל, snapshot, candidate list, unified render | חלקי | RE10-2 (זיהום dict), RE10-6/7 (סדר שאלות/כפילות ציוד), D6/D7 |
| callback_menu.py | תפריט, health:activate/review, confirm:goal_cal, approve/reject goal | שלם | D3 (approvals נערמים) |
| callback_plans.py | hub תוכניות, ג'נרציה, בחירה, unify, עריכת פרמטרים, blocked screens | חלקי | RE10-8 (active_goal), RE10-9 (מסך יעד), RE10-11 (אין אשף) |
| callback_meals.py + meals.py + meal_text.py | צילום→ניתוח→טיוטה→אישור/עריכת כמויות/תיקון מלל/דחייה; דדופ; clarifications | שלם ברובו | RE10-15 (מאכלים ישראליים), D5 (בלי הקשר אלרגיות בניתוח ראשון) |
| callback_session.py + workout.py + workout_runtime.py | אימון חי: סטים, RIR, מנוחה+טיימר, split sets, undo, החלפת תרגיל | שלם ברובו | חיווט readiness_score לתוך flow האימון — **דורש אימות** (ראה M-05) |
| checkins.py | check-in (chk:), מסך בריאות, "מה עכשיו" | קיים | היקף השימוש בפועל — דורש אימות |
| ui.py | מקלדות, safe_edit, render workout overview | שלם | — |
| assistant.py (bot) | חיבור intents לפעולות; אישורים מספריים | שלם | — |

## 1.3 FSM — conversation.py (מצב אמיתי, מאומת)

- **23 states ב-`FlowName`** (conversation.py:25-48): `idle, onboarding_question, lifestyle_capture, profile_confirmation, plan_frequency, deferred_question, pain_location, avoidance_detail, allergy_detail, equipment_detail, med_name, basics_fix, meal_logging, meal_correction, routine_confirm, confirm_number, goal_review, nutrition_plan_selection, workout_plan_selection, unified_plan_review, workout_parameter_edit, workout_session, health_import`.
- קבוצות: `QUESTION_FLOWS` (10), `MICROFLOWS` (7), `LONG_FLOWS` (7).
- טבלת `active_flow` — שורה אחת למשתמש; suspend/resume; expiry; version+flow_id בכל כפתור דרך `encode_callback`.
- שכבה שנייה: `conversation_state` (core.py) ל-flows בעלי שם (plan_completion, pending_plan_action) — **שתי מערכות state במקביל**; עובד, אבל מפתח חדש חייב להבין ששתיהן קיימות.

## 1.4 DB — db.py (מאומת מלא)

**28 טבלאות:** `schema_migrations, users, goals, approvals, meals, meal_items, sessions, sets, health, audit, routine_profile, daily_flags, job_state, user_facts, user_fact_history, medical_constraints, medication_events, conversation_state, analytics_events, exercise_overrides, active_flow, goal_versions, product_events, plan_versions, active_plans, meal_fingerprints, mini_login_tokens, plan_feedback`.

**מיגרציות (db.py:554-564):** מנגנון מסודר — `SCHEMA_MIGRATIONS` ממוספרות 1–9 (`single_active_session, job_delivery_state, foreign_keys_and_health_identity, active_flow_table, goal_versions_table, planning_and_event_tables, active_flow_expiry, meal_origin, single_active_goal_version`) + אכיפת FK על 24 טבלאות (`TARGET_FOREIGN_KEYS`). `scripts/migrate_db.py` קיים.

**כיסוי אינדקסים:** קיימים אינדקסים בסכימה — הרשימה המלאה **דורשת אימות** (משימה M-09 בודקת ומשלימה).

## 1.5 שכבת AI (מאומת)

| רכיב | תפקיד | הערות |
|---|---|---|
| profile.py: analyze_meal_image / analyze_meal_text / reanalyze_meal_with_text_and_image | ניתוח ארוחות (Responses API + structured output → MealAnalysis) | פרומפטים משוכפלים inline; אין locale ישראלי (RE10-15); ניתוח ראשון בלי אלרגיות (D5) |
| profile.py: extract_daily_routine | חילוץ שגרה מטקסט חופשי | מזין את routine_confirm |
| assistant.py: classify_intent | ‏17 intents + fallback דטרמיניסטי במילות מפתח | טוב; רשימת ה-actions בשורות 22-40 |
| recommendations.py | תפריט בוקר, next-meals תוך-יומי, סיכום ערב, מוטיבציה | צריך יישור לפורמט "מצב היום" החדש (Phase 7) |
| prompt_builder.py | מעטפת אחידה לכל קריאת AI (domain, context, completeness) | להשתמש בו בכל קריאת AI חדשה (RE10-10, RE10-15) |
| decision_engine.py | DecisionAudit + שער שלמות-הקשר לפני קריאת AI | לחווט לכל flow המלצה חדש |
| meal_validation.py + data_quality.py | ולידציה של פלט AI: מגבלות תזונה, סבירות מאקרו | עיקרון "AI מציע — הקוד מאמת" ממומש |

## 1.6 Apple Health (מאומת)

- **ערוץ 1 — ZIP בטלגרם:** handle_document → import_health_export_file → פרסר סטרימינג (>1GB בלי לטעון לזיכרון), חלון 18 חודשים, דחיסת דגימות, dedup (מדווח duplicates), הגנות zip-bomb בקונפיג. **שלם ועובד.**
- **ערוץ 2 — נתיב מקומי:** `/importpath` + local_health_path.py (whitelist roots). שלם.
- **ערוץ 3 — API חי:** `POST /api/healthkit/samples` + `POST /api/shortcut/health` (health_routes.py, מאחורי `enable_healthkit_api` + token) + Apple Watch companion (watch_routes.py). **התשתית קיימת אך כבויה כברירת מחדל; אין אפליקציית iPhone.** שימוש בפועל — דורש אימות.
- **אחרי ייבוא:** facts לא-מאושרים + gate גורף `health:activate` (יוחלף באשף RE10-4), reconciliation (reconcile.py — מחפש דפוסים חוזרים, מציע שינוי אחד לאישור).
- **Readiness פיזיולוגי:** `training_intelligence.readiness_score` (שורה 343) — שינה/אנרגיה/כאב/דלתא דופק מנוחה/דלתא HRV → ציון 0–100 + המלצה ("מנוחה או אימון קל" / "שמור עומס"). **קיים כמנוע; רוחב החיווט למסכים ולתכנון אימון — דורש אימות** (M-05).

## 1.7 Mini App (מאומת)

- **Backend מלא:** dashboard (יעד+נצרך+readiness+תוכניות+coaching brief), next-meal (GET+עדכון סטטוס אימון POST), profile, plans (get/generate/activate/unified), meals/today, login (טוקנים חד-פעמיים TTL 300s + session TTL 3600s), upload. Auth ב-mini_auth.py; שכבת הגנות ב-security.py (rate limit 120/דק', body cap 5MB).
- **Frontend מינימלי:** ‏315 שורות JS, ‏24 שורות CSS. מציג: הפעולה הבאה, תוכניות פעילות, המלצת ארוחה, בחירת תוכנית. **אין גרפים, אין דשבורד יומי/שבועי עשיר, אין תצוגת משקל לאורך זמן** (גרף משקל קיים רק כתמונה בבוט — `send_weight_chart`).

## 1.8 תשתיות איכות ותפעול (מאומת)

- 664 בדיקות עוברות; תיקיות regression + acceptance ייעודיות.
- evaluation.py + evaluations/ + run_evaluations.py — רגרסיה דטרמיניסטית offline ל-CI.
- event_log + replay_session.py — שחזור שיחה דטרמיניסטי.
- retention.py — ניקוי תמונות (30 יום) ונתונים תפעוליים; privacy: export/delete_user_data.py.
- backup.py/restore.py, preflight.py, smoke_test.py, build_release.py, generate_secrets.py.
- Deployment: Dockerfile, compose.yaml, Caddyfile — קיימים; **תוכן ותקינות — דורש אימות** (Phase 8).
- אבטחה: allowed_user_id יחיד, expected_bot_username, API guards, טוקנים, SECURITY.md.

---

# חלק 2 — טבלת פערים מלאה (מצב קיים מול מוצר רצוי)

חומרה: **P0** שובר זרימה/אמון · **P1** פוגע בערך המרכזי · **P2** שיפור מהותי · **P3** עתידי.

| # | פער | חומרה | השפעה על המשתמש | קבצים מושפעים | פתרון מומלץ | בדיקות נדרשות |
|---|---|---|---|---|---|---|
| G1 | כפתורי אישור שגרה מתים (`routine:*` לא מנותב) | P0 | תקוע ב-onboarding, אובדן אמון | callback_router.py:223 | RE10-1 | routing unit + orphan-prefix test |
| G2 | dict גולמי נשמר ומוצג באלרגיות/מגבלות (2 נתיבים) | P0 | טקסט שבור באנגלית במסכים מרכזיים | onboarding.py (bot) 624-653, 913-937, 1896-1901; DB קיים | RE10-2 + ניקוי דאטה | 4 units + cleanup test |
| G3 | ‏3 הצעות אימונים זהות | P0 | הבחירה חסרת משמעות | planning.py:832-880 | RE10-3 + D13 | deep-compare payloads |
| G4 | `active_goal` באנגלית + לא ברשימת חוסרים ראשונית | P0 | בלבול, מסלול מקוטע | callback_plans.py, user_model.py | RE10-8 + D11 | labels coverage test |
| G5 | אין סבב אישורים אחרי ייבוא Health | P1 | נתונים "מוזרקים" בלי שליטה; שאלות כפולות | health_jobs.py, callback_menu.py | RE10-4 + D15 | wizard flow units |
| G6 | אין השוואת מדווח-מול-נמדד בזמן onboarding | P1 | סתירות שקטות בפרופיל | reconcile.py, health_jobs.py, routine.py | RE10-5 | threshold units |
| G7 | סדר שאלות השלמה שגוי + שאלת ציוד כפולה | P1 | חיכוך ושאלות מיותרות | onboarding.py (bot), questions.py | RE10-6, RE10-7 | ordering units |
| G8 | מסך יעד: בלי שאלות מקדימות, בלי טווח זמן, בלי override בכפתור | P1 | יעד לא מדויק ולא מוסבר | callback_plans.py, targets.py, planning.py | RE10-9 + D1/D2/D3/D4 | wizard units, rate-safety unit |
| G9 | הסבר יעד יבש (template) | P2 | פחות אמון והבנה | targets.py, שירות חדש | RE10-10 (guardrails!) | mock-AI fallback units |
| G10 | אין אשף אימונים מדורג (סוג→מבנה→תרגילים) | P1 | בחירה עמוסה ולא מותאמת | callback_plans.py, onboarding.py (bot) | RE10-11 + D6/D7 | step-transition units |
| G11 | תוכנית שבועית לא ממוינת כרונולוגית | P1 | סדר יום לא קריא | planning.py:1116-1169, onboarding.py:1462-1477 | RE10-12 + D12 | sort units |
| G12 | "מצב היום" דל + מציג נתוני פעילות ישנים | P1 | המסך המרכזי לא נותן ערך | workout.py:120-232, nutrition_context, next_meal | RE10-13 + D9/D10 | allocator units + snapshot |
| G13 | אין עריכת פרופיל | P1 | אין תיקון נתונים בלי onboarding מחדש | onboarding.py (bot), questions.py | RE10-14 | edit-flow units |
| G14 | מאכלים ישראליים לא מזוהים (במבה) | P1 | תיעוד תזונה שגוי | profile.py, מודול חדש israeli_foods | RE10-15 + D5 | lookup + override units |
| G15 | **הודעה קולית מובטחת אך לא נתמכת** — q_daily_routine אומר "אפשר גם לשלוח הודעה קולית" אבל אין `filters.VOICE` handler (אומת: אפס מופעי voice/audio בקוד) | P1 | המשתמש שולח קול → שקט מוחלט | runtime.py, questions.py:301-305, handler חדש | M-02: או תמלול (OpenAI transcription) או הסרת ההבטחה מהטקסט — החלטת מוצר | voice-message unit |
| G16 | Mini App בלי גרפים ודשבורד עשיר | P2 | אין תצוגת מגמות (משקל/קלוריות/חלבון/צעדים) | miniapp/*, mini_api.py | M-03 (Phase 6) | endpoint + render tests |
| G17 | יעד צעדים קופץ 4,789→8,000 | P2 | יעד לא ריאלי | targets.py:144 | D1 | targets units |
| G18 | חיווט readiness_score לאימון בפועל לא מאומת | P2 | ציון קיים אך אולי לא משפיע | training_intelligence.py:343, callback_session.py | M-05: לאמת ולחווט (הצעת הפחתת עומס כשציון נמוך) | wiring unit |
| G19 | פרואקטיביות לא מיושרת לפורמטים החדשים | P2 | הודעות בוקר/ערב בסגנון ישן אחרי Phase 3 | recommendations.py, jobs/proactive.py | M-06 (Phase 7) | format snapshot |
| G20 | followup לארוחות מתוכננות שלא דווחו — חיווט לצ'אט דורש אימות | P2 | פספוס תזכורת טבעית | meal_followup.py, jobs | M-07 | claim/dedup unit |
| G21 | Rate limiting לקריאות AI מהצ'אט (טקסט/תמונה) — **דורש אימות** אם קיים מעבר ל-debounce | P2 | עלות/latency בהצפה | profile.py, config | M-08: לאמת; אם אין — cap פשוט לפי משתמש/דקה | limiter unit |
| G22 | אינדקסי DB — כיסוי לא מאומת | P2 | ביצועים עם נתוני Health גדולים | db.py | M-09: audit ‏EXPLAIN לשאילתות חמות | — |
| G23 | ריבוי משתמשים | P3 | חסום ל-beta | config, אבטחה, DB | מחוץ להיקף המסמך; אל תקשיח single-user חדש | — |
| G24 | קבצי " - Copy" בשורש כולל `.git - Copy` | P2 | בלבול, סיכון עריכה שגויה | שורש הריפו | M-01: ניקוי — **רק באישור המשתמש** | — |
| G25 | onboarding.py בשורש מול noam_coach/bot/onboarding.py — כפילות שמות | P2 | מפתח עורך את הקובץ הלא נכון | שורש | M-10: לאמת שהשורש הוא shim בלבד; לתעד בראש הקובץ | import test |

---

# חלק 3 — תוכנית עבודה לפי Phases

**כלל זהב לכל Phase:** לפני תחילת עבודה — לקרוא את "כללי העבודה המחייבים" ב-`CODEX_WORK_PLAN_RE10.md` סעיף 1 (ניתוב callbacks, מודל facts, state, עקרונות מוצר). בסוף כל Phase — ‏pytest מלא ירוק + בדיקה ידנית בטלגרם של ה-flows שהשתנו.

## Phase 0 — ייצוב ובדיקת בסיס

**מטרה:** אפס באגים חוסמים; רשת ביטחון שמונעת הישנות.
**משימות:**
1. RE10-1 — תיקון ניתוב `routine:*` (callback_router.py:223) + בדיקה ידנית של שלושת המסלולים (D14).
2. RE10-2 — תיקון שני נתיבי דליפת ה-dict + helper משותף + **מיגרציה 10: ניקוי facts מזוהמים** (db.py: להוסיף ל-SCHEMA_MIGRATIONS ‏`(10, "clean_polluted_gap_values")`).
3. טסט "כפתורים יתומים": סריקת כל callback_data סטטי מול משפחות ה-router (חלק מ-RE10-1).
4. D11 — טסט כיסוי labels: לכל key אפשרי ב-missing יש תרגום עברי.
5. M-10 — אימות ש-onboarding.py בשורש הוא shim; תיעוד בראש שני הקבצים.
**קבצים:** callback_router.py, noam_coach/bot/onboarding.py, user_model.py, db.py, tests/.
**DB:** מיגרציה 10 (ניקוי ערכים; ללא שינוי סכימה).
**DoD:** כל הבדיקות עוברות; שלושת כפתורי השגרה עובדים בטלגרם; אין `{` באף מסך; טסט היתומים בסוויטה.

## Phase 1 — UX ושיחה

**מטרה:** זרימת שאלות הגיונית, בלי כפילויות, בלי הבטחות שווא.
**משימות:**
1. RE10-6 — סדר שאלות השלמה לפי התוכנית שביקשה (plan_type בכל השרשרת + resume).
2. RE10-7 — היסק ציוד ממקום אימון (location=`gym` → equipment=`full_gym`; ערכים מאומתים ב-questions.py:178/268).
3. RE10-8 — איחוד רשימות חוסרים + עברית בלבד ב-`_render_planning_blocked`.
4. M-02 — הודעות קוליות: **החלטת מוצר נדרשת** — (א) מימוש: handler ‏`filters.VOICE` → הורדה → תמלול (OpenAI `audio.transcriptions`, מודל דרך Settings) → הזרמה ל-handle_text_message; או (ב) הסרת "אפשר גם לשלוח הודעה קולית" מ-questions.py:304. אם מממשים: גודל מקס 2 דק', הודעת "מתמלל..." בינתיים, fallback ידידותי בכשל.
5. איחוד הודעות שגיאה: לוודא שכל exception path עובר דרך `friendly_error` (helpers.py) — audit קצר, בלי refactor רחב.
**UX:** כל שאלה עם כפתורים כשאפשר; "⬅️ חזור" בכל מסך שאלה.
**DoD:** flow השלמה מ-nutrition שואל תזונה קודם; אין שאלת ציוד אחרי חדר כושר; קול מטופל או לא מובטח; המסך הראשון מציג את כל החוסרים.

## Phase 2 — תזונה וארוחות

**מטרה:** תיעוד ארוחות מדויק כולל מזון ישראלי, עם הקשר בטיחותי מלא.
**משימות:**
1. RE10-15 — איחוד שלושת הפרומפטים למודול משותף + בלוק locale ישראלי + מודול `israeli_foods.py` (30–50 מוצרים, ערכי יצרן, aliases עברית/תעתיק) + post-processing עם דריסה רק בהתאמה ודאית.
2. D5 — הזרמת בלוק ההקשר (אלרגיות+מגבלות, "safety context only") גם ל-analyze_meal_image ו-analyze_meal_text.
3. אימות שרשרת הוולידציה: כל MealAnalysis עובר meal_validation + data_quality לפני תצוגה (audit קיים — לוודא שאין נתיב עוקף; **דורש אימות** בנתיב ה-Mini App upload).
**DB:** אין שינוי (הטבלה בקוד, לא ב-DB).
**DoD:** "אכלתי שקית במבה" → ערכי במבה אמיתיים; צילום במבה → זיהוי נכון או לפחות ערכים מיושרים; תיקון משתמש תמיד גובר.

## Phase 3 — "מה לאכול עכשיו" + "מצב היום"

**מטרה:** המסך המרכזי נותן תמונת יום מלאה בפורמט שהמשתמש הגדיר.
**משימות:**
1. RE10-13 — שכתוב `build_daily_status` על גבי `build_nutrition_context`; מקצה יתרה per-slot (**reuse של הלוגיקה מ-allocate_next_meal_budget — D9**); תיוג slots סביב אימון בזמן ריצה (D10); הסרת נתוני פעילות ישנים; 2 אפשרויות pre-workout מ-generate_next_meal_recommendation; כפתורי "דווח ארוחה"/"האימון בוצע".
2. D4 — כשה-goal הוא fallback לא-מאושר: קריאה מפורשת לאישור בכל מסך שמציג אותו.
3. אימות כללי ה-cap הקיימים נשמרים: כל האופציות ≤ יתרה אלא אם חריגה מוסברת (הבדיקות הקיימות של 269/450 ממשיכות לעבור).
**DoD:** "מצב היום" תואם את פורמט המשתמש (מפורט ב-RE10-13); סכום ההקצאות ≤ יתרה תמיד; snapshot test עובר.

## Phase 4 — אימונים

**מטרה:** בחירה אמיתית בין תוכניות שונות, בתהליך מדורג, עם readiness מחווט.
**משימות:**
1. RE10-3 — בידול אמיתי בין 3 ההצעות (תדירות מעל resolve_availability — D13; נפח/תוכן לפי strategy) + D6 (הסרת חיתוך ‎[:4]‎ בתצוגה).
2. RE10-11 — אשף: סוג (מומלץ לפי primary_goal) → מבנה (2–3 וריאציות, אחת מומלצת) → תרגילים (החלפה מ-alts) → אישור. + D7 ("צור מחדש" חוזר לתחילת האשף).
3. RE10-12 — מיזוג כרונולוגי בתוכנית השבועית (items ממוינים; תאימות לאחור ל-meals/workouts — D12).
4. M-05 — ‏readiness: לאמת היכן `readiness_score` נקרא בפועל; לחווט ל-flow האימון: לפני "התחל אימון", אם יש check-in/נתוני שינה שמורידים את הציון מתחת ל-45/70 — הצעה מפורשת "להקל היום?" (הפחתת סט/משקל דרך המנגנון הקיים ב-training_intelligence). לא אוטומטי — תמיד באישור.
**DoD:** שלוש הצעות שונות בתוכן; אשף מלא עובד בטלגרם; שבוע ממוין; readiness משפיע (באישור משתמש) על אימון.

## Phase 5 — Apple Health ו-Readiness נתונים

**מטרה:** ייבוא בשליטת המשתמש, בלי שאלות כפולות, עם התרעות פער.
**משימות:**
1. RE10-4 — אשף אישורים פרטני אחרי ייבוא (תדירות→ימים→שעה→משקל), מחליף את המסך הגורף (D15); facts מאושרים מדכאים שאלות.
2. RE10-5 — התראות פער מדווח-מול-נמדד (ספים: 60 דק' לשעות, 1 לתדירות) על בסיס reconcile.py הקיים.
3. M-11 — החלטת מוצר על ערוץ ה-API החי (health_routes/watch_routes): להשאיר כבוי + לתעד, או לתכנן אפליקציית iOS (מחוץ להיקף). ברירת מחדל: להשאיר כבוי, לתעד ב-ENVIRONMENT_VARIABLES.
**DoD:** העלאת ZIP עוברת סבב אישורים לפני סיכום; אין שאלה חוזרת על נתון מאושר; פער מעל סף מציג הכרעה.

## Phase 6 — Mini App ודשבורדים (M-03)

**מטרה:** דשבורד ויזואלי אמיתי בעברית, מובייל-first.
**משימות:**
1. Endpoint חדש `GET /mini/api/trends` — סדרות זמן: משקל (health), קלוריות/חלבון יומי (meals), צעדים (health), עמידה ביעד — 7/30 יום. חישוב בשרת.
2. Frontend: כרטיסי "היום" (יתרה, חלבון, אימון) + גרפים (קו למשקל, עמודות לקלוריות מול יעד) — ספריית גרפים קלה או SVG ידני; RTL; טעינה מהירה.
3. עריכת פרופיל בסיסית ב-Mini App (משתמש ב-endpoints הקיימים של profile) — משלים את RE10-14.
4. יישור ה-dashboard הקיים לפורמט "מצב היום" החדש (אותם מספרים בדיוק — מקור אחד: nutrition_context).
**API:** trends חדש; אין שינוי DB (שאילתות על קיים).
**DoD:** פתיחת /mini מציגה היום+מגמות; המספרים זהים לצ'אט; עובד במובייל.

## Phase 7 — פרואקטיביות ותזכורות (M-06, M-07)

**מטרה:** הבוט יוזם ברגעים הנכונים, באותה שפה ופורמט כמו שאר המערכת.
**משימות:**
1. יישור job_morning/evening/calorie_watch לפורמטים החדשים (מצב היום, יתרה, slots) — recommendations.py צורך את אותו nutrition_context.
2. חיווט meal_followup.py: ארוחה מתוכננת שלא דווחה בתוך X דקות → תזכורת עדינה אחת (בכפוף ל-budget/quiet hours הקיימים) עם כפתורי "אכלתי — תעד"/"דלג".
3. תזכורת pre-workout (30–60 דק' לפני שעת האימון המתוכננת, אם לא דווח) — משתמש ב-workout_scheduled_time; ותזכורת שקילה שבועית (יום קבוע, רק אם אין משקל טרי מ-Health).
4. כיבוד קשיח של המגבלות הקיימות: proactive_daily_limit=4, min_gap 75 דק', שקט 22:30–07:00 — כל הודעה חדשה עוברת דרך claim_job_delivery. **אסור לעקוף.**
**DoD:** אין יותר מ-4 הודעות יזומות ביום; אין הודעות בשעות שקט; כל תזכורת עם כפתור פעולה וכפתור "הפסק תזכורות כאלה" (עדכון daily_flags/העדפה).

## Phase 8 — בדיקות, אבטחה ו-Deployment

**מטרה:** שחרור בטוח.
**משימות:**
1. השלמת כל הבדיקות מחלק 8 במסמך זה; ‏run_evaluations על evaluations/.
2. M-08 — אימות/הוספת rate limit לקריאות AI מהצ'אט (למשל 10 ניתוחי תמונה/שעה למשתמש; חריגה → הודעה ידידותית).
3. M-09 — audit אינדקסים: ‏EXPLAIN QUERY PLAN לשאילתות החמות (health לפי user+type+time, meals לפי user+יום, product_events) והוספת חסרים במיגרציה 11.
4. סקירת אבטחה לפי SECURITY.md: טוקנים, allowed_user_id, הרשאות קבצים ב-storage, אי-חשיפת stack traces (audit ‏friendly_error), גבולות upload.
5. אימות Deployment: ‏Dockerfile/compose/Caddyfile רצים נקי; ‏preflight.py + smoke_test.py ירוקים בסביבת היעד; backup/restore מתורגלים פעם אחת בפועל.
6. M-01 — ניקוי קבצי " - Copy" (רק באישור המשתמש; קודם גיבוי).
**DoD:** צ'קליסט חלק 10 סגור במלואו.

---

# חלק 4 — פירוט פונקציונלי מלא

## 4.1 תזונה

| יכולת | מצב | מימוש | מה נדרש |
|---|---|---|---|
| תיעוד מתמונה | ✅ קיים | handle_photo → analyze_meal_image → approval → render_meal (שמור/ערוך כמויות/תקן במלל/דחה) | RE10-15, D5 |
| תיעוד מטקסט | ✅ קיים | intent log_meal_text → analyze_meal_text → אותו מסך אישור | RE10-15, D5 |
| תיקון ארוחה | ✅ קיים | fixmeal → reanalyze עם locked_corrections (תיקונים קודמים לא מתהפכים) + _enforce_user_quantities (מספר של המשתמש = חוק) | — |
| אישור לפני שמירה | ✅ קיים | approvals table; consumed רק אחרי approve_meal | — |
| consumed מול planned | ✅ מופרד | daily_state.consumed_meals = מקור אמת; planned ב-active_plans payload | לשמור על ההפרדה בכל פיצ'ר חדש |
| יעד קלורי/חלבון | ✅ קיים | goal_versions + goals; fetch_goal | D4 (fallback גלוי), RE10-9 |
| יתרה יומית | ✅ קיים | goal − consumed (workout.py:145, nutrition_context) | RE10-13 מציג נכון |
| חריגה מהיעד | ✅ קיים | at_or_over policy: נשנוש ≤150 או חריגה מבוקרת באישור | — |
| אלרגיות/העדפות/מגבלות רפואיות | ✅ קיים | dietary_restrictions (813 שורות, IDs קנוניים, aliases עברית) + meal_validation + medical_constraints | RE10-2 מנקה את הערכים |
| המלצה לפי יום אימון/מנוחה | ✅ קיים | WorkoutPhase (7 פאזות) משנה תקציב ותבניות | — |

## 4.2 "מה לאכול עכשיו" — הגדרה מלאה (המצב הקיים נכון, לשמר!)

- **קלטים:** יעד+נצרך (calorie/protein balance), פאזת אימון (`_workout_state`: מתוכנן/דווח/לפני-מיידי/לפני-קרוב/במהלך/אחרי), שעות עד שינה (`_bedtime_hours`), ארוחות שנותרו (`_meals_remaining`), מגבלות/אלרגיות, צום (daily_flags).
- **יתרה:** ‏remaining = goal − consumed; **hard cap** ב-`_cap` (next_meal.py:703-711) — אופציה רגילה לעולם לא חורגת; חריגה רק ליד אימון (עד +35%/+250) או בבקשה מפורשת, תמיד עם הסבר.
- **חלבון חסר:** ‏protein_gap / meals_left → protein_min/max לכל תקציב; ביתרה נמוכה — תבניות protein-dense.
- **מצבי מדיניות:** ‏normal / low_remaining (≤סף) / at_or_over_target / workout / explicit_overage / fasting — כל אחד עם rationale בעברית.
- **בחירת אפשרויות:** תבניות פר-פאזה → סינון מגבלות → התאמה לתקציב → דירוג → **בדיוק 2 אופציות** + כפתורי סטטוס אימון + "איך חשבתי?" (explainability).
- **מה אסור לשבור:** בדיקת ה-269/450 (יתרה 269 → אין הצעת 450); consumed בלבד נספר; אלרגיה לעולם לא מוצעת.

## 4.3 אימונים

- **תוכנית שבועית:** readiness gates (workout+safety) → availability resolver (עדיפות מקורות: corrected>confirmed>reported>health) → 3 מועמדים (אחרי RE10-3: שונים) → בחירה (אחרי RE10-11: אשף) → activate.
- **אימון בפועל (קיים ושלם):** startworkout → sessions row → show_session: תרגיל נוכחי, סט, משקל/חזרות/RIR בכפתורים, טיימר מנוחה, split set (6+2), undo, החלפת תרגיל (alts), עריכת פרמטרים עם persist ב-exercise_overrides.
- **כאב/פציעה:** intent report_pain → medical_constraints → adapt_exercises מסנן/מחליף. קיים.
- **התקדמות:** fatigue/plateau ב-training_intelligence (deload_recommended); recommend_load. חיווט מלא למסכים — דורש אימות (M-05).
- **סיכום אימון:** קיים (workout_summary). שיפור תצוגה — לא בהיקף.

## 4.4 Apple Health

- **העלאת ZIP:** עד 50MB בטלגרם (מגבלת Telegram Bot API — לתעד למשתמש!); מעל זה — ‏/importpath מקומי (עד 4GB XML) או Mini App upload (עד 300MB). **הודעת ההנחיה צריכה לציין את שלוש הדרכים** (להוסיף ב-RE10-4).
- **Parsing/dedup:** סטרימינג, 18 חודשים, upsert עם זהות רשומה (מיגרציה 3), duplicates נספרים ומדווחים.
- **מדדים:** משקל, צעדים, קלוריות פעילות, דופק/מנוחה, שינה, אימונים, body_fat (מנרמל ייעודי).
- **כשלים:** קובץ לא תקין/גדול → הודעה ידידותית (friendly_error); flow ‏health_import נסגר. קיים.
- **Readiness:** ראה M-05.

---

# חלק 5 — UX מלא (עברית, כפתורים תחילה)

## 5.1 תפריט ראשי (קיים ב-home_keyboard; יעד סופי)

```
🍽 מה לאכול עכשיו   |  📸 תיעוד ארוחה
🏋️ האימון שלי        |  📊 מצב היום
🎯 יעדים             |  👤 פרופיל
📈 סיכום שבועי       |  🧠 עדכון מצב (עייפות/כאב/צום)
🌐 Mini App
```
(המבנה הקיים קרוב; ליישר תוויות אחרי Phase 3. השוואה מדויקת מול home_keyboard — בעת המימוש.)

## 5.2 תבנית מסך אישור (סטנדרט מחייב לכל שינוי)

```
<b>אני עומד לעדכן:</b>
{מה בדיוק}

זה ישפיע על: {נגזרות}

[✅ אשר]  [✏️ ערוך]  [❌ בטל]
```
קיים בפועל ליעד/ארוחות/health; האשפים החדשים (RE10-4/9/11) חייבים לרשת אותו.

## 5.3 הודעות שגיאה סטנדרטיות (קיימות ברובן — לוודא אחידות)

| מצב | הודעה |
|---|---|
| כשל AI | "לא הצלחתי לנתח את זה כרגע. שלח שוב או כתוב לי בקצרה מה אכלת." |
| ZIP כושל | "לא הצלחתי לקרוא את קובץ Apple Health. אפשר לנסות לייצא שוב, לשלוח נתיב מקומי עם ‎/importpath, או להמשיך בלי." |
| קלט לא ברור | "לא בטוח שהבנתי. אפשר לבחור:" + כפתורים |
| חריגה קלורית | "כבר עברת את היעד היומי. אם אתה עדיין רעב — עדיף משהו קטן ועשיר בחלבון." |
| כפתור ישן | "הכפתורים בהודעה הישנה כבר לא פעילים." + רינדור המסך הנוכחי (קיים ב-router) |
| קול (אם לא ממומש) | אין להבטיח קול בטקסטים (M-02) |

## 5.4 Flows מלאים (מצב סופי אחרי כל ה-Phases)

**Onboarding:** ‏/start → פתיחה (לא תחליף רופא) → [📤 העלה Apple Health | 🚀 התחל בלי] → שאלות ליבה (כאב, מגבלה רפואית, מטרה, ימי אימון, מין, גיל) → שגרת יום (טקסט/קול) → "זה מה שהבנתי — נכון?" [נכון/לתקן/דלג — **עובד אחרי RE10-1**] → אם הועלה Health: אשף אישורים (RE10-4) + פערים (RE10-5) → תפריט ראשי. השאר נדחה ל-JIT.

**ארוחה:** תמונה/טקסט → "מנתח..." → מסך טיוטה (פריטים+גרמים+קלוריות+חלבון+סה"כ, שאלת הבהרה אחת לכל היותר) → [שמור/ערוך כמויות/תקן במלל/דחה] → נשמר → "מצב היום" מעודכן.

**מה לאכול עכשיו:** כפתור/שאלה → יתרה+פאזה → 2 אפשרויות עם ערכים והסבר → [בחרתי בזה=תעד | תן חלופה | סטטוס אימון | איך חשבת?].

**אימון:** האימון שלי → overview (או readiness prompt אם ציון נמוך — M-05) → התחל → סט-אחר-סט בכפתורים → טיימר → סיכום. מכשיר תפוס → החלף תרגיל.

**שינוי יעד:** יעדים → אשף (גובה/משקל מטרה/טווח — רק החסר) → הצעה+הסבר AI → [אשר | כתוב יעד אחר | דחה].

**Apple Health:** קובץ → "מעבד..." → אשף אישורים פריט-פריט → פערים מול דיווח → סיכום מלא → הצעה להמשיך לתוכניות.

**עקרונות:** מינימום הקלדה — כל קלט שאפשר בכפתור, בכפתור; טקסט חופשי רק לשגרה/תיקונים/כמויות; `/cancel` + "⬅️ תפריט" מכל מקום; אין שאלה על נתון שכבר ידוע ומאושר.

---

# חלק 6 — FSM מלא (מאומת מול conversation.py)

## 6.1 טבלת states

| State (FlowName) | כניסה | קלט מתקבל | יציאה | הערות |
|---|---|---|---|---|
| idle | ברירת מחדל | הכול → intent router | לכל flow | |
| onboarding_question | set_pending(q_id) | טקסט/כפתור qa: | השאלה הבאה/idle | QUESTION_FLOW |
| lifestyle_capture | שאלת שגרה | טקסט חופשי (קול — M-02) | routine_confirm | |
| routine_confirm | אחרי חילוץ שגרה | routine:confirm/fix/skip | המשך onboarding | **שבור עד RE10-1** |
| profile_confirmation | מסכי basics/patterns | onb:basics_ok/fix, onb:pat_* | idle/הבא | |
| plan_frequency | plan:recommend | מספר/כפתור plan:set | ג'נרציה | legacy path |
| deferred_question | JIT | תשובה | חזרה ל-flow העוטף | suspend/resume |
| pain_location / avoidance_detail / allergy_detail / equipment_detail / med_name / basics_fix | שאלות עומק | טקסט | חזרה | MICROFLOWS חלקם |
| meal_logging | תמונה/טקסט ארוחה | אישור/עריכה/תיקון | meal_correction/idle | |
| meal_correction | fixmeal | טקסט תיקון | חזרה לטיוטה | תיקונים קודמים נעולים |
| confirm_number | ערך מספרי רגיש | confirm:{kind}:{value} | idle/resume | תבנית לכל אישור מספרי |
| goal_review | menu:goal | approve/reject/confirm:goal_cal | idle | יורחב באשף RE10-9 |
| nutrition/workout_plan_selection | render_candidate_list | planv2:select (versioned) | idle | expiry ‏24h; יורחב ב-RE10-11 (steps: type/structure/exercises) |
| unified_plan_review | planv2:unify | צפייה | idle | |
| workout_parameter_edit | editparams | param:/wparamtext | חזרה | |
| workout_session | startworkout | session actions (parts) | סיום/ביטול | LONG_FLOW |
| health_import | קובץ התקבל | (עיבוד) | אשף RE10-4 (state חדש health_confirm) | להוסיף enum |

## 6.2 כללי מסגרת (קיימים — לשמר בכל state חדש)

- **הודעה לא קשורה באמצע flow:** ‏QUESTION/MICROFLOW — suspend הנוכחי, טיפול, resume (מנגנון suspended payload). יש רגרסיה קיימת על next-meal — כל state חדש חייב טסט כזה.
- **ביטול:** ‏/cancel + כפתור תפריט → clear/resume; **כל state חדש חייב נתיב ביטול** (עיקרון 1.4 ב-RE10).
- **שגיאה:** ‏friendly_error + ניקוי pending; ‏on_error עם error_id ללוג.
- **כפתור ישן:** version/flow_id mismatch → הודעה + רינדור מצב נוכחי.
- **Restart:** ‏load_pending_state משחזר caches מ-active_flow — כל state חדש עם in-memory cache חייב להשתחזר שם.
- **States חדשים במסמך זה:** ‏`health_confirm` (RE10-4), הרחבת steps ב-workout_plan_selection (RE10-11), אשף יעד (goal_review עם steps). כולם דרך אותו enum + encode_callback.

---

# חלק 7 — DB ומיגרציות

## 7.1 מצב קיים (מאומת)

28 טבלאות (רשימה בחלק 1.4); מיגרציות 1–9 עם `schema_migrations`; FK על 24 טבלאות; ‏migrate_db.py.

**מיפוי לוגי:** ליבה: users, user_facts(+history), goals/goal_versions, approvals · תזונה: meals, meal_items, meal_fingerprints, daily_flags · אימונים: sessions, sets, exercise_overrides, plan_versions/active_plans, plan_feedback · בריאות: health, routine_profile, medical_constraints, medication_events · שיחה: active_flow, conversation_state · תפעול: audit, analytics_events, product_events, job_state, mini_login_tokens.

## 7.2 שינויים נדרשים

| מיגרציה | תוכן | Phase |
|---|---|---|
| 10 ‏`clean_polluted_gap_values` | ניקוי user_facts שערכם מכיל repr של gap-dict (RE10-2) | 0 |
| 11 ‏`hot_query_indexes` | אחרי audit ‏M-09: אינדקסים חסרים (מועמדים: health(user_id,sample_type,start_time), meals(user_id,eaten_at), product_events(user_id,created_at) — **דורש אימות מה כבר קיים**) | 8 |
| — | ‏goal_timeframe_weeks: ‏fact חדש ב-user_facts — אין שינוי סכימה | 3 (RE10-9) |
| — | ‏items בתוכנית מאוחדת: בתוך payload JSON — אין שינוי סכימה | 4 |
| — | טבלת מאכלים ישראליים: קבוע בקוד, לא DB | 2 |

**מה חסר לשמירה (לוודא בעת המימוש):** ‏source של דריסת israeli_foods על פריט ארוחה (שדה בתוך meal_items JSON/notes — בלי מיגרציה); העדפות תזכורות פר-סוג (daily_flags או fact — החלטה ב-Phase 7).

---

# חלק 8 — תוכנית בדיקות

**בסיס:** 664 בדיקות עוברות היום (אומת בהרצה). כל משימה מוסיפה את הבדיקות שהוגדרו לה ב-RE10; להלן המפה הכוללת, כולל הבדיקות המחויבות שביקש המשתמש:

| בדיקה מחויבת | סטטוס | היכן/מה להוסיף |
|---|---|---|
| יעד קלורי 2100 | ✅ קיימת | לוודא שממשיכה לעבור אחרי RE10-9 |
| יתרה 269 → אין הצעת 450 | ✅ קיימת | רגרסיה קבועה; אסור לגעת |
| ארוחה מתמונה / תיקון / אישור | ✅ קיימות | להוסיף: תיקון על פריט israeli_foods (התיקון גובר) |
| consumed מול planned | ✅ קיימת | + snapshot מצב היום לא סופר planned |
| אלרגיות | ✅ קיימת | + ניתוח ראשון מקבל אלרגיות (D5) |
| יום אימון / לפני / אחרי | ✅ קיימות (WorkoutPhase) | + תיוג slots סביב אימון (D10) |
| אימון מלא עם סטים | ✅ קיימת | + readiness prompt (M-05) |
| Health ZIP תקין/כושל | ✅ קיימות | + אשף אישורים (RE10-4): סדר צעדים, תיקון ערך, דיכוי שאלה |
| Mini App dashboard | ✅ קיימת (test_miniapp_render, test_mini_profile_api) | + trends endpoint (Phase 6) |
| FSM הודעה לא קשורה | ✅ קיימת | + עבור health_confirm ואשף האימונים |
| כפתורי callback | ✅ קיימות | + **טסט היתומים** (כל prefix ממופה) — החשוב ביותר |

**חדשות עיקריות לפי Phase:** ‏P0: ניתוב routine, זיהום dict (2 נתיבים), ניקוי, labels; ‏P1: סדר השלמה, היסק ציוד, איחוד חוסרים, voice; ‏P2: israeli lookup/override; ‏P3: allocator (סכום≤יתרה), snapshot; ‏P4: בידול הצעות (deep-compare), אשף (מעברי שלבים, stale), מיון כרונולוגי; ‏P5: אשף health, ספי פערים; ‏P6: trends; ‏P7: budget/quiet/claim לכל תזכורת חדשה; ‏P8: evaluations מלא.

**End-to-end ידני (לכל release):** הצ'קליסט בחלק 10.3.

---

# חלק 9 — חובות טכנולוגיים וסיכונים

## 9.1 Known issues (מאומתים)
G1–G4 (חלק 2) + הזיהום השמור ב-DB (חייב ניקוי, לא רק fix) + הבטחת קול ללא מימוש (G15).

## 9.2 Refactor נדרש (מדורג, לא לפני Phase 4)
- פיצול noam_coach/bot/onboarding.py (2,170 שורות, אחריות מעורבת: onboarding+פרופיל+plan rendering) — רק אחרי שהאשפים מתייצבים.
- איחוד שכבות ה-state (active_flow + conversation_state) לממשק אחד — לא דחוף; לתעד קודם.
- shim-ים בשורש (onboarding.py, coach_bot re-exports) — לצמצם בהדרגה; לא לשבור imports של בדיקות.

## 9.3 אזורים רגישים (אסור לשבור)
1. `next_meal.py` — כללי cap; כל שינוי = רגרסיה מלאה. 2. `db.py` — סכימה/מיגרציות. 3. `callback_router.py` — מפל הניתוב. 4. `daily_state` — consumed. 5. ‏quiet hours/budget בפרואקטיבי. 6. הפרדת planned/consumed. 7. ‏encode_callback/version guard. 8. פרומפטים — כל שינוי דרך evaluation.

## 9.4 סיכונים
| סיכון | הסתברות | חומרה | מיטיגציה |
|---|---|---|---|
| רגרסיה בהמלצות בעקבות RE10-13 | בינונית | גבוהה | reuse ‏allocate_next_meal_budget (D9), snapshot tests |
| האשפים שוברים flows קיימים | בינונית | גבוהה | flags נפרדים? לפחות בדיקות מעברים+stale לכל שלב |
| ניקוי הדאטה מוחק ערך אמיתי | נמוכה | גבוהה | dry-run + גיבוי (scripts/backup.py) לפני מיגרציה 10 |
| טבלת israeli_foods דורסת בטעות | בינונית | בינונית | דריסה רק בהתאמה ודאית; סימון מקור; טסט אי-דריסה |
| עריכת קובץ " - Copy" בטעות | בינונית | בינונית | M-01 ניקוי מוקדם (באישור) |
| עלויות AI בלי limiter | נמוכה | בינונית | M-08 |

## 9.5 דורש בדיקה ידנית תמיד
שלושת כפתורי routine (D14) · אשף health מקצה לקצה עם ZIP אמיתי · אשף אימונים מלא · צילום במבה אמיתי · הודעה קולית (אחרי M-02) · Mini App במובייל אמיתי · restart באמצע כל אשף (שחזור state).

---

# חלק 10 — פלט סופי

## 10.1 סדר עדיפויות מחייב

```
Phase 0  (P0): RE10-1, RE10-2+ניקוי, טסט יתומים, D11, M-10
Phase 1  (P1): RE10-6, RE10-7, RE10-8, M-02
Phase 2  (P1): RE10-15, D5
Phase 3  (P1): RE10-13, D4, D9, D10
Phase 4  (P1): RE10-3, RE10-11, RE10-12, D6/D7/D12/D13, M-05
Phase 5  (P1): RE10-4, RE10-5, D15, M-11
Phase 6  (P2): M-03 (trends+גרפים+עריכת פרופיל ב-Mini)
Phase 7  (P2): M-06, M-07
Phase 8  (P2): M-08, M-09, אבטחה, deployment, M-01
לרוחב כל השלבים: RE10-9, RE10-10, RE10-14 (אשף יעד+הסבר AI+עריכת פרופיל) — לשבץ אחרי Phase 3 ולפני Phase 6.
```

## 10.2 רשימת משימות לפי קבצים (עיקרי)

| קובץ | משימות |
|---|---|
| noam_coach/bot/callback_router.py | RE10-1, טסט יתומים |
| noam_coach/bot/onboarding.py | RE10-2, RE10-6, RE10-7, RE10-14, D6, D7 |
| user_model.py | RE10-2 (get_value), D11 |
| db.py | מיגרציות 10–11 |
| planning.py | RE10-3, RE10-12, RE10-8 (goal gap), D13 |
| callback_plans.py | RE10-8, RE10-9, RE10-11 |
| callback_menu.py | RE10-4 (health steps), D3 |
| targets.py | D1, D2 |
| שירות חדש goal_explainer | RE10-10 |
| noam_coach/services/health_jobs.py | RE10-4, RE10-5, D15 |
| noam_coach/services/profile.py + meal_prompts חדש + israeli_foods חדש | RE10-15, D5 |
| noam_coach/bot/workout.py | RE10-13 |
| noam_coach/services/next_meal.py / nutrition_context.py | מקצה slots (RE10-13, D9, D10) |
| noam_coach/app/runtime.py | M-02 (voice) |
| callback_session.py / training_intelligence.py | M-05 |
| recommendations.py / jobs/proactive.py / meal_followup.py | M-06, M-07 |
| mini_api.py + miniapp/* | M-03 |
| questions.py | RE10-7, M-02 (טקסט) |

## 10.3 Checklist למפתח (לפני כל commit)
- [ ] קראתי את כללי סעיף 1 ב-RE10 (ניתוב, facts, state, עקרונות)
- [ ] כל callback חדש ממופה במשפחת router ועובר את טסט היתומים
- [ ] אין get_value לתצוגה בלי סינון gap
- [ ] כל flow חדש: ביטול, תפריט, טסט הודעה-לא-קשורה, שחזור אחרי restart
- [ ] מספרים מחושבים בקוד; AI רק מנסח; ולידציה על פלט AI
- [ ] עברית בלבד למשתמש; אין keys טכניים
- [ ] pytest מלא ירוק; הוספתי את הבדיקות שהוגדרו למשימה
- [ ] לא נגעתי בקבצי " - Copy"

## 10.4 Checklist לבדיקות (לפני release)
- [ ] pytest מלא + run_evaluations ירוקים
- [ ] צ'קליסט הידני של RE10 (Verification שם) + סעיף 9.5 כאן
- [ ] smoke_test.py + preflight.py בסביבת היעד
- [ ] backup לפני עליית מיגרציות; restore נוסה
- [ ] אין הודעה יזומה מחוץ ל-budget/שעות שקט (בדיקת יום מלא)

## 10.5 Definition of Done — פר Phase
Phase נחשב סגור רק כאשר: כל משימותיו הושלמו כולל דיוקי D המשויכים · הבדיקות החדשות בסוויטה וכולן ירוקות · בדיקה ידנית של ה-flows שהשתנו בוצעה בטלגרם אמיתי · אין רגרסיה (כל ה-664+ עוברות) · טקסטים בעברית טבעית נסקרו מול המשתמש כשמדובר במסך חדש (RE10-13, אשפים) · המסמכים (RE10 / מסמך זה) עודכנו אם ההחלטה בשטח סטתה מהתכנון.

---

# חלק 11 — סקירת מומחה: תזונאי ספורט + מאמן כוח (ביקורת מקצועית על הלוגיקה עצמה)

נבדקה הלוגיקה הדומיינית בפועל (targets.py, next_meal.py, exercise_plans.py, training_intelligence.py) מול פרקטיקה מקצועית מקובלת בתזונת ספורט ובתכנות אימוני התנגדות. **מטרת הפרק: לוודא שהמערכת באמת מיטיבה עם המתאמן, לא רק שהקוד עובד.**

## 11.1 מה שכבר נכון מקצועית (לשמר — אסור "לשפר")

| נושא | למה זה נכון |
|---|---|
| חלבון 2.0 ג'/ק"ג בגירעון, עם משקל ייחוס = משקל יעד/מסה רזה (targets.py:129-141) | פרקטיקה מיטבית: בגירעון חלבון גבוה משמר שריר, וייחוס למשקל יעד מונע over-prescription באחוזי שומן גבוהים. תקרת 220 ג' ורצפת 90 ג' — סבירות |
| bonus אימון מוגבל ל-150 קל' ומופחת כשצעדים גבוהים (targets.py:120-124) | מונע ספירה כפולה של הוצאה — טעות נפוצה אצל מתאמנים שמנפחת יעדים |
| רצפת בטיחות 1400 קל' | שמרני ובטוח לגבר; מונע גירעון קיצוני |
| hard cap על יתרה + חריגה רק ליד אימון ומוסברת (next_meal._cap) | זה בדיוק מה שמאמן אמיתי עושה: גמישות סביב אימון, משמעת בשאר היום |
| חלוקת חלבון per-meal (protein_min/max לכל תקציב) | תואם עיקרון פיזור חלבון (~0.4-0.55 ג'/ק"ג לארוחה) לסינתזת חלבון מרבית |
| התקדמות עומסים לפי RIR + double progression + deload בעייפות/פלטו | שיטת התקדמות מקובלת ובטוחה; RIR הוא הכלי הנכון למתאמן ביתי-עצמאי |
| readiness: שינה/אנרגיה/כאב/RHR/HRV עם ספי 45/70 והמלצות שמרניות | היוריסטיקה שקופה וסבירה; לא מתיימרת לאבחנה |
| ממוצע משקל 7 ימים לצד מדידה אחרונה | חובה מקצועית — משקל יומי רועש; המערכת כבר עושה זאת |

## 11.2 תיקוני מומחה נדרשים (E1–E7) — משובצים במשימות קיימות

### E1 — גירעון קבוע ‎-450 קל' → גירעון נגזר מקצב ויעד (→ RE10-9, Phase 3)
`GOAL_ADJUSTMENT["fat_loss_muscle_retention"] = -450` (targets.py:19-24) הוא קבוע עיוור: למשתמש כבד (תחזוקה 2,650) זה ~17% — סביר; למשתמש קל (תחזוקה 1,800) זה 25%+ — אגרסיבי מדי. **תיקון:** ברגע שקיים `goal_timeframe_weeks` (RE10-9), הגירעון היומי ייגזר מהקצב: `deficit = (kg_per_week × 7700) / 7`, מוגבל (clamp) ל-**10%–25% מהתחזוקה**. בלי טווח זמן — נשאר ‎-450 כברירת מחדל אך עם clamp האחוזים. בדיקות: משתמש 70 ק"ג לא מקבל גירעון >25%; משתמש 100 ק"ג עם קצב 0.5 ק"ג/שבוע מקבל ~550.

### E2 — ארוחת הלילה: לא רק "קלה" אלא חלבון איטי (→ RE10-13, Phase 3)
בפורמט "מצב היום" ארוחת הלילה הוגדרה "~150 קל'". מקצועית, בגירעון עם דגש שימור שריר, חטיף לפני שינה עדיף **חלבון-דומיננטי** (קוטג', יוגורט חלבון, גבינה — קזאין איטי). **תיקון:** ה-slot הלילי במקצה יקבל תמיד תבניות protein-dense (קיימות ב-`_low_remaining_templates`), לא פחמימה ריקה. בדיקה: אופציות הלילה ≥12 ג' חלבון.

### E3 — רגליים פעם בשבוע בפינת "כתפיים ורגליים" — חולשה תכנותית אמיתית (→ RE10-3 + RE10-11, Phase 4)
ב-`SPLIT_BY_FREQUENCY[3] = A/B/C` הרגליים מופיעות רק ב-C, חולקות אימון עם כתפיים (3 תרגילים). לירידה בשומן ושימור מסה, תדירות 2×/שבוע לקבוצת שריר עדיפה, ורגליים הן המנוע הקלורי הגדול ביותר. **תיקון (משתלב באשף שלב ב'):** להציע ל-3 ימים גם מבנה **Full-Body ×3** (תבנית F קיימת!) וגם **Upper/Lower/Full**, עם Full-Body כמומלץ למתחילים ולמטרת ירידה בשומן; A/B/C נשאר כאופציה למי שמעדיף. זה גם פותר את "אין גיוון" (G3) בדרך הנכונה מקצועית — הווריאציה האמיתית היא במבנה, לא רק בטקסט.

### E4 — גבולות נפח שבועי לוריאנטים (→ RE10-3, Phase 4)
כשהוריאנטים יובדלו בנפח: לוודא ש-consistency לא יורד מתחת ל~6 סטים/שריר/שבוע (מינימום אפקטיבי) ו-performance לא עולה מעל ~20 (תקרת התאוששות למתאמן ביניים). להוסיף כ-assertion בבדיקות היחידה של הבידול.

### E5 — שרירים משניים + חיבור mind-muscle (→ Phase 4, משימה חדשה M-13)
לתבניות יש רק `muscle` יחיד + cues. ב-`training_intelligence.CATALOG` כבר קיימים `primary_muscles` כ-tuple (למשל chest+triceps). **משימה M-13 (קטנה):** להעשיר את תצוגת התרגיל (render_workout_overview / show_session) בשריר משני מתוך ה-CATALOG הקיים + cue — בלי שינוי סכימה. זו בקשה מקורית של המשתמש (מסמך החפיפה 5.8) שלא כוסתה עד כה.

### E6 — רקליברציה חודשית של היעד (→ משימה חדשה M-12, Phase 7)
מאמן אמיתי לא קובע יעד ושוכח: אחרי 3–4 שבועות הוא משווה קצב בפועל (ממוצע 7 ימים מול 7 ימים לפני חודש) מול הקצב המתוכנן. **משימה M-12:** job חודשי (דרך מנגנון ה-claims הקיים) שמחשב את הפער; אם הקצב בפועל סוטה >40% מהמתוכנן במשך חודש — **הצעה** (לא שינוי אוטומטי!): "ירדת בפועל X במקום Y — לעדכן יעד ל-Z קל' או להוסיף 1,500 צעדים?" עם אישור. משתמש ב-plan freeze של coach_intelligence כדי לא להציע בתוך חלון ההקפאה. בדיקות: סטייה קטנה → אין הצעה; סטייה גדולה → הצעה אחת בלבד.

### E7 — יעד צעדים הדרגתי (חיזוק D1 בנימוק מקצועי)
קפיצה 4,789→8,000 (+67%) היא מרשם לאי-היענות ולכאבי עומס. הכלל המקצועי: העלאה של 1,000–2,000 צעדים ותו לא, ועדכון רק אחרי עמידה עקבית. זה מאשש את D1 — לממש בדיוק כמתואר שם.

## 11.3 מה נבדק ונמצא תקין — לא לגעת
מנגנון ה-fasting (fast_break עדין), עדיפויות התזמון סביב אימון (pre-near 360–650 עם פחמימה+חלבון מתון, post עם דגש חלבון 30–45 ג'), ההימנעות מייעוץ רפואי, ה-constraints הקשיחים על אלרגיות, וההיגיון של "הצעה תמיד עם הסבר". אלו עומדים בסטנדרט מקצועי — כל "שיפור" בהם הוא סיכון רגרסיה מיותר.

## 11.4 שילוב בתוכנית
- E1, E2 → מתווספים ל-acceptance criteria של RE10-9 ו-RE10-13 (עודכן גם ב-RE10).
- E3, E4, E5 (M-13) → Phase 4.
- E6 (M-12) → Phase 7.
- E7 → כלול ב-D1.

— סוף המסמך —
