# PROJECT_STATE.md — Session Snapshot (2026-06-28)

## 2026-06-28 Post-Claude Audit Snapshot

- Starting commit: `678a1604c3a7dea293bb689a6d2e93fffa9f26b5`.
- Baseline was verified in `C:\coach_bot\noam_coach_audit_678a160`.
- Baseline gates: compile pass, Ruff pass, evaluations `33/33`, preflight pass, pytest `1 failed, 626 passed, 3 warnings` due to a date-dependent nutrition context test.
- Fixed next-meal nutrition realism: options now include typed ingredient details and derive displayed calories/protein from ingredient sums.
- Fixed `nextmeal:editqty`: quantity changes are a real production callback flow and save through the active recommendation snapshot.
- Fixed availability parsing so each day segment can carry its own duration.
- Fixed deterministic nutrition context regression with an injected clock.
- Current fixed-tree gates: compile pass, Ruff pass, pytest `630 passed, 3 warnings`, evaluations `33/33`, preflight pass.

This file captures the complete state of the project as of the current session,
including all changes made, decisions taken, and the exact continuation point.

---

## 1. Project Purpose and Current Architecture

### Purpose
A personal coaching Telegram bot for a single user (Noam). The bot provides:
- Workout planning and tracking (exercise selection, sets, reps, RIR, load recommendation)
- Nutrition planning and meal photo analysis (via OpenAI vision)
- Health data import from Apple Health exports (local ZIP files)
- Daily targets (calories, protein, steps) derived from the user model
- Proactive messaging (morning menu, next meal, evening summary)
- A Mini App (web-based profile editor)

### Architecture
```
coach_bot.py              ← Composition root / backward-compat facade
noam_coach/
  bot/                    ← Telegram handlers (callbacks, meals, onboarding, workout, assistant)
  services/               ← Domain services (profile, training, goals, health, dietary_restrictions, availability, body_fat)
  api/                    ← FastAPI routes (health, watch, system, mini_auth)
  app/                    ← Runtime bootstrap (schedule_jobs, build_telegram_app, run)
  jobs/                   ← Proactive messaging engine
  runtime_bind.py         ← @runtime_bound decorator for lazy name resolution

Root-level pure-domain modules (no Telegram imports):
  user_model.py           ← Fact store, readiness, display labels
  models.py               ← Pydantic models (FoodItem, MealAnalysis, etc.)
  planning.py             ← Constraint-driven plan generation, versioning, activation
  targets.py              ← Mifflin-St Jeor calorie/protein/steps targets
  training_intelligence.py← Exercise adaptation, fatigue, load recommendation
  meal_intelligence.py    ← Deterministic meal correction, fingerprinting
  recommendations.py      ← AI recommendation models (morning menu, next meal, evening summary)
  conversation.py         ← Single active flow per user, stale-callback detection
  assistant.py            ← Intent classification, free-text routing
  onboarding.py           ← Stage machine for onboarding
  health_service.py       ← Health data upsert, routine sync
  health_import.py        ← Apple Health XML parser
  config.py               ← Settings, OpenAI client, logger
  db.py                   ← SQLite with WAL, 8 migrations, all table definitions
```

### Database
SQLite with WAL mode. Key tables: `users`, `user_facts` (living profile as key/value with provenance), `user_fact_history`, `goals`, `goal_versions`, `active_plans`, `plan_versions`, `approvals`, `meals`, `meal_items`, `sessions`, `sets`, `health`, `active_flow`, `medical_constraints`, `exercise_overrides`.

### Key Patterns
- **@runtime_bound**: Decorator that injects module-level names into bot handler functions at call time, enabling testability.
- **Fact store**: `user_facts` table with kind/source/confidence/confirmed/affects columns. Changes append to `user_fact_history`.
- **Flow system**: Single `active_flow` per user with version/flow_id for stale-callback detection.
- **Plan versioning**: Candidates → active → superseded lifecycle in `plan_versions`.
- **Meal approval**: Photo → analysis → approval (pending) → user confirms → persist_meal (atomic).

---

## 2. All Changes Made in This Session

### Task Context
Implementing **REC-PROGRAM-04** — a 16-issue recording batch based on `Recording_20260627_1145.mht`, covering the Telegram flow from program center through workout planning, nutrition restrictions, nutrition planning, meal-photo analysis, meal correction, saving, and daily status.

### Changes Made (in order)

