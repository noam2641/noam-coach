# דוח עבודה ביניים - Noam Coach

תאריך עבודה: 2026-07-06

מטרת הדוח: מיפוי לפני המשך יישום ה-audit המתוקן על בסיס 29 התמונות. הדוח מתעד איפה נמצאות התשתיות הקיימות, מה כבר קיים חלקית, ומה חייב להתחזק לפני עבודה על Nutrition ו-Training.

## סדר עבודה מחייב

1. Telegram-safe: ACK בטוח, stale callback recovery, safe edit/reply, מניעת הודעות כפולות, טיפול Network/DNS transient.
2. State/Parsing: פירוש תשובות לפי `active_flow.step`, מניעת ערבוב בין גובה/משקל/יעד/שינה/קלוריות/אלרגיות.
3. Data confidence/provenance: לכל נתון מקור, אמינות, אישור, freshness ופעולה נדרשת.
4. Onboarding confirmation: מסך אישור נתוני בסיס, dedupe שאלות, next action דינמי.
5. Nutrition: מצב היום ומה לאכול עכשיו לפי נתוני היום ואמינותם.
6. Training: split מלא, ABC נכון, planned/actual, progression, pain-aware, substitutions.

## קבצים אחראיים ל-Telegram callbacks

- `noam_coach/bot/callback_router.py`: נקודת כניסה מרכזית ל-callbacks, כולל בדיקת גרסת flow ומסכי stale.
- `noam_coach/bot/callback_menu.py`: תפריטים, יעדים, Health, next action וחלק מהניווט.
- `noam_coach/bot/callback_meals.py`: callbacks של ארוחות, תיקונים, ביטולים ושמירה.
- `noam_coach/bot/callback_plans.py`: callbacks של תוכניות ותכנון.
- `noam_coach/bot/callback_session.py`: callbacks של אימון בזמן אמת.
- `noam_coach/bot/ui.py`: כפתורים, `safe_edit`, מקלדות ומסכי אימון.
- `noam_coach/services/telegram_errors.py`: סיווג שגיאות Telegram, transient errors ו-redaction.
- `coach_bot.py`: facade שמייבא ומייצא handlers לצורך תאימות לאחור.

פערים מאומתים:

- `callback_router.py` עדיין קרא ישירות ל-`query.answer()`.
- `safe_edit` התחיל להתחזק אך עדיין צריך לוודא שהוא מתפקד כ-`safe_edit_or_reply` בכל המסכים.
- יש קריאות `query.answer()` ישירות במודולי callback/onboarding נוספים שצריך להעביר ל-wrapper.
- `_is_duplicate_tap` קיים אבל מכסה רק prefixים מסוימים; idempotency ברמת DB קיימת חלקית בלבד.

## קבצים אחראיים ל-state/onboarding

- `conversation.py`: מקור האמת ל-`active_flow`, כולל flow, step, payload, version, flow_id.
- `noam_coach/bot/onboarding.py`: שאלות, pending question, אישור נתונים, completion readiness, wizard.
- `questions.py`: הגדרת שאלות, `normalize_answer`, שמירת תשובות.
- `user_model.py`: שמירת facts, קריאת profile/readiness, source/confidence/confirmed.
- `data_quality.py`: בדיקות readiness ופערים.
- `noam_coach/services/core.py`: helpers סביב flow/state.

פערים מאומתים:

- `active_flow` קיים והוא הכיוון הנכון.
- `questions.normalize_answer` עדיין נומרי כללי מדי; אין בו הגנה מספקת לערכים כמו `00:20-06:50` בשדה שאינו שינה.
- צריך להבטיח ש-174 נשמר כגובה רק בשלב height, 83 כ-goal_weight רק בשלב goal, ו-2100 כקלוריות רק בהקשר מתאים.
- צריך להפריד בין אלרגיות, רגישויות, הימנעויות והעדפות, ולא לשמור “אגוזים” כאלרגיה בלי הקשר.

## קבצים אחראיים ל-Apple Health import

- `health_import.py`: parsing של Apple Health export ויצירת rows.
- `health_service.py`: הכנסת health rows וסנכרון מדידות לפרופיל.
- `noam_coach/services/health_jobs.py`: זרימת ייבוא, אישור wizard, follow-up לאחר ייבוא.
- `noam_coach/services/health_quality.py`: דוח איכות/freshness/wear/sleep/workout confidence.
- `routine.py`: חישובי שגרה, אימונים, שינה, צעדים, frequency.
- `noam_coach/api/health_routes.py`: API לקבלת health samples.

פערים מאומתים:

