# Sanitized Manual-Session Audit — 2026-07-18

All identifiers in this document are stable aliases (USER_1, TRACE_nnn,
INTERACTION_nnn, MEDIA_nnn, MEAL_nnn, AI_CALL_nnn). Raw Telegram ids,
provider file ids, free text with personal details, health measurements,
AI prompt contents and local paths were removed or replaced; causality is
preserved via the aliases and the append-ordered
`SANITIZED_EVENT_INDEX.json`. The unsanitized evidence (full trace,
timeline, recovered images) exists only in a local private package that is
not part of this repository.

## 1. Repository and audit anchor

| | |
|---|---|
| Inspected branch | `review/2026-07-18_1` |
| Inspected HEAD | `7877077` |
| Session window (UTC) | 2026-07-18 06:21:05 → 07:03:51 (~43 min, no internal gaps) |
| Session window (local) | 09:21 → 10:04 (UTC+3) |
| Volume | 905 canonical events · 121 interactions · 13 traces |
| Interaction kinds | 108 callbacks · 9 texts · 3 photos |
| AI calls | 6 (1 intent classification — failed; 3 image analyses; 2 reanalyses) |
| Observability mode | `content` (default) |
| Runtime under audit | the bot process ran the PRE-review-branch build (the fixes on `review/2026-07-18_1` were not deployed during this session) |
| Export methodology | canonical `product_events` stream, selected by event-id window after the previous session's last event; deterministic timeline rendered by the existing O9 renderer; read-only inspection |

## 2. Chronological user journey

1. **TRACE_001-005 — workout planning and editing (INTERACTION_002-073).**
   The user generated three workout-plan alternatives, chose "ביצועים",
   walked the 3-step wizard, and edited exercise parameters extensively
   (rest 1:30 for whole workouts, two weight changes) via the typed
   preview→confirm path. Routing, previews, applies and re-renders were
   coherent throughout; parameter edits persisted and re-displayed
   correctly. The user then started workout B, viewed the load explanation
   ("איך חושב?"), logged one set (rest timer started/cancelled correctly),
   opened the finish dialog, chose "✅ סיים מלא" (F-A4), reopened, and
   finally cancelled the workout (F-A3 context).
2. **TRACE_006 — medication report (INTERACTION_074-075).** Flags →
   "לקחתי תרופה" → typed a medication name (redacted) → fact stored and
   acknowledged. Coherent.
3. **TRACE_007 — free text "מה עכשיו?" (INTERACTION_076).** The intent AI
   call failed (provider `BadRequestError`, class `api_error`); the
   deterministic keyword fallback delivered the capabilities screen
   (F-A7). Graceful degradation worked as designed.
4. **TRACE_008-010 — meal photo #1 and corrections (INTERACTION_077-101).**
   Photo (MEDIA_001) analyzed; user corrected the identity by text
   ("לא פלאפל, שניצל") — identity swap worked; then questioned the
   quantity semantics ("איך שניצל זה 3 כדור?") — the reanalysis produced a
   3-GRAM schnitzel and the meal was approved and persisted at a
   physically implausible ~210 kcal total (F-A2, MEAL_001). The user then
   browsed next-meal (two refreshes returned an identical proposal —
   known limitation), status, profile (F-A10), Apple Health screen
   (F-A8), weekly summary, and the morning briefing (F-A6).
5. **TRACE_011-013 — meal photos #2/#3 and the approval dead-end
   (INTERACTION_102-121).** Photo #2 (MEDIA_002, a protein drink,
   MEAL_002) was analyzed and presented. Photo #3 (MEDIA_003, MEAL_003)
   arrived while MEAL_002's card was open — the first flow was suspended
   and the new card presented. The user corrected MEAL_003's identity by
   text (worked), pressed ❌ on the OLD pre-correction card
   (INTERACTION_105) — which consumed MEAL_003's approval and resumed the
   suspended MEAL_002 flow *without re-presenting it* — and then pressed
   **"✅ שמור" on the corrected card eleven times (plus one ⚖️ press),
   every press consumed with zero response** (F-A1). A final ❌ press
   (INTERACTION_119) "worked" and closed out the state. Neither MEAL_002
   nor MEAL_003 was ever saved. The session ended on next-meal/status
   screens whose totals still count only the erroneous 210 kcal meal.

## 3. Findings (ranked)

### Critical

**F-A1 — Meal approval dead-end: 11 consecutive save presses silently
dropped; the meal is unrecoverable through the UI.**
- Evidence: INTERACTION_105 (reject consumes MEAL_003's approval, resumes
  MEAL_002), INTERACTION_106-118 (11× `approve_meal` + 1× `editqtymenu`
  presses on the corrected card, each: received → control resolved →
  routing `meal_flow/consume` → **no further events**), INTERACTION_119
  (final reject renders "נדחתה" and completes the flow).
