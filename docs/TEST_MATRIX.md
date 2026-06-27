# Test Matrix

Updated: 2026-06-27

## Mandatory Gates

| Gate | Command | Result |
|---|---|---|
| Compile | `C:\Users\user\anaconda3\python.exe -m compileall -q coach_bot.py noam_coach` | Passed |
| Lint | `C:\Users\user\anaconda3\python.exe -m ruff check .` | Passed |
| Unit/acceptance tests | `C:\Users\user\anaconda3\python.exe -m pytest --maxfail=0 -ra` | 557 passed, 0 skipped, 0 xfailed, 3 warnings |
| Evaluations | `C:\Users\user\anaconda3\python.exe scripts\run_evaluations.py` | 33/33 passed |
| Preflight | `C:\Users\user\anaconda3\python.exe scripts\preflight.py --skip-runtime-secrets` | Passed |
| Startup smoke | bounded `coach_bot.py` process | Passed startup milestones, no traceback |
| Release build | `C:\Users\user\anaconda3\python.exe scripts\build_release.py` | Passed, 169 files |

## REC-PROGRAM-04 Coverage

| Area | Tests |
|---|---|
| Availability precedence and production planning integration | `tests/test_recording_04_regression.py`, `tests/acceptance/test_recording_04_program_meal_flow.py`, `tests/test_mini_profile_api.py` |
| Dietary alias boundaries and negation | `tests/test_recording_04_regression.py` |
| Nutrition plan restriction filtering | `tests/acceptance/test_recording_04_program_meal_flow.py` |
| Body-fat source-aware normalization | `tests/test_recording_04_regression.py`, `tests/acceptance/test_recording_04_program_meal_flow.py` |
| Delta meal correction | `tests/test_recording_04_regression.py`, `tests/acceptance/test_recording_04_program_meal_flow.py` |
| Mini App CSP/XSS/static rendering | `tests/test_miniapp_render.py` |
| Mini upload body limits | `tests/test_api_guard.py` |

No known-bug `xfail` cases remain in the test tree.