- `health_quality.py` כבר כולל freshness, אבל `SLEEP_MIN_NIGHTS_FOR_CONFIRMATION` מוגדר 5 בעוד הדרישה החדשה היא 7.
- יש freshness section אבל צריך לחבר אותו למסך import כך שקובץ ישן יוצג כ-warning ולא error.
- צריך לוודא שמשקל עם פחות מ-3 מדידות לא מוצג כמגמה.
- צריך לוודא שאימוני Health מחושבים לפי חלונות 28/60/90 ימים ולא דורסים אישור ידני של המשתמש.

## קבצים אחראיים ל-nutrition

- `noam_coach/services/nutrition_context.py`: הקשר תזונתי ופרובננס ליעדים.
- `noam_coach/services/next_meal.py`: בניית “מה לאכול עכשיו”, תקציב ארוחה, מגבלות, feedback.
- `noam_coach/services/meal_validation.py`: ולידציה לארוחות.
- `noam_coach/services/dietary_restrictions.py`: מגבלות, אלרגיות, רגישויות והימנעויות.
- `meal_intelligence.py`: ניתוח ארוחות.
- `recommendations.py`: המלצות כלליות ותפריטים.
- `noam_coach/bot/meal_text.py`, `noam_coach/bot/meals.py`, `noam_coach/bot/callback_meals.py`: הצגה ופעולות Telegram.
- `noam_coach/services/health_jobs.py`: כולל `build_next_meal_text` ו-DailyContext ישן יותר.

פערים מאומתים:

- `next_meal.py` כבר בונה context, מגבלות, budget ו-2 אופציות, אבל צריך להקשיח הצגה לפי “אין ארוחות רשומות” ולא לכתוב “נשארו” בצורה מטעה.
- צריך לוודא שהפלט תמיד 2-3 אופציות, עם קלוריות/חלבון/סוג/סיבה, ושאינו מציע ארוחה כבדה סמוך לשינה.
- צריך לוודא שמגבלות מזון נלקחות מ-restrictions ולא מערבבות העדפות עם אלרגיות.

## קבצים אחראיים ל-training

- `planning.py`: יצירת candidates, workout payload, repair, activation.
- `exercise_plans.py`: תרגילים ותבניות.
- `training_intelligence.py`: התאמות, fatigue, progression עקרוני.
- `noam_coach/services/training.py`: load decision, pain-aware progression.
- `noam_coach/bot/workout_runtime.py`: אימון בזמן אמת, שמירת sets/sessions.
- `noam_coach/bot/callback_session.py`: פעולות אימון בזמן אמת.
- `noam_coach/bot/onboarding.py`: wizard של בחירת תוכנית אימון וסוג מבנה.

פערים מאומתים:

- `sets.client_event_id` כבר קיים בסכמה, אבל צריך לוודא שימוש מלא בכל שמירת סט.
- `training.py` כבר מכיל חסימת progression לפי כאב, אבל צריך בדיקות שמכסות pain >= 4.
- `planning.py` כבר מייצר candidates מלאים, אך צריך להקשיח בקשת ABC כך שלא תנותב לעריכת Leg Press.
- צריך להבטיח שאין מצב שבו תרגיל בודד מוצג כתוכנית מלאה.

## איפה נשמרים נתונים

- `users`: משתמשי Telegram.
- `user_facts`: פרופיל, source, confidence, confirmed, valid, affects.
- `user_fact_history`: היסטוריית שינויים לפרופיל.
- `active_flow`: flow יחיד פעיל, step, payload, version.
- `product_events` / `analytics_events` / `audit`: אירועים, flow_id, action trail.
- `health`: דגימות Apple Health.
- `routine_profile`: פרופיל שגרה מחושב.
- `goals` / `goal_versions`: יעדים פעילים/זמניים/מאושרים.
- `meals` / `meal_items` / `meal_fingerprints`: ארוחות, פריטים, מניעת כפילויות.
- `plan_versions` / `active_plans`: תוכניות planned.
- `sessions` / `sets`: ביצוע בפועל של אימונים.
- `daily_flags`: מצב יומי, flags, העדפות זמניות.
- `medical_constraints`: כאב/מגבלות.

## מודלים/טבלאות חסרים או דורשים הרחבה

- אין טבלת `idempotency_keys` כללית לכל action רגיש; יש פתרונות נקודתיים (`client_event_id`, unique approval/meal fingerprint).
- אין שכבת `profile_audit_view` מפורשת; אפשר להתחיל כ-service שמרכיב מ-`user_facts`, Health ו-goals.
- אין מודל `TrainingProfile` מפורש כטבלה; פרטי אימון מפוזרים ב-`user_facts`, constraints ו-plan payload.
- אין `ExerciseCatalog` עשיר עם movement pattern, contraindications ו-substitutions ברמת data model מלאה; חלק קיים בתבניות/תרגילים.
- אין שדה pain_score ב-`sets`; יש pain_location ב-`sessions`, ולכן דיווח כאב לסטים דורש הרחבה עתידית.

