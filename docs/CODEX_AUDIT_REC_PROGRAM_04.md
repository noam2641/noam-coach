# Codex Audit: REC-PROGRAM-04

Date: 2026-06-27

Scope: independent repository audit, completion of the current `REC-PROGRAM-04`
batch, adversarial review, verification, and release inspection. `REC-NEXT-MEAL-05`
was not started.

## Repository State Found

- `AGENTS.md`: not present.
- `CLAUDE.md`: not present.
- Git history: one local baseline commit, `d7be7e9 Initial baseline before REC-PROGRAM-04 (cleaned: removed .idea/, HealthKit.zip)`.
- Working tree before final checkpoint: partial REC-PROGRAM-04 changes were present and uncommitted.
- Forbidden tracked files: none found for `.env`, DB files, SQLite WAL/SHM, HealthKit exports, caches, `.venv`, IDE metadata, or private storage.
- `docs/TEST_MATRIX.md`: missing at audit start; created in this pass.

## Claim Classification

| Claim | Classification | Evidence |
|---|---|---|
| Full test suite passes | PROVEN | `557 passed, 3 warnings in 30.61s`, exit code 0. |
| Startup works | PROVEN | Bounded startup reached Telegram application start, scheduler start, bot identity, FastAPI startup, Uvicorn running; no traceback. |
| Dietary firewall integration | PARTIALLY_PROVEN | Production meal rendering and nutrition candidate generation use canonical restrictions. Older AI recommendation paths still need the future `REC-NEXT-MEAL-05` central service. |
| Availability resolver integration | PROVEN | Used by workout candidate generation, Telegram profile/program views, and Mini App API/display. |
| Body-fat normalization | PROVEN | Apple Health sync and profile display use centralized source-aware normalizer; tests cover fraction, percent, Apple Health, NaN/Inf, ambiguous values. |
| Delta meal correction | PROVEN | Text correction route applies deterministic remove/replace patches before AI fallback; tests prove unrelated items and oil are preserved. |
| Telegram integration | PARTIALLY_PROVEN | Profile/program/meal production handlers call the new services. Full live Telegram conversation replay remains externally limited. |
| Mini App integration | PROVEN | Mini profile/dashboard return resolved availability; client renders it; CSP inline handlers removed. |
| Database migration compatibility | PROVEN | `preflight.py --skip-runtime-secrets` passed; tests initialize fresh SQLite databases. |
| Release safety | PROVEN | Release ZIP built and independently inspected: 169 files, forbidden-entry count 0. |
| Secret exclusion | PROVEN | Git tracked-file scan and ZIP inspection found no forbidden secrets/private data. |
| Acceptance replay status | PARTIALLY_PROVEN | `tests/acceptance/test_recording_04_program_meal_flow.py` passes and exercises production services; it is not a full Telegram-network replay. |

## REC-PROGRAM-04 Classification