#### Phase 0 — Baseline
1. Initialized Git repository for local tracking (non-push, local only).
2. Verified baseline: 477 tests passing, compileall clean, ruff clean, evaluations 33/33.
3. **Git cleanup**: Removed `.idea/` (3 files) and `HealthKit.zip` (61MB Apple Health export with user data) from Git tracking. Updated `.gitignore`. Amended initial commit. Local files preserved.

#### Phase 1 — New Domain Modules (3 files created)

**noam_coach/services/dietary_restrictions.py** (737 lines)
- REC-PROGRAM-04-05: Unified dietary-restriction model
- REC-PROGRAM-04-06: Allergy-safe recommendation firewall
- `RESTRICTION_ALIASES`: 100+ Hebrew/English surface forms → 7 canonical IDs (tree_nuts, peanuts, dairy, gluten, eggs, soy, fish)
- `RESTRICTION_GROUPS`: "nuts" → {tree_nuts, peanuts}
- `DietaryRestriction` dataclass with canonical_id, type, severity, confirmation
- `normalize_restriction()`: Alias-aware normalization
- `parse_restrictions()`: Comma/semicolon/conjunction splitting
- `merge_restrictions()`: Idempotent merge without duplicates
- `validate_meal_restrictions()`: CRITICAL firewall — checks item names against restrictions using alias and group expansion. Catches "יוגורט יווני עם אגוזים ודבש" when nuts are restricted.
- `load_restrictions_from_facts()` / `restrictions_to_fact_value()`: Bridge old fact format ↔ new model

**noam_coach/services/availability.py** (392 lines)
- REC-PROGRAM-04-01: Canonical training availability resolver
- `TrainingAvailability` dataclass with max_days, preferred_days, preferred_time, session_minutes, source, confidence, confirmed
- `SOURCE_PRIORITY`: user_corrected > user_confirmed > active_plan > inferred_history > default
- `resolve_availability()`: Async resolver that queries all relevant facts with priority ordering. Explicit user values always override Apple Health inferred patterns.
- `format_availability_summary()`: Hebrew formatted summary
- `availability_confirmation_text()`: Prompt asking user to confirm known values
- `WEEKDAY_NAMES`: Hebrew day names

**noam_coach/services/body_fat.py** (209 lines)
- REC-PROGRAM-04-08: Centralized body-fat normalization
- `BodyFatResult` dataclass with normalized_pct, source_unit, confidence, status, warning
- `normalize_body_fat()`: Accepts raw value + source_unit + source_type + metadata. Returns structured result.
- Supports: fraction (0.20→20%), percent (20→20%), apple_health_pct, apple_health_percent, unknown unit with inference
- Statuses: valid, inferred_unit, ambiguous, implausible, missing
- Plausibility bounds: 3%–65%
- `display_body_fat()`: Returns Hebrew display string or "דורש אימות" for ambiguous/implausible

#### Phase 2 — Modified Production Files (4 files)

**user_model.py** (+20 lines changed)
- `display_value()`: Added raw dict interception (gap dicts with `{"missing": True, ...}` now return "לא צוין" instead of raw Python dict string)
- `display_value()` for `body_fat_pct`: Delegates to centralized `normalize_body_fat()` + `display_body_fat()` instead of inline heuristic

**meal_intelligence.py** (+211 lines)
- Added `_REPLACEMENT_PATTERNS`: 5 compiled regex patterns for Hebrew item replacement ("X לא Y", "X ולא Y", "X במקום Y", "זה X לא Y", "זה X ולא Y")
- Added `_parse_replacement_corrections()`: Deterministic parser for replacement instructions
- Updated `parse_meal_correction()`: Added replacement as priority 2 (after removal, before preparation)
- Added `apply_item_replacement_correction()`: Renames target item, adjusts macros via `_KCAL_PER_100G` lookup, preserves grams, does NOT touch unrelated items
- Added `_KCAL_PER_100G`: Conservative calorie density table for common foods

**noam_coach/bot/meals.py** (+44/-22 lines)
- Added import of `load_restrictions_from_facts`, `validate_meal_restrictions` from dietary_restrictions module
- `render_meal()`: Replaced naive substring restriction matching with firewall-based validation
  - "block" violations → ⛔ icon with "אלרגיה/רגישות:" label
  - "warn" violations → ⚠️ icon with "רשום אצלך כהימנעות:" label
  - "substitute" violations → 🔄 icon with "מרכיב לא זמין:" label

**noam_coach/bot/meal_text.py** (+9 lines)
- `_handle_meal_correction_text()`: Added `replace_corrections` handling between preparation and AI fallback. Calls `meal_intelligence.apply_item_replacement_correction()`.