## בדיקות קיימות רלוונטיות

- Telegram/runtime: `tests/test_telegram_lifecycle.py`, `tests/test_callbacks.py`.
- Conversation/state: `tests/test_conversation_v2.py`, `tests/test_onboarding.py`, `tests/test_onboard_02_regression.py`.
- Health: `tests/test_health_import_security.py`, `tests/regression/test_re13_health_quality.py`, `tests/regression/test_re12_wear_and_wizard.py`, `tests/regression/test_re11_health_wizard_trend.py`.
- Nutrition/next meal: `tests/test_nutrition_context.py`, `tests/acceptance/test_rec_next_meal_05.py`, `tests/test_next_meal_callbacks.py`, `tests/regression/test_re9_next_meal_polish.py`.
- Training: `tests/test_planning_v2.py`, `tests/test_training_intelligence.py`, `tests/regression/test_pain_aware_training.py`, `tests/regression/test_re10_11_workout_wizard.py`, `tests/regression/test_training_lifecycle_trace.py`.

## בדיקות שיש להוסיף בסבב התשתיות

- stale callback לא מפיל flow ולא שולח הודעת שגיאה טכנית.
- `safe_edit_or_reply` שולח הודעה חדשה כש-edit נכשל על הודעה ישנה.
- `NetworkError` / `getaddrinfo failed` לא שולח הודעת כשל למשתמש.
- לחיצה כפולה על פעולה רגישה לא שומרת פעמיים.
- `00:20-06:50` מתקבל רק בשלב sleep ולא בשלב calories.
- `174`, `83`, `101.8` נשמרים לפי step ולא לפי regex כללי.
- Health ZIP ישן מ-2026-06-16 מוצג כ-warning.
- פחות מ-7 לילות שינה = low confidence / hint בלבד.
- פחות מ-3 מדידות משקל = ללא מגמה.

## סטטוס יישום לפני המשך

בוצע חלקית לפני כתיבת הדוח:

- נוספו זיהוי stale callback/stale edit ב-`noam_coach/services/telegram_errors.py`.
- `classify_telegram_error` הותאם כך ש-stale callback לא יודיע למשתמש.
- נוספה התחלה של `safe_answer_callback` ב-`noam_coach/bot/ui.py`.
- `safe_edit` התחיל fallback ל-reply כאשר edit נכשל בגלל הודעה ישנה.

המשך מיידי:

1. לחבר `safe_answer_callback` ל-`callback_router.py`.
2. להוסיף tests ל-Telegram-safe.
3. להעביר קריאות `query.answer()` ישירות במודולים מרכזיים ל-wrapper.
4. להעלות את סף שינה ל-7 ולהוסיף בדיקות confidence.
5. להוסיף guard ל-`normalize_answer`/onboarding כדי למנוע פירוש שינה כקלוריות או מספר בהקשר שגוי.

## עדכון יישום בסבב הנוכחי

בוצע:

- `safe_answer_callback` חובר ל-`callback_router.py`.
- כל קריאות `query.answer()` הישירות שנמצאו תחת `noam_coach/bot` הועברו ל-`safe_answer_callback`, מלבד הקריאה הפנימית בתוך ה-wrapper עצמו.
- `safe_edit` חוזק כך שכאשר edit נכשל בגלל הודעה ישנה/לא ניתנת לעריכה הוא מנסה לשלוח reply חדש.
- `telegram_errors.classify_telegram_error` מזהה stale callback ולא שולח הודעת כשל למשתמש.
- נוסף guard ב-`questions.py` וב-`noam_coach/bot/onboarding.py` שמזהה טווח שעות כמו `00:20-06:50` לפני regex נומרי, כדי שלא יישמר כ-0/קלוריות/מספר לא נכון.
- `health_quality.py` עודכן:
  - שינה ניתנת לאישור רק מ-7 לילות ומעלה.
  - Health export מעל 7 ימים מסומן stale/warning.
- נוספה שכבת `user_model.build_profile_audit` שמחזירה לכל שדה: value, source, confidence, approved, freshness, last_updated, kind, action_required.
- `next_meal.NutritionTotals` הורחב עם `meals_logged_count`.
- מסך “מה לאכול עכשיו” כבר לא פותח ב-“נשארו” כאשר לא נרשמו ארוחות היום; הוא מציין “עוד לא נרשמו ארוחות היום”.

בדיקות שנוספו/עודכנו:

- `tests/test_telegram_lifecycle.py`
  - stale callback ACK נבלע.
  - BadRequest שאינו stale עדיין נזרק.
  - safe edit נופל ל-reply.
  - stale callback מסווג transient בלי הודעה למשתמש.
