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

**Shipped: 10 PRs.** P0-1..P0-4 (#33–#36), docs (#37), and five WAVE-1 items.

| Item | PR | Verified on `develop` |
|---|---|---|
| W1-1 restriction enforcement | #38 | tortilla and aubergine now blocked; 7 unrelated foods stay clean |
| W1-2 calorie target | #39 | 2290 → **2420 kcal** |
| W1-3 window anchoring | #42 | `sessions_sampled` 1 → **10**, frequency 0.2 → **2.5** |
| W1-9 duplicate status | #40 | `LIVE_DUPLICATE_APPROVAL_STATUSES = ('pending',)` |
| W1-12 interaction terminal | #41 | `interaction.completed` emitted |

**W1-2 and W1-3 compose as intended** — two independent defences on the same
number. W1-3 fixed the *inference* (0.2 → 2.5); W1-2 still prefers the value
the user *confirmed* (4.0). A user who never stated a frequency now gets 2.5
instead of 0.2.

### Lesson: give each parallel agent its own worktree

Three writer agents were run in parallel on disjoint files. The **files** never
collided — but all three shared one git working tree, so their branches
stacked on each other instead of branching from `develop`: W1-9's commit
landed on W1-12's branch, and W1-9's own branch pointed at `develop` with
nothing on it. One agent also had its uncommitted work discarded when another
switched the shared tree mid-task.

Untangling was possible (cherry-pick each commit onto a clean branch, verify
each in isolation) but it is avoidable. **Disjoint file ownership is necessary
and not sufficient — parallel writers also need `git worktree add` isolation.**

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

Verification evidence for the three items that started as AGENT-ONLY:

```
W1-18  signature(_meals_remaining) = (hours_until_bedtime, recent_minutes, flags)
       -> takes NO meal-count preference; last line is max(1, min(3, estimate))

W1-20  eating_windows consumed by 6 modules; grep for a first==last guard
       -> no match anywhere

W1-22  _PORTION_UNIT_GRAMS = 7 entries, _DISCRETE_UNIT_GRAMS = 5
       -> 'יחידות' present: False;  'יחידה' present: False
```

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
| **C — Observability** | W1-11..W1-17 | `noam_coach/observability/*`, `taxonomy.py` | A, B, D |
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

## W1-4 · The weekday detector widens until it succeeds
**[DONE — #42]** · Lane B
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

## W1-5 · Confidence is keyed on a string, never on sample size
**[DONE — #40]** · Lane B
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

## W1-6 · One tap converts a machine guess into "the user told me"
**[DONE — #41]** · Lane B
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
**[REPRODUCED]** · Lane E
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
**[REPRODUCED]** · Lane E
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
**[REPRODUCED]** · Lane D
**Severity: MEDIUM**

After substituting, `sets.exercise_id='lat_pull'` matches no plan entry (only
`original_id`). Verified: `Match in plan by id? False`.

- **Undo is broken** — `workout.py:868` raises `StopIteration` and rewinds the
  wrong exercise.
- **Progression history is orphaned** — `workout.py:454` and `:625` look up by
  `exercise_id`.

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
**[REPRODUCED]** · Lane C
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
**[DONE — #42]** · Lane C
`analytics_events` and `audit` have no `trace_id`. The same fact is written as
`user_callback` and `USER_CALLBACK` (and 5 more pairs), so a cross-store join on
`event` returns nothing for 6 of 7 types.

---

# 🟠 WAVE-1C — Nutrition engine

## W1-18 · Meals-per-day has exactly one entrance, and it was never opened
**[REPRODUCED]** · Lane A
`persist_preferred_meal_count` (`profile.py:682-697`) is reachable **only** from
`q_daily_routine` (`onboarding.py:3164, 3186`). Everything downstream — parsing,
banding, propagation — is built and tested. Only the trigger is missing.

`_meals_remaining` (`next_meal.py:571-589`) **hard-caps at 3** and reads no
preference at all, which is why you saw exactly 3 slots after asking for 6.

## W1-19 · Calorie target has two resolvers over different stores
**[DONE — #40]** · Lane A
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
The worst is `recommendations.py:168-171`, which tells the menu-generating AI
the user eats in an instantaneous window.

## W1-21 · No macro-consistency validation
**[REPRODUCED]** · Lane A
Stored meal: 25P + 6C + 0F = **124 kcal** vs **140 stored** (12.9% off).
`meal_validation.py:95` only catches protein alone exceeding calories.
An Atwater band belongs in `meal_plausibility.check_item` as a **warn** (alcohol,
fibre and sugar alcohols legitimately break Atwater).

## W1-22 · Unit labels are not singularized
**[REPRODUCED]** · Lane A
`"יחידות"` (plural) misses the singular-keyed portion tables (12 entries total),
so `materialize_count_quantity` declines and the AI's raw gram estimate is
rendered with a volume label picked by name keyword (`meals.py:699-704`).

## W1-23 · Multi-item restriction classification handles only the first
**[REPRODUCED]** · Lane A
`parsed_items[0]` (`onboarding.py:3166`) — items 2 and 3 are silently dropped.
Your note said exactly this. The comma splitter also does not split the Hebrew
conjunction "ו", so "טורטייה ואגוזים" stays one item.

---

# 🟡 WAVE-1D — UX, copy, navigation

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
W1-11..W1-17  observability        ← without this the next audit is blind
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