- Reproduction sequence: photo A → photo B while A's card is open (A
  suspends) → correct B by text (new card) → reject B **on the older
  card render** → press save on the corrected B card.
- Actual: every save press is silently ignored; the corrected meal is
  lost; the suspended meal A is never re-presented and is lost too.
- Expected: either the reject invalidates all of B's live cards visibly,
  or the corrected card's save still works; the suspended card must be
  re-presented on resume; any refusal must be visible.
- Owning subsystem: meal approval lifecycle (approval consumption vs.
  live card renders; meal-flow suspend/resume).
- Likely root cause: one approval id backs multiple live card renders;
  reject consumes the approval; subsequent `persist` returns None and the
  handler returns silently. (The *silence* half is already fixed on
  branch `review/2026-07-18_1` — refusal events + terminal card edit —
  but the *card-lifecycle* half, and resume-without-re-present, are open.)
- Confidence: high. **Evidence sufficient for implementation.**

**F-A2 — Quantity-unit confusion persisted a 3-gram schnitzel; daily
totals are built on a ~210 kcal meal that is wrong by roughly 3-4×.**
- Evidence: INTERACTION_077 (initial analysis: count-based items, e.g.
  "3 כדור"), INTERACTION_078 (identity correction OK), INTERACTION_079
  (user challenges the unit; reanalysis output contains `grams: 3.0` for
  the schnitzel item; deterministic override then *reduced* its calories
  15→8.4 consistent with 3 g), INTERACTION_080 (approve; MEAL_001
  persisted; post-save summary ≈210 kcal), all later status/morning/
  next-meal screens count 210 kcal.
- Actual: `quantity_count` (3 units) was written into the grams field;
  no plausibility validation caught an 8-kcal "schnitzel"; downstream
  budgets/remaining-protein computations consumed the wrong totals all
  session.
- Expected: unit-safe mapping (count ≠ grams); a sanity gate on
  kcal-per-item and minimum grams for named solid foods; a visible
  clarification instead of silent acceptance.
- Owning subsystem: meal reanalysis result mapping + deterministic
  validation layer.
- Confidence: high. **Evidence sufficient for implementation.**

### High

**F-A3 — Workout-state contradiction across surfaces: "האימון של היום
כבר הושלם ✅" on a day with zero performed workouts.**
- Evidence: the workout menu displayed "already completed" repeatedly
  throughout the session (before any workout attempt, e.g.
  INTERACTION_009-011 range, and again later); the weekly summary
  (INTERACTION_093) states "אימונים שבוצעו: 0"; the morning briefing
  (INTERACTION_101) states the day has no planned workout; the user's
  only session attempt was cancelled (INTERACTION_072).
- Likely root cause: completion state derived from imported Health data
  or a stale flag, presented without provenance (matches prior finding
  F-07 of review 2026-07-18_1, still needs_investigation).
- Confidence: the contradiction is confirmed; the source is not.
  **Needs targeted investigation** (workout-completion derivation).

**F-A4 — "✅ סיים מלא" saved the workout as partial.**
- Evidence: INTERACTION_069: user pressed the explicit "full" finish
  option; the render states "האימון נשמר כחלקי ⏳".
- Expected: either honor the user's explicit choice or don't offer it;
  if a <threshold policy forces "partial", the dialog must say so.
- Owning subsystem: workout finish handler.
- Confidence: high (may be an intentional threshold policy presented
  wrongly — either way a defect in choice/labeling). Sufficient for
  implementation as a UX-contract fix.