- `tests/test_questions.py`
  - 174/83/101.8 נשמרים כמספרים לפי question context.
  - `00:20-06:50` לא מתקבל כתשובה נומרית.
- `tests/regression/test_re13_health_quality.py`
  - 5 לילות שינה עדיין לא confirmable.
  - export בן 8 ימים כבר warning/stale.
- `tests/test_user_model.py`
  - profile audit מסמן source/approved/freshness/action_required.
- `tests/acceptance/test_rec_next_meal_05.py`
  - ללא ארוחות רשומות אין headline מטעה של “נשארו”.

פקודות בדיקה ותוצאות:

- `pytest tests/test_telegram_lifecycle.py -q` -> עבר.
- `pytest tests/test_questions.py -q` -> עבר.
- `pytest tests/regression/test_re13_health_quality.py -q` -> עבר.
- `pytest tests/test_user_model.py -q` -> עבר.
- `pytest tests/acceptance/test_rec_next_meal_05.py -q` -> עבר.
- `pytest tests/test_telegram_lifecycle.py tests/test_questions.py tests/test_user_model.py tests/regression/test_re13_health_quality.py -q` -> 62 עברו.

מה נשאר פתוח:

- Onboarding confirmation מלא שמציג את `build_profile_audit` למשתמש עם אשר/תקן/דלג.
- Idempotency כללית לכל action רגיש באמצעות action/client event registry, מעבר ל-`sets.client_event_id` ולפתרונות נקודתיים קיימים.
- Health import success text צריך להציג במפורש warning כאשר ZIP ישן, עם כפתורי המשך/ייצוא חדש/עדכון ידני.
- משקל: בדיקת פחות מ-3 מדידות = ללא מגמה חזקה.
- אימונים: הפרדה מלאה בין בקשת ABC לבין עריכת תרגיל, כולל gate מקצועי ותיעוד tradeoff אם המשתמש מתעקש.
- TrainingProfile ו-ExerciseCatalog עשירים עדיין לא הוספו כמודלים מלאים.
- Nutrition: צריך להשלים מסך “מצב היום” מלא עם יעד/נאכל/נשאר/מקור/אמינות ומספר ארוחות.
- “מה לאכול עכשיו” עדיין צריך הצגת סוג ארוחה קלה/בינונית/כבדה לכל אופציה, וחיזוק חוקים סמוך לשינה.
## עדכון נוסף בסבב הנוכחי

בוצע:

- מסך השלמת נתוני בסיס באונבורדינג מציג עכשיו audit לפי `user_model.build_profile_audit`: מקור הנתון, האם מאושר, אמינות, freshness ופעולה נדרשת.
- Health import success מציג warning כאשר הקובץ יובא בהצלחה אבל הרשומה האחרונה ישנה מ-7 ימים, למשל 2026-06-16 ביחס ל-2026-07-06. זה warning ולא error.
- `next_meal` קיבל מצב `near_bedtime`: סמוך לשינה הוא לא דוחף ארוחה כבדה גם אם נשארה יתרה גדולה.
- מסך "מה לאכול עכשיו" מציג מקור יעד/מספר ארוחות שנרשמו, ומוסיף "למה עכשיו" לכל אופציה, לא רק לאופציה המומלצת.
- `planning.workout_quality_issues` הוחמר: מספר הסשנים חייב להתאים ל-frequency, לכל session חייב להיות code/name, ותוכנית עם תרגיל יחיד מסומנת `workout_plan_too_small`.
- `repair_workout_payload` משלים code/name בטוח ל-payload ישן, אבל לא הופך תוכנית של תרגיל אחד לתוכנית תקינה.

בדיקות שעברו בסבב הזה:

- `pytest tests/test_onboard_02_regression.py -q`
- `pytest tests/regression/test_re11_tap_first_sweep.py tests/test_onboarding.py -q`
- `pytest tests/acceptance/test_rec_next_meal_05.py -q`
- `pytest tests/test_planning_v2.py -q`
- `pytest tests/test_telegram_lifecycle.py tests/test_questions.py tests/test_user_model.py tests/regression/test_re13_health_quality.py tests/test_onboard_02_regression.py tests/regression/test_re11_tap_first_sweep.py tests/test_onboarding.py tests/acceptance/test_rec_next_meal_05.py tests/test_planning_v2.py -q`

עדיין פתוח להמשך:

- Idempotency כללי לכל פעולה רגישה מעבר לפתרונות המקומיים.
- TrainingProfile ו-ExerciseCatalog כמודלים מלאים ועשירים.
- הפרדה מלאה ברמת NLP בין בקשת split כמו ABC לבין עריכת פרמטר של תרגיל, מעבר ל-guardrails של איכות התוכנית.
- הרחבת מסך "מצב היום" עם confidence מפורש לכל נתון, מעבר לתיקונים שנעשו ב-next meal.
