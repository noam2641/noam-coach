# Noam Coach — Work Plan (WAVE-1)

**Base:** `develop` @ `1d9b5e7` · **Evidence:** 779 `product_events` from the live
session 2026-07-27 05:00–07:15 UTC, `logs/session_20260727_075856.log`, seven
read-only audit agents, every critical finding re-verified independently.

**Status of WAVE-0 (2026-07-26 session):** shipped, 8 PRs merged. Verified in
production this session: `Intent.slots` (10/10 classifications succeeded, was
0/9), typed weight persisted, Mini App button (93/93 deliveries, zero failures).

**Status of P0 (this session):** 4 fixed and pushed, PR #33 open, rest pending.

---

---

## Progress

**Shipped: 14 PRs.** P0-1..P0-4 (#33–#36), docs (#37, #43), and eight WAVE-1
items. Every value below was verified **on `develop` after merge**, not taken
from the PR that claimed it.

| Item | PR | Verified on `develop` |
|---|---|---|
| W1-1 restriction enforcement | #38 | tortilla and aubergine now blocked; 7 unrelated foods stay clean |
| W1-2 calorie target | #39 | 2290 → **2420 kcal** |
| W1-3 window anchoring | #42 | `sessions_sampled` 1 → **10**, frequency 0.2 → **2.5** |
| W1-4 weekday disclosure | #44 | floors `{28:2, 60:3, 90:4, 180:4}`, monotonic, capped |
| W1-5 confidence scaling | #45 | n=1 fact 0.90 → **0.45**; scalar facts unchanged |
| W1-6 provenance | #46 | `source=derived` preserved, still decision-usable |
| W1-9 duplicate status | #40 | `LIVE_DUPLICATE_APPROVAL_STATUSES = ('pending',)` |
| W1-12 interaction terminal | #41 | `interaction.completed` emitted |

### Three independent defences on one defect

W1-3, W1-5 and W1-6 all touch the same failure — a training frequency derived
from a single sample being treated as established fact. They were built by
three separate agents and compose without conflict. Measured on `develop`:

```
W1-3  inference : 2.5 /wk from 10 sessions   (was 0.2 from 1)
W1-5  confidence: asked 0.85 -> stored 0.425 (n=1 attenuated)
W1-6  provenance: source=derived, NOT laundered to user_report
      still usable for decision: True
```

W1-2 sits above all three: it prefers the frequency the user *confirmed* (4.0)
over any inference. A user who never stated one now gets 2.5 instead of 0.2.

None of the four depends on another. That is the property worth preserving —
each is a separate reason the original defect cannot recur.

### Lesson: give each parallel agent its own worktree

The first parallel batch (W1-3/W1-9/W1-12) ran on disjoint **files** and the
files never collided — but all three shared one git working tree, so their
branches stacked on each other instead of branching from `develop`: W1-9's
commit landed on W1-12's branch, and W1-9's own branch pointed at `develop`
with nothing on it. One agent had uncommitted work discarded when another
switched the shared tree mid-task. Both agents reported it independently.

Untangling was possible (cherry-pick each commit onto a clean branch, verify
each in isolation) but avoidable. **Disjoint file ownership is necessary and
not sufficient — parallel writers also need `git worktree add` isolation.**

The second batch (W1-4/W1-5/W1-6) used one worktree per agent under
`C:/coach_bot/wt-*` and produced **zero collisions**. That is now the standard
for any parallel writer.

### Cross-agent composition must be instructed, not hoped for

W1-6 was launched while W1-5 was still changing `set_fact` in a different
worktree. Its brief said explicitly: *another agent is adding sample-based
confidence attenuation — do not fight it; if you pass `confidence=0.85` for a
one-sample derivation their change will attenuate it, which is correct; design
for that outcome.*

It did. The merged result is the 0.85 → 0.425 line above. Left to infer the
situation, an agent would reasonably have "defended" its value against the
attenuation and the two fixes would have cancelled out.

## Verification status and agent assignment

Every item below carries a verification verdict. **Agent output is a proposal,
never a finding** — across two waves about a quarter of agent claims did not
survive checking, and acting on them would have meant fixing correct code.

| Verdict | Meaning |
|---|---|
| **REPRODUCED** | The Work Manager re-ran it against the live DB or code and observed the failure directly. Ready to implement. |
| **CODE-CONFIRMED** | Verified by reading the code path end to end; not reproducible without a live trigger. Implement, but validate the trigger first. |
| **AGENT-ONLY** | Reported by an agent, not yet independently checked. **Must be reproduced before any code is written.** |
| **REJECTED** | Checked and did not hold. Recorded so it is not re-found. |

**Current state: 19 REPRODUCED, 4 CODE-CONFIRMED, 0 AGENT-ONLY.** Nothing in
this plan rests on an unverified agent claim.

> **B0 reconciliation, 2026-08-13.** The verdicts above record what was true when
> each item was raised; they are **not** evidence that the mechanism still holds.
> This plan was reconciled against `origin/develop` @ `2895559`: W1-17 is closed as
> obsolete, W1-12/W1-19's completion marks were corrected, and W1-13/W1-18/W1-22/W1-23
> were re-scoped. **Two path conventions matter when reading this document:** several
> citations name package paths for files that live at the **repository root** —
> `data_quality.py`, `db.py`, `event_log.py`, `recommendations.py`,
> `meal_intelligence.py`, `assistant.py`, `routine.py`, `config.py`,
> `conversation.py`, `retention.py`. A grep at the wrong path returns "no such file"
> and **reads as evidence the defect was fixed**; that mistake was made and caught
> during this reconciliation. Current per-item status with `file:line` evidence lives
> in `docs/WORK_MANAGER_STATE.md`, which is authoritative where the two disagree.

Verification evidence for the three items that started as AGENT-ONLY:

```
W1-18  signature(_meals_remaining) = (hours_until_bedtime, recent_minutes, flags)
       -> takes NO meal-count preference; last line is max(1, min(3, estimate))

W1-20  eating_windows consumed by 6 modules; grep for a first==last guard
       -> no match anywhere

W1-22  _PORTION_UNIT_GRAMS = 7 entries, _DISCRETE_UNIT_GRAMS = 5
       -> 'יחידות' present: False;  'יחידה' present: False
```

**Superseded in part on 2026-08-13** — the three readings above were accurate when
taken and are retained as the historical record, but two are no longer the current
state: `_meals_remaining` is now only a fallback argument (see W1-18), and the
`_PORTION_UNITS` canonical map at root `meal_intelligence.py:115` *does* contain
`יחידות → יחידה`; it is simply not wired into the materialization path (see W1-22).

### Claims rejected at verification (do NOT implement)

| Claim | Why it does not hold |
|---|---|
| "The approved meal bypassed plausibility" | The check runs on every card render (`meals.py:693`). Beverages disable two rules, so it passed legitimately. A measurement gap, not an enforcement gap — see W1-21. |
| "The knee constraint was never detected" | Detected correctly; `squat` and `leg_press` both declare `knee` in `joint_load`. The real defect was narrower: the plan renderer never called the warning function (fixed, P0-3). |
| "`audit` rows have no corresponding product events" | The windows are not empty. Survives only for `safety_alert` and `approve_substitution` — see W1-14. |
| "`sessions.pain_location` NULL is a lost write" | It is a transactional claim token; NULL is the success state. Pinned by a test in WAVE-0. |
| "Israel timezone is wrong" | There is no APScheduler in this repo; every `run_daily` already passes `tzinfo=TZ`. |

### Agent assignment

Lanes are grouped so each agent owns **disjoint files**, which is what made
WAVE-0's five-lane merge produce zero conflicts.

| Lane | Items | Files owned | Parallel-safe with |
|---|---|---|---|
| **A — Nutrition safety** | W1-1, W1-9, W1-18..W1-23 | `dietary_restrictions.py`, `meals.py`, `next_meal.py`, `day_plan.py`, `meal_*` | B, C, D |
| **B — Facts & learning** | W1-2..W1-6 | `routine.py`, `health_service.py`, `health_jobs.py`, `user_model.py`, `goals.py` | A, C, D |
| **C — Observability** | W1-11, W1-13..W1-16 *(W1-12 done; **W1-17 closed obsolete**)* | `noam_coach/observability/*`, `taxonomy.py`, **root `data_quality.py` / `event_log.py`** | A, B, D — **but NOT internally parallel**: `noam_coach/bot/callback_session.py` is shared by W1-13/W1-14/W1-15/W1-16, three of them in the same regions (`_emit_session_event` ~`:170-202`, substitution handler ~`:878-916`). Serialize W1-16 → W1-14 → W1-15 |
| **D — Workout** | W1-10 | `workout.py`, `callback_session.py` | A, B, C |
| **E — Intent & routing** | W1-7, W1-8 | `assistant.py`, `bot/assistant.py` | **conflicts with A** (shares `meal_text.py` reach) — serialize after A |
| **F — UX & copy** | W1-24..W1-36 | inline Hebrew literals across all handlers | **NOT parallel with anything** — no central strings module |
| **G — Data hygiene** | W1-37..W1-43 | live DB only, no code | after A and B land |

**Read-only agents** (`repository-auditor`, `architecture-reviewer`,
`test-verifier`) may run in parallel with any lane, on any batch.

---

## Rules for whoever executes this

1. Never implement on `develop`. One branch + one PR per item.
2. Never touch `C:\coach_bot\noam_coach_complete_release\` or `noam-coach-private-audit\`.
3. **A PR touching a schema migration must upgrade a copy of a real
   previously-migrated DB before merge.** Green CI is not sufficient — this was
   learned the hard way (PR #31).
4. Merge only when CI is green in **both** push and pull_request contexts.
5. Verify every fix by **reverting it and confirming the tests fail**.
6. Gates: focused tests → `pytest tests/ --ignore=tests/regression --ignore=tests/acceptance`
   → `pytest tests/regression tests/acceptance` → `ruff check .` → `compileall -q .`

---

# ✅ DONE — P0 (this session)

| ID | Fix | Commit |
|---|---|---|
| P0-1 | `menu:goals` routed; unhandled callbacks now answer + emit an event | `95a82f2` |
| P0-2 | Dev-note guard accepts `#...#`, the form actually typed | `5d0e60e` |
| P0-3 | Weekly plan marks exercises loading an injured joint | `f8a5982` |
| P0-4 | "No allergies" no longer inferred from an unanswered question | `e2b0c1a` |

---

# 🔴 WAVE-1A — Correctness defects with direct user impact

## ✅ W1-1 · Two of your three dietary restrictions are unenforceable
**[DONE — #38]** · Lane A
**Severity: CRITICAL (food safety) · Verified end-to-end**

`load_restrictions_from_facts("חציל, טורטייה ואגוזים", …)` parses **3**
restrictions correctly. But enforcement matches through
`_canonical_ids_in_text` (`dietary_restrictions.py:313-335`), which resolves
only terms present in `RESTRICTION_ALIASES` — an **85-entry table**.

A restriction whose `canonical_id` is a raw Hebrew literal **can never match its
own name**. Measured:

```
'חציל'    in RESTRICTION_ALIASES -> False
'טורטייה' in RESTRICTION_ALIASES -> False
'אגוזים'  in RESTRICTION_ALIASES -> True

validate_meal_restrictions('ראפ טורטייה עם עוף') -> []      ← CLEAN
validate_meal_restrictions('חציל בתנור')          -> []      ← CLEAN
validate_meal_restrictions('סלט עם אגוזי מלך')    -> [warn]  ← caught
```

This is the mechanism behind the tortilla recommendation. It is systematic: any
restriction the alias table does not recognise is stored, displayed, counted,
sent to the AI — and structurally invisible to the deterministic firewall.

**Second half:** `avoidance` maps to `warn` (`dietary_restrictions.py:652`), and
`menu_validation.py:255-256` only records a violation when the action is
`block`. So **no entry from `diet_restrictions` can ever hard-block a menu** —
only true allergies can.

**Required:** match unmapped restrictions on their literal text (with Hebrew
prefix/plural handling) as a fallback when no canonical id resolves. Decide with
the owner whether `avoidance` should block or warn.

**Acceptance:** the three real restrictions all produce a violation on a
matching meal name; a test drives the exact live values.

## ✅ W1-2 · Your calorie target is 130 kcal/day too low
**[DONE — #39]** · Lane B
**Severity: CRITICAL · Verified by direct computation**

Two stores hold the same quantity and disagree by 20×:

```
routine_profile.workout.weekly_frequency  = 0.2
user_facts.workout_pattern.weekly_frequency = 4.0
```

[goals.py:297-300](noam-coach/noam_coach/services/goals.py#L297-L300) feeds
`routine_profile` into `compute_targets`. Measured:

```
workouts_per_week=0.2 -> 2290 kcal      ← what you got
workouts_per_week=4.0 -> 2420 kcal      ← correct
                          130 kcal/day, ~910/week
```

The sync is one-way (`sync_routine_to_facts` writes facts *from* the profile,
never the reverse), so a correction propagates one direction and **re-diverges
every night** when `job_evening` recomputes the profile.

**Required:** one authoritative source for training frequency. Either make the
profile read back from confirmed facts, or make `goals.py` read the fact.

**Acceptance:** the two stores cannot disagree; a test asserts the target uses
the user-confirmed frequency.

## ✅ W1-3 · Learning windows anchored to today, on a 43-day-old export
**[DONE — #42]** · Lane B
**Severity: HIGH · Root cause of W1-2**

`learn_workout_pattern` (`routine.py:1048`) measures 45 days back from `now()`.
Your export ends 2026-06-14. Intersection: **1 session out of 296.** Ten
sessions exist in the file's own last 45 days.

`average_daily_steps` already solved this — it anchors to the last complete day
*in the file* (`routine.py:483`) and documents why. `learn_workout_pattern`,
`learn_sleep_schedule` and `learn_eating_windows` never got the same treatment.
`learn_sleep_schedule` ignores its window entirely (456-day span presented as
"120 nights").

**Acceptance:** all `learn_*` functions anchor to the data; a test with a stale
fixture yields the same result as a fresh one.

## ✅ W1-4 · The weekday detector widens until it succeeds
**[DONE — #44]** · Lane B
**Severity: HIGH · This is why Saturday kept coming back**

`_historical_workout_weekdays` (`health_jobs.py:442`) retries at
28 → 60 → 90 → 180 → full history until it can return the requested count.
Reproduced against your data: 28d → 2 days; **60d → 5 days → returns
`[0,2,5,6]`**, exactly what is stored.

At 90d+ the "≥2 sessions" floor admits all seven weekdays, so success is
unconditional. Only `full_history` is disclosed; `last_60_days` renders
identically to `last_28_days`.

**Acceptance:** the caller can distinguish "found" from "widened"; the user is
told when the window was widened.

## ✅ W1-5 · Confidence is keyed on a string, never on sample size
**[DONE — #45]** · Lane B
**Severity: HIGH**

`user_model.py:778-779` assigns confidence from `SOURCE_CONFIDENCE[source]`.
`sessions_sampled=1` and `nights_sampled=120` get **identical** confidence.
`confirm_fact` then hardcodes `MAX(confidence, 0.9)` (`user_model.py:938`) — a
0.55→0.90 jump on one tap with no new evidence.

Facts in your DB that contradict their own metadata:
- `workout_pattern`: `weekly_frequency 4.0` with `sessions_sampled 1`, `valid_weeks_sampled 0`
- `common_weekdays [6,0,2,5]` with `weekday_hour_samples {"6": 1}`
- `eating_windows`: first == last == "08:00", `avg_daily_calories 140.0`, confidence 0.90

**Acceptance:** confidence scales with sample size; a fact derived from n=1
cannot reach 0.90.

## ✅ W1-6 · One tap converts a machine guess into "the user told me"
**[DONE — #46]** · Lane B
**Severity: HIGH**

Five sites (`health_jobs.py:1191, 1207, 1216, 1224, 1254`) rewrite a derived
value as `source=user_report, confidence=0.85, confirmed=1`.

Verified in your data: `session_minutes=38` and `workout_window="19:00"` — both
from **one** workout — are now indistinguishable from you having typed them. No
history row is written for a newly-created key, so provenance is gone. This
matters because `_confirmed_fact_value` (`health_jobs.py:623-639`) trusts
`source == SOURCE_USER` as "the user really said this".

**Acceptance:** a confirmed derivation keeps `source=derived` with a
`confirmed_by_user` marker; provenance is recoverable.

## W1-7 · `build_plan` mutates state with no confirmation
**[DONE — verified live on `develop`]** · Lane E

> Status corrected 2026-07-29: this entry read `[REPRODUCED]` while the fix was
> already merged. `_plan_rebuild_confirmation` is present in
> `noam_coach/bot/assistant.py` and pinned by
> `tests/test_plan_rebuild_confirmation.py`.
**Severity: HIGH**

"6 ארוחות ביום" (6 *meals* a day) was classified `build_plan(frequency=6)` at
0.95 confidence and immediately wrote `training_days_per_week=6` and a plan
titled "התוכנית השבועית שלך — 6 אימונים". You corrected it 47 seconds later.

Two faults: the `Action` taxonomy (`assistant.py:30-51`) has **no label for
meals-per-day**, so the model had no correct answer; and
`noam_coach/bot/assistant.py:433-460` mutates with no confirmation step, so one
misread becomes persisted damage.

**Acceptance:** a plan rebuild asks first; a meals-per-day statement has a
correct intent to land on.

## W1-8 · A correction was discarded while the bot said it agreed
**[DONE — verified live on `develop`]** · Lane E

> Status corrected 2026-07-29: this entry read `[REPRODUCED]` while the fix was
> already merged. `apply_schedule_correction` is called before the
> acknowledgement in `noam_coach/bot/assistant.py`, pinned by
> `tests/test_schedule_correction.py` and `tests/test_schedule_correction_wiring.py`.
>
> The saved plan is still **not** reconciled after the correction — that is a
> separate, still-open defect, now owned by **A9** in
> `docs/PERSONALIZED_WORKOUT_ARCHITECTURE_PLAN.md`.
**Severity: HIGH**

"אני מתאמן בשישי לא בשבת" → classified `redundant_question_challenge` (0.95).
The bot replied **"צודק, אשתמש במידע שכבר יש לי"** and emitted **no state
mutation**. `weekly_availability` still decodes to Mon, Wed, **Sat**, Sun.

You said it twice (07:01:03 and 07:15:09). Both discarded.

**Acceptance:** a schedule correction updates availability, or the bot says it
cannot and asks. It must never claim agreement while dropping the input.

## ✅ W1-9 · Duplicate detection matches against rejected meals
**[DONE — #40]** · Lane A
**Severity: MEDIUM · You reported this verbatim**

`meals.py:262-264` filters user, file id, 6-hour window and kind — **never
`status`**. Approval `W-RbRHEKu94` was rejected at 07:07:57 and its image
deleted; `Lk2XAFA31bI` raised a duplicate against it **15 seconds later**.

Your note: *"זה ארוחה שלא אישרתי לכן לא אמור להופיע לי ההודעה הזאת"*.

**Fix:** add `AND status='pending'`. **Acceptance:** a rejected meal never
raises a duplicate; the pending row is also swept.

## W1-10 · Exercise substitution orphans the set history
**[SPLIT — absorbed into A2 + A8; no longer a standalone item]** · Lane D
**Severity: MEDIUM**

After substituting, `sets.exercise_id='lat_pull'` matches no plan entry (only
`original_id`). Verified: `Match in plan by id? False`.

> **Correction 2026-07-29 — the original description was factually wrong.**
> It claimed `workout.py:868` *raises* `StopIteration`. It does not: the
> exception is **caught** at `workout.py:871`, together with `KeyError`,
> `TypeError` and `json.JSONDecodeError`, and the handler falls back to
> `session["exercise_index"]`. The delete is correct and the transaction is
> intact — no data is corrupted.
>
> The real residual defects are narrower, and there are **two**:
> 1. The fallback uses the **live pointer**, which is wrong only once the user
>    has advanced past the substitution point.
> 2. `next(...)` returns the **first** matching index. A plan that legitimately
>    programs the same exercise twice (a superset, or one movement early and
>    late) rewinds to the wrong occurrence. This is independent of substitution
>    and was never catalogued.

- **Undo targets the wrong occurrence** — now owned by **A2** (occurrence
  identity on `sets`).
- **Progression history is orphaned** — `workout.py:454`, `workout.py:625` and
  `noam_coach/services/training.py:268` all look up by `exercise_id`. That is
  **three** readers, not two; now owned by **A5** and **A8**.

See `docs/PERSONALIZED_WORKOUT_ARCHITECTURE_PLAN.md`.

---

# 🔴 WAVE-1B — Observability (this is why defects survive)

## W1-11 · The causal tree has zero edges
**[REPRODUCED]** · Lane C
`parent_span_id` is **NULL on all 779 rows** (verified). Only 114 carry a
`span_id`. `interaction_scope` resets `_span_id = None`
(`obs_context.py:85`), and the two `span_scope()` call sites
(`telegram_ingress.py:349`, `ai_invocation.py:198`) are never nested. So
`trace_reader.span_children()` is dead code against real data.

## ✅ W1-12 · No interaction ever terminates
**[DONE — #41]** · Lane C
There is **no `interaction.completed`/`.failed` event in the taxonomy**. All 87
interactions are formally unterminated — no latency, no verdict, no way to tell
"handled" from "silently dropped".

**This is exactly what hid the dead button:** `routing.decided` is written
*before* dispatch and nothing records the outcome.

## W1-13 · The application logs almost nothing
**[PARTIALLY COMPLETE — count target MET and retired; residual re-scoped 2026-08-13]** · Lane C

> **Re-measured on `2895559`: product-wide logging rose 57 → 108 calls (+89%),
> so the original count target is met and is retired.** The new calls landed in the
> service layer A12/A13 were already writing (e.g. `substitution_patterns.py` 29,
> `plan_mutations.py` 14), **not** in the files this item named. The surviving
> residual is exactly: **`noam_coach/bot/workout.py` = 0** (unchanged) and
> **`noam_coach/bot/assistant.py` = 1**; `callback_session.py` now has 3. Re-scope
> W1-13 to instrumenting those two files, not to a product-wide count.

Across 2h15m, 63 taps and 20 messages, the app wrote **2 log lines**.

| module | lines | LOGGER calls |
|---|---|---|
| `callback_session.py` | 1,236 | **0** |
| `workout.py` | 962 | **0** |
| `assistant.py` | 409 | **0** |

57 logging calls in the entire product.

## W1-14 · Safety events exist only in `audit`
**[REPRODUCED]** · Lane C
Cross-checking all 7 `audit` rows: **`safety_alert`** (the knee/elbow
constraint) and **`approve_substitution`** have no domain event in
`product_events` — only generic boilerplate. A canonical-stream reconstruction
would report neither happened.

## W1-15 · Half the callbacks produce no domain event
**[REPRODUCED]** · Lane C
33 of 63 emit only boilerplate. The entire **split-set sub-flow**
(`split`→`splitw`→`splitr`) mutates workout data and emits **zero**
`state.mutated`. Within one flow: `rir:` and `ready:` emit, `reps:` does not.

## W1-16 · `state.mutated` cannot reconstruct its own mutation
**[REPRODUCED]** · Lane C
42 of 48 have no `before_state`; 16 have neither. Eight mutually disjoint
payload shapes share the name, and `event_version` is `1` on every row.

## W1-17 · Three stores, no shared key, case-drift names
**[CLOSED — OBSOLETE / SUPERSEDED BY ARCHITECTURE, 2026-08-13]** · Lane C

> **Status history, preserved.** This entry read `[DONE — #42]`, which was
> **withdrawn on 2026-08-03** as a misattribution (PR #42 is W1-3, window
> anchoring). The item was then **reopened** on the claim below. That reopening
> mechanism has now itself been **disproven by measurement**, and the item is
> closed as obsolete — **no work was done, and none is required.**

**The original text of this item is retained for the historical record:**
*"`analytics_events` and `audit` have no `trace_id`. The same fact is written as
`user_callback` and `USER_CALLBACK` (and 5 more pairs), so a cross-store join on
`event` returns nothing for 6 of 7 types."*

**Why it is obsolete:**

1. **The case-drift half is refuted.** `track_event`
   (`noam_coach/services/core.py:118`) is a **deliberate dual write**: `:121-127`
   inserts into `analytics_events` verbatim, then `:128-139` calls
   `event_log.append_event(..., event.upper(), ...)` → `product_events` (root
   `event_log.py:105`). So the router's lowercase `"user_callback"` lands as
   `USER_CALLBACK` in the table `data_quality.py:249` actually reads. Measured on
   a temp DB (real code path, live DB never opened): `can_send_proactive` returns
   `(False,'user_recently_active')` for both the callback and the message path.
   **The claim that the suppression query is dead is formally withdrawn.**
   Normalizing either spelling would **break** `data_quality.py:249` and the
   tests at `tests/test_analytics_dsar_redaction.py:144/160-161/167`, which pin
   both spellings deliberately.
2. **The three-store premise is the intended architecture.**
   `noam_coach/observability/__init__.py:17-19` declares `audit` and
   `analytics_events` **"intentionally remain separate stores with separate
   purposes"**, layered under `product_events` as the canonical interaction trace
   (`event_log.py:8-16`, `config.py:94`). The cross-store `event` join described
   above is one **no code performs and none needs**.
3. **No `trace_id` migration is justified.** Every reader of `audit` and
   `analytics_events` was enumerated; none keys on trace. Full reasoning and the
   reopen condition are recorded in `docs/WORK_MANAGER_STATE.md`.
4. **The one surviving sub-concern is transferred to W1-14** — safety evidence
   reachable only via `audit`. W1-14's own fix (emit a product event) closes it
   with no schema change, because `write_audit` already carries
   `entity`/`entity_id` as a join key.

---

# 🟠 WAVE-1C — Nutrition engine

## W1-18 · Meals-per-day has exactly one entrance, and it was never opened
**[PARTIALLY COMPLETE — headline CLOSED, residual re-scoped 2026-08-13]** · Lane A

> **The headline defect is closed, by direct work.** A second entrance now exists
> at `noam_coach/bot/assistant.py:710-712` (`set_meal_frequency` →
> `_handle_meal_frequency` → `persist_preferred_meal_count` at `:752`), so a user
> who says "6 ארוחות ביום" outside onboarding is honoured. The earlier note that
> this was "partially fixed incidentally by W1-7" is **withdrawn as wrong.**
> `resolve_remaining_meals_estimate` reads the preference
> (`noam_coach/services/day_plan.py:326`) and clamps at **10**, not 3
> (`day_plan.py:43-44`).
>
> **The surviving residual is narrower and lives elsewhere:**
> (a) `MEAL_SPACING_HOURS = 2.5` truncates any stated band ≥5 from mid-afternoon
> (`day_plan.py:263-267`), while its docstring claims it bites only late in the day;
> (b) **`noam_coach/services/nutrition_context.py:402`** is an independent third
> estimator hard-defaulting to 3 and ignoring the preference — the real surviving
> "cap of 3"; (c) the bridge at `noam_coach/services/next_meal.py:648-649` is
> wrapped in a bare `except Exception` that silently degrades to the legacy cap.

**Original text, retained:** `persist_preferred_meal_count` (`profile.py:682-697`)
is reachable **only** from `q_daily_routine` (`onboarding.py:3164, 3186`).
Everything downstream — parsing, banding, propagation — is built and tested. Only
the trigger is missing. `_meals_remaining` (`next_meal.py:571-589`) **hard-caps at
3** and reads no preference at all, which is why you saw exactly 3 slots after
asking for 6. *(That function survives at `next_meal.py:589` but is now only the
`legacy_estimate` fallback argument, not the primary path.)*

## W1-19 · Calorie target has two resolvers over different stores
**[PARTIALLY COMPLETE — the `DONE — #40` mark is WITHDRAWN, 2026-08-13]** · Lane A

> **Withdrawn:** this entry claimed `DONE — #40`, which is **unsupported by
> repository evidence**. The two-resolver framing is stale, but the **read side is
> still open**: `proposal_only` is written once
> (`noam_coach/services/goals.py:409`) and has **zero production readers**, so an
> unapproved 0.55-confidence proposal remains consumable as if approved. The 2100
> fallback also survives (`noam_coach/services/nutrition_context.py:441` →
> `:318-325`, `config.py:83`), with `noam_coach/services/next_meal.py:418`
> contributing a fourth number (2000). Scope W1-19 to the read side.
`nutrition_context.py:318-325` reads `goal_versions` → falls back to
`SETTINGS.default_calories` (**2100**). `goals.py:337-405` computes **2290** and
writes a `proposal_only` fact. Two screens can legitimately show different
numbers, and did (2100 at 05:00, 2300 at 06:57).

**`proposal_only` is written but never read** — nothing branches on it, so an
unapproved 0.55-confidence proposal is consumable as if approved.

## W1-20 · Zero-width eating window feeds ten consumers
**[REPRODUCED]** · Lane A
`first == last == "08:00"`, `typical_meal_hours []`, from **one** meal.
Ten consumers read it; **none guards `first == last`** or checks `meals_sampled`.
The worst is `recommendations.py:168-171` (**repo root**, not
`noam_coach/services/`), which tells the menu-generating AI the user eats in an
instantaneous window.

> **Re-verified 2026-08-13 — still open, and the root cause is deeper than
> recorded.** No `first == last` guard exists anywhere; the only site touching both
> fields is a display helper (`noam_coach/services/health_jobs.py:564-566`,
> truthiness only). Critically, **W1-5's confidence attenuation provably cannot
> reach these consumers**: they read the `routine_profile` blob directly
> (`noam_coach/services/nutrition_context.py:200-208`, a raw
> `SELECT profile FROM routine_profile`) rather than through `user_model`, so the
> attenuated confidence is computed and then structurally unreachable. A fix
> applied only in `user_model` is therefore insufficient — the correction belongs at
> a shared read/quality boundary, not in each consumer separately.

## W1-21 · No macro-consistency validation
**[REPRODUCED]** · Lane A
Stored meal: 25P + 6C + 0F = **124 kcal** vs **140 stored** (12.9% off).
`meal_validation.py:95` only catches protein alone exceeding calories.
An Atwater band belongs in `meal_plausibility.check_item` as a **warn** (alcohol,
fibre and sugar alcohols legitimately break Atwater).

## W1-22 · Unit labels are not singularized
**[OPEN — but RE-SCOPED to a wiring gap, 2026-08-13]** · Lane A
`"יחידות"` (plural) misses the singular-keyed portion tables (12 entries total),
so `materialize_count_quantity` declines and the AI's raw gram estimate is
rendered with a volume label picked by name keyword (`meals.py:699-704`).

> **REUSE BEFORE BUILD — the singularizer already exists; do not build a second
> one.** The tables live in **root `meal_intelligence.py:1406-1425`**, not in
> `noam_coach/bot/meals.py` as stated above. `meal_intelligence.py:115` already maps
> `יחידות → יחידה` in `_PORTION_UNITS` — but that map is consulted **only** at
> `:264-265`, on a different parsing path. The materialization path compares `unit`
> **raw** at `:1479`, `:1487` and `:1491`, so a plural label never reaches
> `_DISCRETE_UNIT_GRAMS`. The requirement is to **wire the existing canonical map
> into the materialization path**, not to add a parallel normalizer.

## W1-23 · Multi-item restriction classification handles only the first
**[PARTIALLY COMPLETE — half 1 open at TWO sites; half 2 already done, 2026-08-13]** · Lane A
`parsed_items[0]` (`onboarding.py:3166`) — items 2 and 3 are silently dropped.
Your note said exactly this. The comma splitter also does not split the Hebrew
conjunction "ו", so "טורטייה ואגוזים" stays one item.

> **Half 1 — STILL OPEN, and at two sites, not one.** The real lines are
> `noam_coach/bot/onboarding.py:3550` (the diet-restrictions path) **and** `:3455`
> (an independent copy on the allergies-gap path, which does not even persist the
> remaining items). The `qa:diet_type:` callback that consumes the keyboard
> (`onboarding.py:995`, built at `:3029-3036`) is single-item by construction, and
> `noam_coach/services/question_dedup.py` re-dispatches the same callback
> (`:155`, `:165`, `:264`) — so any sequencing must be honoured there too.
>
> **Half 2 — ALREADY DONE.** `onboarding.py:3643` splits on
> `[,\n]+|\s+ו\s+`, so the "ו" connector is handled. **Caveat:** the pattern
> requires whitespace on *both* sides, so the example above
> (`"טורטייה ואגוזים"`, prefixed vav — the normal Hebrew orthography) **still
> stays one item.** Mechanism present; the cited case still fails.
>
> **The requirement is: every parsed restriction is classified, none silently
> dropped.** The state mechanism is **not** predetermined — prefer the existing
> `active_flow` payload / conversation state machinery (`conversation.set_active_flow`
> accepts a persisted `payload` dict; `onboarding.py:2113` already stores a list in
> it). A new queue or new persisted state requires recorded evidence that the
> existing flow payload cannot represent the sequence.

---

# 🟡 WAVE-1D — UX, copy, navigation

> **Re-verified 2026-08-13: still OPEN and still not parallelizable with any logic
> lane** — there is **no central strings module**, so Hebrew literals remain inline in
> the handlers that own the logic. Two items now carry code evidence: **W1-33** —
> `noam_coach/bot/callback_menu.py` ships the home button as both `⬅️ תפריט`
> (`:173`, `:185`, `:307`) and `🏠 תפריט` (`:477`, `:504`, `:521`) from one file;
> **W1-29** — 12 ASCII `ק"ג` vs 31 gershayim `ק״ג` occurrences across `noam_coach/`.
> The rest of the band remains UNVERIFIED in either direction.

| ID | Item | Evidence |
|---|---|---|
| W1-24 | 46% of taps are navigation; `menu:home` tapped 10× | 29 of 63 taps |
| W1-25 | Same screen re-rendered 5× in 53s; plan hub 4× | ids 301,316,330,353,374 |
| W1-26 | `safety_training_limitations` asked 3×, `q_primary_goal` 2× | `flow.completed` fires for unanswered questions |
| W1-27 | 12 of 92 renders (13%) ship zero buttons — dead ends | ids 8,29,46,110,205,221,390,454,571,589,592,619 |
| W1-28 | 1545-char weekly plan re-sent verbatim 12 min apart | ids 491, 753 |
| W1-29 | `ק"ג` (ASCII, 3 msgs) vs `ק״ג` (gershayim, 10 msgs) — split cleanly by module | — |
| W1-30 | Three calorie/gram systems; id 38 uses all three in one screen | — |
| W1-31 | `StepCount` raw identifier shown twice in Hebrew UI | id 199 |
| W1-32 | `Leg Press`, `Full Body`, `A/B/C`, `RIR` untranslated | ids 479,491,753 |
| W1-33 | Home button ships as both `⬅️ תפריט` and `🏠 תפריט` from one file | `callback_menu.py` |
| W1-34 | Empty section header + opaque "ועוד 2 פריטים" in a 1155-char screen | id 241 |
| W1-35 | 9 messages exceed 6 buttons; max 11 | id 2 |
| W1-36 | 12 interactions over 5s with no typing indicator evidenced | — |

---

# 🟡 WAVE-1E — Data hygiene (live DB)

> **BLOCKED BY GOVERNANCE, not obsolete (recorded 2026-08-13).** Every item in this
> band is a live-database row correction with **no code component**. The standing
> constraints forbid using the live DB, and the Bot/API is running and must not be
> touched or restarted. These items stay registered and **unschedulable** until an
> explicitly authorized maintenance window exists. Do not reclassify them as
> complete or obsolete.

| ID | Row | Action |
|---|---|---|
| W1-37 | `food_environment_context.raw_text` holds a dev note, `confirmed=1` | Delete the fact; it has zero real signal |
| W1-38 | `weekly_availability` has Saturday, you train Friday | Correct after W1-8 lands |
| W1-39 | `allergies='none'` parses into a phantom restriction `canonical_id='none'` | Remove; verify the parser ignores "none" |
| W1-40 | `eating_windows` from 1 meal, `workout_pattern` from 1 session | Delete; let them re-derive under W1-5 |
| W1-41 | Session 1 still `active`, never closed | Close it |
| W1-42 | `conversation_state` holds an abandoned split set (45.5 kg stranded) | Sweep |
| W1-43 | Approval `Lk2XAFA31bI` pending forever | Resolve; add a TTL |

---

# Sequencing

```
W1-1  restrictions unenforceable   ← food safety, do first
W1-2  calorie target 130 low       ← affects you every day
W1-3  window anchoring             ← root cause of W1-2
W1-4  weekday widening             ← why Saturday persists
      ↓
W1-11..W1-16  observability        ← without this the next audit is blind
              (W1-12 done; W1-17 closed obsolete; W1-16 -> W1-14 -> W1-15 serial)
      ↓
W1-5..W1-10   correctness batch
      ↓
W1-18..W1-23  nutrition engine
      ↓
W1-24..W1-36  UX + copy            ← parallelizable only where files are disjoint
      ↓
W1-37..W1-43  data hygiene         ← after the code that would re-create the rows
```

**Parallel lanes:** observability (W1-11..17) touches
`noam_coach/observability/*` and is disjoint from everything else. Nutrition
(W1-18..23) and workout (W1-10) are disjoint. UX/copy is **not** parallelizable
with logic — there is no central strings module, so Hebrew literals live inside
the handlers that own the logic.

---

# Out of scope
AI Gateway · `planning._meal_slots` Phase-2 · multi-user · live Apple Health
integration · destructive sleep-fact migration · ledger decision U-3.

---

# W1-44 · Supported reconciliation of a saved plan after an availability change

**[ABSORBED into A9 — not started]** · Lane D

> Reassigned 2026-07-29. The design decision this was blocked on is resolved:
> the answer is a **copy-on-write weekday remap proposed for approval**, not an
> in-place edit (test-forbidden), not invalidation, and not silent regeneration.
>
> W1-44 is now **A9** in `docs/PERSONALIZED_WORKOUT_ARCHITECTURE_PLAN.md`, one
> operation on a single supported mutation boundary. **No standalone bypass is
> authorized** — in particular no direct write to the `active_workout_plan`
> fact, and no direct JSON or database mutation of a saved plan.
>
> The scope and design notes below remain accurate and are retained as the
> historical record.

## Why this exists

W1-8 made a training-day correction apply to the availability facts. It
deliberately does **not** touch the saved workout plan, because an architecture
gate restricts direct readers of the `active_workout_plan` fact to an allowlist
and routes everyone else through `workout_catalog` — which today exposes
**readers only**. There is no supported way to write a corrected weekday back.

Removing that capability was the right call (see `PIL-003`), but it leaves a
real gap:

> A user corrects "I train Friday, not Saturday". Availability updates. A saved
> plan may still schedule a Saturday session until it is rebuilt.

This item closes that gap **as a domain capability**, not as a patch.

## What this is NOT

- Not "fix stale Saturday" — a targeted edit to one fact would recreate exactly
  the boundary violation the gate rejected.
- Not broadening `_ALLOWED_READER_FILES`.
- Not a generic fact-mutation helper. A general "write any field of
  `active_workout_plan`" API hands every caller the ability to desynchronise the
  plan from availability, which is the failure mode this is meant to end.

## Shape

A narrow operation owned **inside** the workout-plan domain, e.g.:

```python
reconcile_saved_plan_after_availability_change(
    db, user_id, *, availability: Sequence[int], reason: str
) -> ReconciliationOutcome
```

Callers state *what changed and why*; the domain decides what happens to the
plan. Callers never name a field.

## Design decisions this task must resolve

**1. Edit, regenerate, invalidate, or version?** Four different products:

| Strategy | Keeps | Costs |
|---|---|---|
| Edit weekdays in place | session identity, history | may violate the plan's own spacing/recovery rules |
| Regenerate | internal consistency | discards user edits and exercise substitutions |
| Invalidate + prompt | honesty, user control | leaves the user without a plan until they act |
| Version (new revision, old retained) | auditability, reversibility | more state, needs a selection rule |

Not answerable from the code — it is a product decision about whose intent wins
when a schedule change makes an existing plan partly invalid.

**2. Both tiers.** Tier-2 is the `active_workout_plan` fact; Tier-1 is the
authoritative `plan_versions` row read by `workout_catalog._tier1_sessions`.
Reconciling only Tier-2 creates a second source of truth — the precise defect
W1-2 cost 130 kcal/day. Either both move, or the operation must state which is
authoritative and why.

**3. Completed and in-flight sessions.** A session already logged against a
removed day is history and must not be rewritten. A session scheduled for
today, mid-workout, is a live object. The operation must define both.

**4. Auditability.** Every reconciliation needs a recoverable before/after and
a reason. `user_fact_history` writes only for keys that already exist (see
W1-6), so this likely needs an explicit record.

**5. Truthful user-facing behaviour when reconciliation is delayed or
impossible.** The current reply is honest because it claims only what it did.
Any richer behaviour must stay that way — never "your plan was updated" unless
it was.

## Acceptance criteria

- `_ALLOWED_READER_FILES` unchanged; the architecture guard passes untouched
- No caller outside the workout-plan domain reads or writes the fact directly
- Availability and the saved plan cannot disagree after the operation returns —
  or, if reconciliation is deferred, both the API and the user-facing reply say so
- Completed sessions are never rewritten
- The before/after and the reason are recoverable
- The W1-8 test asserting the plan is currently *unchanged* is updated
  deliberately, with its replacement asserting the new contract

## Dependencies

W1-8 (#52) and its wiring must be merged first — this reconciles what that
flow corrects.