| ID | Classification | Notes |
|---|---|---|
| 04-01 Canonical training availability | PROVEN | `resolve_availability` is called by `planning.build_workout_candidates`, Telegram profile/program views, and Mini App API/display. Explicit user values override historical estimates. |
| 04-02 Detailed workout proposal preview | PARTIALLY_PROVEN | Proposal payloads contain sessions/exercises/sets/reps/rest/load guidance; Telegram candidate rendering shows session preview. Full expanded exercise preview UX is still basic. |
| 04-03 Plan activation remediation | PARTIALLY_PROVEN | Existing blockers preserve typed missing data and plan callbacks are versioned; no new broad remediation flow was added in this pass. |
| 04-04 Explainable proposal compatibility | PROVEN | Telegram candidate formatting no longer shows arbitrary precise percentages; rationale/tradeoffs/assumptions are shown. |
| 04-05 Canonical dietary restrictions | PROVEN | Typed `DietaryRestriction` model distinguishes allergy, sensitivity, intolerance, avoidance, preference, unavailable, and unknown. |
| 04-06 Restriction firewall | PARTIALLY_PROVEN | Meal display and nutrition plans are validated; older recommendation surfaces are documented as pending centralization under REC-NEXT-MEAL-05. |
| 04-07 Natural-language updates | PARTIALLY_PROVEN | Existing REC-PLAN-MEAL-03 parsing routes many updates; full repeated NL restriction update idempotency across all contexts is not fully replayed. |
| 04-08 Profile/body-fat presentation | PROVEN | Raw dicts are suppressed; body-fat normalization is source-aware; Hebrew output tests pass. |
| 04-09 Completeness service | PARTIALLY_PROVEN | Existing readiness service is shared by plan/profile flows; the richer status taxonomy is not fully centralized beyond current readiness fields. |
| 04-10 Concrete nutrition plans | PROVEN | Generated nutrition candidates include meal slots, timing, calories, protein, options, restrictions, rationale, and tradeoffs; canonical protein filtering is proven. |
| 04-11 Transparent targets | PROVEN | Target explanations include inputs/provisional status; evaluations cover goal provisional cases. |
| 04-12 Delta meal corrections | PROVEN | Deterministic remove/replace patches modify only targeted items before AI fallback. |
| 04-13 Meal totals | PROVEN | Totals derive from item values; tests cover NaN/Inf/negative rejection and displayed/saved source behavior. |
| 04-14 Oil/revision safety | PROVEN | Oil item preservation and approval idempotency are covered by regression/acceptance tests. |
| 04-15 Context-aware routing | PARTIALLY_PROVEN | Active meal correction routing is proven; full ordered routing taxonomy remains a documented architecture constraint. |
| 04-16 Daily status/saved meals | PROVEN | Daily totals use saved current-day meals; pending approvals are excluded; stale health data is not treated as today in existing status logic. |

## Defects Fixed In This Pass

- `planning.py`: canonical restriction IDs were computed but not passed into meal-slot/protein-option generation, leaving nutrition filtering incomplete.
- `noam_coach/services/dietary_restrictions.py`: added standalone `nut` alias and tight negation handling for phrases such as `contains no nuts`, `nut-free`, `ללא אגוזים`, and `בלי חלב`.
- `miniapp/templates/index.html` and `miniapp/static/app.js`: removed inline event handlers that conflicted with CSP; escaped server text before HTML rendering; repaired corrupted Hebrew; aligned Mini App weekday IDs with backend `0=Sunday`.
- `noam_coach/api/security.py`: `/mini/upload` now uses the Health upload limit instead of the small generic API body limit.
- `mini_api.py`: Mini App profile/dashboard now return resolved canonical availability.

## Verification Results

| Command | Exit code | Result |
|---|---:|---|
| `C:\Users\user\anaconda3\python.exe -m compileall -q coach_bot.py noam_coach` | 0 | Passed |
| `C:\Users\user\anaconda3\python.exe -m ruff check .` | 0 | Passed, `All checks passed!` |
| `C:\Users\user\anaconda3\python.exe -m pytest --maxfail=0 -ra` | 0 | `557 passed, 3 warnings in 30.61s`; skipped 0; xfailed 0 |
| `C:\Users\user\anaconda3\python.exe scripts\run_evaluations.py` | 0 | 33/33 passed |
| `C:\Users\user\anaconda3\python.exe scripts\preflight.py --skip-runtime-secrets` | 0 | Passed |
| Startup smoke | 0 for harness | Process reached Telegram app, scheduler, FastAPI/Uvicorn startup; no traceback; stopped after smoke window |
| `C:\Users\user\anaconda3\python.exe scripts\build_release.py` | 0 | Built release ZIP with 169 files |

## Release Evidence

- ZIP: `dist\noam_coach_2.0.0-rc5.zip`
- SHA-256: `48ffab080da0b07fda606c9a108cfd5777481b177670057dc9e91ba4fd4d9fe6`
- ZIP file count: 169
- Forbidden entries: 0
- Extracted release compileall: passed
- Extracted release import smoke: passed (`import-ok 2.0.0-rc5`)
- Extracted release preflight: passed with expected warning that DB does not exist yet

## Unresolved External Limitations

- Full production Telegram/OpenAI behavior still depends on real external services and credentials.
- Extracted release full Telegram startup was not run because `.env` is intentionally excluded from the release artifact.
- `REC-NEXT-MEAL-05` remains intentionally unimplemented.