#### Phase 3 — Test Files (2 files created)

**tests/acceptance/test_recording_04_program_meal_flow.py** (585 lines)
- 20 async tests in `TestRecording04ReplayFlow`
- Uses file-backed SQLite via `tmp_path` fixture
- Covers: availability override, proposal generation, proposal details, restriction firewall, alias matching, idempotent merge, body-fat normalization, profile presentation, meal replacement/removal, meal totals, target explanation, save idempotency, daily totals

**tests/test_recording_04_regression.py** (512 lines)
- 36 tests across 6 classes:
  - `TestDietaryRestrictionModel` (8 tests): normalize, parse, merge, firewall block/warn/group
  - `TestAvailabilityResolver` (3 tests): default, explicit override, format summary
  - `TestMealCorrection` (4 tests): replace pattern, remove pattern, target-only change, oil preserved
  - `TestBodyFatNormalization` (14 tests): fraction→percent, percent passthrough, implausible, zero, 1.0 ambiguous, negative, NaN, Inf, explicit units, Apple Health source, Hebrew not reversed
  - `TestMealTotalConsistency` (3 tests): totals match items, no NaN, negative rejected
  - `TestProfilePresentation` (4 tests): gap not displayed raw, enum displayed Hebrew, missing value Hebrew, availability Hebrew, restriction Hebrew

---

## 3. Files Changed — Summary

| File | Status | What Changed |
|------|--------|-------------|
| `.gitignore` | Modified | Added `.idea/`, `HealthKit.zip`, `*.health_export`, `export*.zip`, `pytest_full.log` |
| `user_model.py` | Modified | `display_value()`: raw dict guard + body-fat normalization delegation |
| `meal_intelligence.py` | Modified | Added replacement patterns, `_parse_replacement_corrections()`, `apply_item_replacement_correction()`, `_KCAL_PER_100G` |
| `noam_coach/bot/meals.py` | Modified | `render_meal()`: restriction check uses firewall module |
| `noam_coach/bot/meal_text.py` | Modified | `_handle_meal_correction_text()`: handles replace corrections |
| `noam_coach/services/dietary_restrictions.py` | **New** | Unified dietary restriction model + firewall |
| `noam_coach/services/availability.py` | **New** | Canonical training availability resolver |
| `noam_coach/services/body_fat.py` | **New** | Centralized body-fat normalization |
| `tests/acceptance/__init__.py` | **New** | Empty package init |
| `tests/acceptance/test_recording_04_program_meal_flow.py` | **New** | 20 acceptance tests |
| `tests/test_recording_04_regression.py` | **New** | 36 regression tests |

---

## 4. Professional and Architectural Decisions

1. **Dietary restrictions as a separate service module** (not inlined into user_model or planning): The restriction logic (aliases, groups, normalization, firewall) is complex enough to warrant its own module. It's pure Python with no async, making it easily testable.

2. **Firewall pattern for allergy safety**: `validate_meal_restrictions()` is a mandatory checkpoint that must be called before any food recommendation is displayed. It uses alias-aware matching (not substring) and group expansion ("nuts" covers both tree_nuts and peanuts).

3. **Body-fat normalizer as structured result**: Instead of inline heuristics in `display_value()`, the normalizer returns a `BodyFatResult` with status/confidence/warning fields. The presentation layer calls `display_body_fat()` which maps status to display text. This separates normalization logic from display logic.

4. **Availability resolver with explicit priority**: The resolver queries multiple fact sources independently and uses the highest-priority non-None value for each field. Apple Health inferred data (workout_pattern) is always lower priority than explicit user values.

5. **Delta-based meal correction**: Item replacement is deterministic (no AI). The `apply_item_replacement_correction()` function modifies only the targeted item and never touches unrelated items. It uses a conservative calorie density table for known food swaps.

6. **Git hygiene**: Removed HealthKit.zip (61MB user health data) and .idea/ (IDE config) from Git tracking. These were in the initial commit because `git add -A` was used before `.gitignore` was complete.

7. **Hebrew strings**: All user-facing Hebrew is stored as normal UTF-8 in source files. The Windows console may display Hebrew as garbled/reversed due to codepage issues, but the actual byte content is correct (verified by character-level assertions in tests).

---

