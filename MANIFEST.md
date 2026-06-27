# Manifest — Noam Coach 2.0.0-rc5

## נקודת כניסה ותאימות

- `coach_bot.py` — composition root ו־facade השומר על ה־API ההיסטורי.
- `config.py`, `db.py`, `models.py`, `helpers.py` — configuration, persistence, schemas ו־utilities.

## החבילה המודולרית

- `noam_coach/app/runtime.py` — בניית Telegram app והרצת FastAPI/Telegram.
- `noam_coach/api/` — security, system, HealthKit, Shortcuts, Watch ו־Mini auth.
- `noam_coach/bot/` — UI, onboarding, meals, workouts, assistant ו־callback families.
- `noam_coach/services/` — core, profile, training, goals, Health jobs ופתרון נתיב מקומי מאובטח.
- `noam_coach/jobs/proactive.py` — עבודות יזומות, retry ו־delivery claims.
- `noam_coach/runtime_bind.py` — תאימות לממשק הישן בזמן המעבר.

## Domain ותשתיות קיימות

- `conversation.py`, `planning.py`, `training_intelligence.py`, `meal_intelligence.py`.
- `coach_intelligence.py`, `data_quality.py`, `event_log.py`, `evaluation.py`.
- `health_import.py`, `health_service.py`, `onboarding.py`, `questions.py`, `user_model.py`.
- `recommendations.py`, `reconcile.py`, `routine.py`, `targets.py`, `exercise_plans.py`.
- `retention.py`, `feature_flags.py`, `assistant.py`.

## Mini App

- `mini_api.py` — נתיבי API עסקיים.
- `miniapp/` — HTML, JavaScript ו־CSS נפרדים.

## בדיקות ותפעול

- `tests/` — 351 בדיקות unit/integration/architecture.
- `evaluations/core_cases.jsonl` — 33 מקרים ב־11 קטגוריות.
- `scripts/` — preflight, backup/restore, migrations, privacy tools, replay, evaluations, Tunnel helper ו־release build.
- `.github/workflows/ci.yml`, Docker/Compose/Caddy ו־Makefile.

## תיעוד

- `README.md`, `CHANGELOG.md`, `VALIDATION_REPORT.md`, `IMPLEMENTATION_STATUS.md`, `SECURITY.md`.
- `docs/ARCHITECTURE.md`, `API.md`, `OPERATIONS.md`, `RELEASE_CHECKLIST.md`.
- `docs/MINIAPP_SETUP.md`, `IOS_WATCH_IMPLEMENTATION.md`, `SHORTCUTS_SETUP.md`, `WHAT_REMAINS.md`.

## מוחרג מה־release

- `.env` ו־credentials.
- DB, WAL/SHM וגיבויים.
- `.venv`, `.git`, `.claude` ו־caches.
- storage, תמונות וקובצי Health של משתמשים.
- logs, bytecode וארכיוני עבודה.