**F-A5 — Suspended meal flow resumed without re-presentation; second
meal silently lost.**
- Evidence: INTERACTION_103 (photo #3 suspends MEAL_002's flow),
  INTERACTION_105 (reject → `flow.resumed` for MEAL_002 but the render
  shown is only "הארוחה נדחתה" — MEAL_002's card is never re-shown),
  INTERACTION_119 (terminal reject completes the resumed flow while the
  pressed control carried MEAL_003's id).
- Expected: resuming a suspended meal-correction flow re-presents its
  card; a terminal action addressed to meal B must not close meal A's
  flow.
- Owning subsystem: meal flow suspend/resume + entity addressing.
- Confidence: high. Sufficient for implementation (couple with F-A1).

### Medium

**F-A6 — Morning briefing renders a Python list literal in the header:
"☀️ עדכון בוקר — ['שבת']".** Evidence: INTERACTION_101. A day-name list
is interpolated without joining. Confirmed formatting defect; trivial
fix; owning subsystem: morning briefing renderer.

**F-A7 — Intent-classification AI call failed with provider
`BadRequestError` (`api_error`) on a plain two-word message.** Evidence:
INTERACTION_076 (AI_CALL_001 failed; deterministic fallback then rendered
the help screen — correct degradation). The failure itself needs
investigation (request construction vs. transient provider error);
single occurrence, no user harm this session.

**F-A8 — Apple Health screen self-contradicts on freshness.** Evidence:
INTERACTION_090 shows "ייבוא אחרון: 2026-07-18" alongside "היום האחרון
שנקלט: 2026-06-15", while the prior session's import (three days
earlier) reported data through mid-July. One of the two derivations is
wrong or they measure different things without saying so. Needs
investigation; owning subsystem: health status renderer / import
bookkeeping.

**F-A9 — Photo #3's bytes were never persisted; the referenced image is
unrecoverable (observability/persistence gap).** Evidence: MEDIA_003 has
a full content hash recorded in the trace, but no file with that hash
exists anywhere in the configured storage tree, while MEDIA_002 —
equally unapproved — WAS persisted moments earlier. Inconsistent
persistence policy between two same-session unapproved photos; also no
provider file id is recorded in the media event, so recovery required
storage bytes. Owning subsystem: meal photo persistence path (and media
event payload completeness). Needs investigation.

### Low

**F-A10 — Read-path renders mutate user facts.** Evidence: opening the
profile (INTERACTION_087) emitted `user_fact.changed
(detected_training_days)`; approving a meal (INTERACTION_080) emitted
`user_fact.changed (workout_pattern)`. Possibly intended lazy
recomputation, but state changes on read paths blur audit semantics and
can surprise concurrent flows. Architecture smell; investigate intent.

**F-A11 — Next-meal "רענן הצעה" returns an identical proposal.**
Evidence: INTERACTION_082-083 — two refreshes, byte-identical
recommendation. Matches the known single-option limitation documented in
earlier reviews; classified as a known product limitation, not a new
defect.

### Observability gaps

- The 11 silent approval presses (F-A1) produced **no** `error.captured`,
  no refusal decision, and no delivery event — on this build a correct
  refusal, a crash and a hang are indistinguishable. The instrumentation
  that fixes exactly this (refusal decisions + callback-ack delivery
  events + boundary error capture) already exists on branch
  `review/2026-07-18_1` but was not running during the session.
  **Deploying that branch is the single highest-leverage observability
  action.**
- INTERACTION_001 contains only a delivery pair (a system/proactive
  message opening the session envelope) with no received event — a
  window-boundary artifact worth a small reader-side annotation.
- MEDIA_003's event lacks any provider file reference (F-A9), making
  independent image recovery impossible when local persistence skips.

## 4. Cross-interaction consistency checks

| Check | Result |
|---|---|
| Meal identity correction | ✅ identity swaps honored twice (both text corrections changed the card); ❌ quantity semantics broken (F-A2) |
| Reuse of learned corrections | ✅ deterministic override applied a learned item mapping during reanalysis (visible as `deterministic_override`) |
| Daily totals / remaining budget | ✅ internally consistent across save-summary, status, morning, next-meal — but all anchored on the erroneous 210 kcal meal (F-A2) |
| Active menu freshness | ✅ daily menu + next-meal recommendation invalidated on meal save |
| Workout state | ❌ contradictions (F-A3, F-A4); parameter edits themselves consistent and persistent |
| Flow continuation | ❌ suspend/resume loses the suspended card (F-A5); a workout parameter-edit flow also stayed open across unrelated navigation until a home press closed it (smell, no visible harm) |
| Callback/render correlation | ✅ all session callbacks resolved to source renders (a few without stored labels — cosmetic) |
| Stale-edit fallback | Not exercised — every edit delivery succeeded on the first attempt |
| Mini App | Not used this session |
| AI output vs persisted/rendered result | ❌ divergence in F-A2 (validated override *compounded* the unit error); ✅ elsewhere (identity enforcement clean, menus match decisions) |

## 5. Classification summary

| Class | Items |
|---|---|
| Confirmed product defects | F-A1, F-A2, F-A4, F-A5, F-A6 |
| Confirmed contradiction, source unproven | F-A3, F-A8 |
| AI uncertainty (not a code defect per se) | the initial count-unit phrasing ("3 כדור") that seeded F-A2; F-A7's provider error pending investigation |
| Acceptable/known behavior | F-A11 (documented limitation), keyword fallback on AI failure, menu invalidation behavior |
| Missing evidence | F-A9 (no bytes, no provider id); workout-completion source (F-A3) |
| Observability/instrumentation defects | silent refusals on this build (fixed on the review branch, not yet deployed); MEDIA event payload completeness |

No findings were invented; every claim above is reconstructable from the
aliased rows in `SANITIZED_EVENT_INDEX.json` and, in full fidelity, from
the private local package.