## 5. Bugs Identified and Fixed

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| `display_value` shows raw `{"missing": True, "why_matters": ...}` dicts | `record_gap()` stores a dict as the value, and `display_value` had no dict guard | Added isinstance(value, dict) check that returns "לא צוין" for gap dicts |
| Body fat 0.20 displayed as "0.2%" instead of "20.0%" | No fraction-vs-percentage normalization | Created centralized `normalize_body_fat()` with unit-aware conversion |
| "יוגורט יווני עם אגוזים ודבש" not blocked when nuts restricted | Restriction check used naive substring matching | Replaced with `validate_meal_restrictions()` using alias/group-aware matching |
| "שניצל רגיל לא טופו" fell through to AI re-analysis | No deterministic replacement pattern parser | Added `_REPLACEMENT_PATTERNS` and `apply_item_replacement_correction()` |
| Meal correction "שניצל רגיל לא טופו" changed unrelated items | AI re-analysis reinterprets entire plate | Delta correction modifies only the targeted item |
| HealthKit.zip (user data) committed to Git | `git add -A` before .gitignore was complete | Removed from tracking, updated .gitignore, amended commit |
| .idea/ (IDE config with dataSources.xml) committed | Same as above | Removed from tracking, updated .gitignore |

---

## 6. Open Tasks (REC-PROGRAM-04 Issues Still Pending)

### Partially Implemented
| Issue | Title | Status | What Remains |
|-------|-------|--------|-------------|
| 04-01 | Canonical training availability model | Module created | Not yet wired into onboarding, profile summary, program center |
| 04-05 | Unified dietary-restriction model | Module created | Not yet wired into profile updates, plan generation, Mini App |
| 04-06 | Allergy-safe recommendation firewall | Wired into render_meal | Not yet wired into nutrition plan generation, recommendation engine, Mini App |
| 04-08 | Profile presentation and body-fat normalization | Normalizer created | Not yet wired into health_service fact sync, profile view rendering |
| 04-12 | Delta-based meal correction | Replace/remove wired into meal_text handler | Need to verify oil preservation across revisions |

### Not Yet Started
| Issue | Title | Priority |
|-------|-------|----------|
| 04-02 | Detailed workout proposals before selection | High |
| 04-03 | Proposal activation and automatic remediation | High |
| 04-04 | Explain or remove compatibility percentages | Medium |
| 04-07 | Natural-language restriction updates and idempotency | High |
| 04-09 | One authoritative profile-completeness service | Medium |
| 04-10 | Nutrition plans must be concrete and constraint-safe | High |
| 04-11 | Transparent provisional target calculation | Medium |
| 04-13 | Meal totals must come from displayed items | High |
| 04-14 | Revision-safe oil and ambiguity handling | Medium |
| 04-15 | Context-aware free-text routing | Medium |
| 04-16 | Daily status and saved-meal correctness | Medium |

### Checkpoint Tasks (from user interrupt)
| Task | Status |
|------|--------|
| Git secrets audit | ✅ Complete — .idea/ and HealthKit.zip removed |
| Hebrew string verification | ✅ Complete — all strings normal UTF-8, character-level tests added |
| Body-fat normalizer architecture | ✅ Complete — centralized module with structured result |
| Baseline test proof | ✅ 533 passed, exit code 0 |
| Test audit (mutation sensitivity) | ❌ Not yet performed |
| Integration tracing (new modules into real flows) | ❌ Partially done (meals.py wired, others pending) |
| Dietary alias false-positive review | ❌ Not yet performed |
| Git commit quality check | ✅ Complete |

---

## 7. Test Results

### Last Full Run (after checkpoint fixes)
```
Command: C:\Users\user\anaconda3\python.exe -m pytest --tb=no
Result:  533 passed, 3 warnings in 60.71s
Exit:    0
```

### Test Breakdown
- **Original baseline**: 477 tests (all passing)
- **New acceptance tests**: 20 tests (tests/acceptance/test_recording_04_program_meal_flow.py)
- **New regression tests**: 36 tests (tests/test_recording_04_regression.py)
- **Total**: 533 tests, 0 failures, 0 errors, 0 skipped

### Other Verifications
| Check | Result | Exit Code |
|-------|--------|-----------|
| compileall (coach_bot.py noam_coach) | Clean | 0 |
| ruff check . | All checks passed | 0 |
| run_evaluations.py | 33/33 passed | 0 |
| preflight.py | Not yet run |  |
| startup smoke test | Not yet run |  |
| build_release.py | Not yet run |  |

---

## 8. Commands to Run the Project and Tests

### Prerequisites
- Python: `C:\Users\user\anaconda3\python.exe`
- Working directory: `C:\coach_bot\noam_coach_complete_release`
- Environment: `.env` file required (not in repo, see section 9)

### Test Suite
```powershell
# Full test suite
C:\Users\user\anaconda3\python.exe -m pytest --tb=short

# Specific test files
C:\Users\user\anaconda3\python.exe -m pytest tests/acceptance/test_recording_04_program_meal_flow.py -v
C:\Users\user\anaconda3\python.exe -m pytest tests/test_recording_04_regression.py -v

# With output to file
C:\Users\user\anaconda3\python.exe -m pytest --tb=no 2>&1 > pytest_full.log
```

### Quality Checks
```powershell
# Compile check
C:\Users\user\anaconda3\python.exe -m compileall -q coach_bot.py noam_coach

# Lint
C:\Users\user\anaconda3\python.exe -m ruff check .
C:\Users\user\anaconda3\python.exe -m ruff check --fix .   # auto-fix

# Evaluations
C:\Users\user\anaconda3\python.exe scripts/run_evaluations.py

# Preflight
C:\Users\user\anaconda3\python.exe scripts/preflight.py --skip-runtime-secrets

# Release build
C:\Users\user\anaconda3\python.exe scripts/build_release.py
```

### Startup (requires .env with real tokens)
```powershell
C:\Users\user\anaconda3\python.exe coach_bot.py
```

---

## 9. Required Environment Variables (Names Only)

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token |
| `TELEGRAM_ALLOWED_USER_ID` | Single authorized user ID |
| `OPENAI_API_KEY` | OpenAI API key for vision and structured output |
| `HEALTHKIT_API_TOKEN` | Token for HealthKit API endpoints (optional) |
| `MINI_APP_SECRET` | HMAC secret for Mini App authentication |
| `PUBLIC_BASE_URL` | Public HTTPS URL for Mini App and webhooks |
| `DATABASE_PATH` | SQLite database file path (default: `./noam_coach.db`) |
| `STORAGE_DIR` | Directory for meal photos (default: `./storage`) |

---

## 10. Exact Continuation Point for Next Session

### Where to Resume
The session was paused during an **integrity checkpoint** requested by the user. The checkpoint is mostly complete:

1. ✅ Git cleaned (secrets removed)
2. ✅ Hebrew verified (normal UTF-8, character-level tests)
3. ✅ Body-fat normalizer redesigned (structured result with status/confidence)
4. ✅ Test suite proven (533 passed, exit 0)
5. ❌ **Test audit with mutation sensitivity** — need to temporarily disable firewall/normalizer/correction and prove tests fail
6. ❌ **Integration tracing** — need to verify new modules are called by real production flows (not just tested in isolation)
7. ❌ **Dietary alias false-positive review** — need boundary-aware tests (coconut, nutmeg, "contains no nuts", etc.)

### After Checkpoint Completion, Resume REC-PROGRAM-04 Implementation
Remaining issues (11 of 16 not yet started):
- 04-02: Detailed workout proposals before selection
- 04-03: Proposal activation and automatic remediation
- 04-04: Explain or remove compatibility percentages
- 04-07: Natural-language restriction updates and idempotency
- 04-09: One authoritative profile-completeness service
- 04-10: Nutrition plans concrete and constraint-safe
- 04-11: Transparent provisional target calculation
- 04-13: Meal totals must come from displayed items
- 04-14: Revision-safe oil and ambiguity handling
- 04-15: Context-aware free-text routing
- 04-16: Daily status and saved-meal correctness

### After All 16 Issues Implemented
- Full acceptance replay test must pass
- Adversarial review
- Startup smoke test
- Preflight
- Release build and ZIP verification
- Update docs/RECORDING_ISSUES.md, docs/CONTINUATION_STATE.md, CHANGELOG.md, etc.

### Key Files to Read First in Next Session
1. This file (`PROJECT_STATE.md`) — full context
2. `noam_coach/services/dietary_restrictions.py` — restriction firewall
3. `noam_coach/services/availability.py` — availability resolver
4. `noam_coach/services/body_fat.py` — body-fat normalizer
5. `meal_intelligence.py` — delta correction additions (search for `_REPLACEMENT_PATTERNS`)
6. `noam_coach/bot/meals.py` — render_meal restriction wiring
7. `noam_coach/bot/meal_text.py` — replacement correction wiring
8. `tests/acceptance/test_recording_04_program_meal_flow.py` — acceptance tests
9. `tests/test_recording_04_regression.py` — regression tests
