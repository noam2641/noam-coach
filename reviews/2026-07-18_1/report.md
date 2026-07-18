# Engineering Review Report — 2026-07-18_1

- Review protocol: v1.0 · focus: full
- Source commit: `99cf8898c59cf06746ecec52eb946244a375e5b7` (dirty tree)
- Evidence: events 1–632 (632 events, 68 interactions, 2026-07-15T16:06:08.358100+00:00 → 2026-07-15T17:23:01.255677+00:00)
- Detector set: v1.0 · package schema: v1.0

**9 findings** (HIGH: 1 · MEDIUM: 3 · LOW: 5)

## F-01 — Health-import wizard: 'skip for now' on the last remaining item loops back to the same item

`HIGH` · defect · evidence CONFIRMED · confidence high · frequency 3 · scope isolated · status implemented

**User impact:** The user pressed 'דלג כרגע' on the sleep-schedule confirmation three times and received the identical screen back each time; the only escape was 'דלג על שאר האישורים'.

**Observed facts:**
- Events 66/75/84: three consecutive health:skip_item presses on the sleep_schedule item.
- After each press the flow completed and immediately restarted at step __health_edit_sleep_schedule__ (flow.started events 71, 80, 89 — all with flow_after.step __health_edit_sleep_schedule__).
- The re-rendered question text was byte-identical each time (renders 63/72/81).
- Earlier in the same wizard (event 37), skip_item on sleep DID advance — to avg_steps — while other unconfirmed items remained.

**Assumptions (not facts):**
- The loop occurs specifically when the skipped item is the only unconfirmed item left: 'skip for now' re-queues it as the next pending item instead of ending the wizard.

- Evidence: events [66, 71, 75, 80, 84, 89] · interaction in_3cf6446463474be7 — first of the three identical skip loops
- Detectors: repeated_user_input
- Reproduction: synthetically_reproduced
- Root-cause hypothesis: skip_item marks the item 'ask later' and recomputes the next unconfirmed item, which is the same item when nothing else remains; there is no terminal branch for 'all remaining items skipped'.
- Affected components: health_import.py, noam_coach/services (health wizard sub-steps)
- Proposed regression test: Harness journey: drive the wizard to a single remaining item, press health:skip_item, assert the flow terminates (wizard summary rendered) instead of restarting at the same step.
- Implementation: scope small · eligibility eligible

Inspect: `python scripts/review_session.py show-finding 2026-07-18_1 F-01`

## F-02 — Stale/duplicate control presses are dropped with no visible acknowledgement

`MEDIUM` · ux_problem · evidence PROBABLE · confidence medium · frequency 2 · scope systemic_candidate · status implemented

**User impact:** Twice the user tapped a button and — as far as the recorded conversation shows — nothing happened at all, which reads as the bot hanging.

**Observed facts:**
- Event 275 (in_1bc6bde7ca064f3a): pressing the step-1 wizard button after the wizard had advanced produced routing.decided and then zero further events — no render, no state change.
- Event 601 (in_558c5fea44be4afc): a second approve_meal press after the meal was saved produced zero further events.
- Both interactions are the two interaction_no_output signals; the refusals themselves were CORRECT (no double-save, no wizard regression).

**Assumptions (not facts):**
- The user perceived silence. A Telegram callback toast (query.answer) may have been shown but is not instrumented, so the recorded experience is silence.

**Missing information:**
- Whether query.answer produced any visible toast on these two refusal paths.

- Evidence: events [267, 275] · interaction in_1bc6bde7ca064f3a — stale wizard-type press
- Evidence: events [590, 601] · interaction in_558c5fea44be4afc — duplicate approve_meal press
- Detectors: interaction_no_output, repeated_user_input
- Reproduction: synthetically_reproduced
- Root-cause hypothesis: The identity/duplicate gates (B2/B5 family) return early on refusal without sending any user-visible acknowledgement.
- Affected components: noam_coach/services/recommendation_identity.py, noam_coach/services/callback_grammar.py, noam_coach/bot/meals.py
- Proposed regression test: Harness journey: press a stale wizard control and a duplicate approve control; assert a delivered acknowledgement render (or recorded callback answer) exists in the trace.
- Implementation: scope small · eligibility eligible

Inspect: `python scripts/review_session.py show-finding 2026-07-18_1 F-02`

## F-03 — Refusal decisions and callback ACKs are invisible to the trace

`MEDIUM` · observability_gap · evidence CONFIRMED · confidence high · frequency 2 · scope repeated · status implemented

**User impact:** A correct silent refusal is indistinguishable from a hang in the canonical trace, which will repeatedly waste review time.

**Observed facts:**
- In both no-output interactions (events 267-275, 590-601) the trace ends after routing.decided: the gate refusal emitted no decision.* event.
- query.answer / answer_callback_query is not an instrumented delivery boundary anywhere in the O1-O10 event stream (no ACK events exist in this window).

**Assumptions (not facts):**
- Any control-refusal path (not only these two gates) likely shares the gap.

- Evidence: events [267, 275] · interaction in_1bc6bde7ca064f3a
- Evidence: events [590, 601] · interaction in_558c5fea44be4afc
- Detectors: interaction_no_output
- Reproduction: synthetically_reproduced
- Root-cause hypothesis: The gates predate a 'refusal is a decision' convention: nothing emits decision.finalized(outcome=refused, reason=stale_control|duplicate), and ACK toasts bypass the egress instrumentation.
- Affected components: noam_coach/observability/telegram_egress.py, noam_coach/services/recommendation_identity.py, noam_coach/services/callback_grammar.py
- Proposed regression test: Trace assertion: a refused control press yields decision.finalized outcome=refused with a machine-readable reason in the same interaction.
- Implementation: scope small · eligibility eligible

Inspect: `python scripts/review_session.py show-finding 2026-07-18_1 F-03`

## F-06 — Multiple live plan-center cards run parallel wizard instances with version churn

`MEDIUM` · state_consistency · evidence PROBABLE · confidence medium · frequency 3 · scope repeated · status needs_investigation

**User impact:** Three separate messages (1617/1619/1621) all became the plan center with live generate buttons; pressing generate on one silently invalidated the wizard buttons on another (v39 to v40), producing the dead press of F-02 and a confusing multi-card state.

**Observed facts:**
- Messages 1617, 1619 and 1621 each rendered the plan center with planv2:generate buttons during the session.
- Events 244 and 256: planv2:generate:workout pressed on two different messages within a minute; the second regenerated the wizard with token v40 while message 1621 still displayed v39 buttons.
- Event 275: the subsequent v39 press was refused with no output (see F-02).

**Assumptions (not facts):**
- The user did not realize older cards stay interactive but stale; the wizard state is global while cards are per-message.

**Missing information:**
- Whether superseding is intended UX for plan-center cards (vs. decommissioning old cards visually).

- Evidence: events [244, 256, 267, 275] · interaction in_26e21a5aadf14424
- Detectors: repeated_user_input
- Reproduction: not_reproducible
- Root-cause hypothesis: Plan-center renders are per-message but wizard/selection state is singleton; regeneration bumps the version token without editing superseded cards to a non-interactive state.
- Affected components: noam_coach/bot/callback_plans.py, noam_coach/services/flow_convergence.py
- Proposed regression test: Harness journey: render plan center twice, generate on the newer card, assert the older card is edited to a superseded state or its press yields an explicit refusal message.
- Implementation: scope medium · eligibility ineligible

Inspect: `python scripts/review_session.py show-finding 2026-07-18_1 F-06`

## F-04 — Goal-weight typo guard suggests a nonsensical example value

`LOW` · conversation_quality · evidence CONFIRMED · confidence high · frequency 1 · scope isolated · status implemented

**User impact:** A 58 kg user aiming at 48 kg was told to either confirm or write another goal, e.g. 83 — a 25 kg GAIN example that undermines trust in the guard.

**Observed facts:**
- Event 348: the guard render for input 48 (current weight 58) ends with the example value 83.

**Assumptions (not facts):**
- The example value is hardcoded (likely from a different authoring context) instead of derived from the user's current weight.

- Evidence: events [348] · interaction in_080429d68e7a44e4
- Reproduction: evidence_inspected
- Root-cause hypothesis: Static example text in the goal-weight sanity-check template.
- Affected components: questions.py, targets.py
- Proposed regression test: Unit test: the guard message example value is within a plausible band of the user's current weight.
- Implementation: scope small · eligibility eligible

Inspect: `python scripts/review_session.py show-finding 2026-07-18_1 F-04`

## F-05 — Partial-estimate goal card renders a dangling empty 'missing items' header

`LOW` · conversation_quality · evidence CONFIRMED · confidence high · frequency 1 · scope isolated · status implemented

**User impact:** The goal card promises a list of missing items after a colon and shows nothing, looking broken.

**Observed facts:**
- Event 370: the daily-goal render contains the missing-items header followed directly by the next warning block — zero items.

**Assumptions (not facts):**
- The missing-items collection was empty but the header renders unconditionally.

- Evidence: events [370] · interaction in_95a473d05f714d6b
- Reproduction: evidence_inspected
- Root-cause hypothesis: Header rendered before checking the emptiness of the missing-details list.
- Affected components: targets.py, noam_coach/bot/callback_plans.py
- Proposed regression test: Unit test: partial-estimate goal card with no missing items renders no dangling header.
- Implementation: scope small · eligibility eligible

Inspect: `python scripts/review_session.py show-finding 2026-07-18_1 F-05`

## F-07 — Workout menu claims today's workout is already completed on the user's first day

`LOW` · ux_problem · evidence PROBABLE · confidence low · frequency 1 · scope isolated · status needs_investigation

**User impact:** A brand-new user (onboarded 80 minutes earlier, zero in-bot workout activity) opened the workout menu and was told today's workout is already completed.

**Observed facts:**
- Event 1 of the database is this user's /start earlier the same session; no workout set/session events exist anywhere in the window.
- Event 623: the workout menu render states the day's workout is already completed.

**Assumptions (not facts):**
- The completion flag may derive legitimately from the Apple Health import (1,378 records through 2026-07-15, possibly including a workout today) — but the message gives no attribution, so it reads as false state.

**Missing information:**
- Whether an imported Health workout for 2026-07-15 exists and is the source of the completion flag.

- Evidence: events [623] · interaction in_ff35e2cb148941e8
- Reproduction: evidence_inspected
- Root-cause hypothesis: Workout-completion state presented without provenance; if import-sourced, the message should attribute it to the imported health data.
- Affected components: noam_coach/bot/callback_menu.py, health_import.py
- Proposed regression test: Unit test: workout menu on a day whose only completion evidence is imported attributes the completion to the import in the message.
- Implementation: scope small · eligibility ineligible

Inspect: `python scripts/review_session.py show-finding 2026-07-18_1 F-07`

## F-08 — A soft food preference is presented as a hard prohibition in the profile

`LOW` · conversation_quality · evidence CONFIRMED · confidence high · frequency 1 · scope isolated · status implemented

**User impact:** The user explicitly chose the softest option (prefer to avoid) for fruit; the profile screen lists it under hard dietary prohibitions, misrepresenting what they said.

**Observed facts:**
- Event 224: the preference-level button was chosen (label: prefer to avoid).
- Event 227: storage correctly recorded allergies=none (the classification IS distinguished internally).
- Event 436: the profile render lists fruit under the dietary-prohibitions label.

**Assumptions (not facts):**
- Menu-generation severity may also treat preference as prohibition (not proven here — the day's menus simply contained no fruit).

**Missing information:**
- Which fact the profile renderer reads and whether downstream menu constraints honor the preference/allergy distinction.

- Evidence: events [224, 227, 436] · interaction in_5b95b84b4fb044fc
- Reproduction: evidence_inspected
- Root-cause hypothesis: The profile renderer labels the diet_restrictions fact as prohibitions regardless of the stored classification level.
- Affected components: noam_coach/bot/callback_plans.py, noam_coach/services/profile.py
- Proposed regression test: Unit test: a preference-classified restriction renders under a preference label, not under prohibitions.
- Implementation: scope small · eligibility eligible

Inspect: `python scripts/review_session.py show-finding 2026-07-18_1 F-08`

## F-09 — Protein overshoot is shown as a negative remainder in the save summary

`LOW` · conversation_quality · evidence CONFIRMED · confidence high · frequency 1 · scope isolated · status implemented

**User impact:** After the second meal the save summary showed a remaining protein of minus five grams, while the status screen correctly phrases the same state as an overshoot — inconsistent, and the raw negative looks like a bug.

**Observed facts:**
- Event 598: save-summary render contains the raw negative remaining-protein value.
- The subsequent status render phrases the same state as an overshoot of five grams.

**Assumptions (not facts):**
- The save-summary path lacks the overshoot phrasing the status path already has.

- Evidence: events [598] · interaction in_862d943ee4be485d
- Reproduction: evidence_inspected
- Root-cause hypothesis: Two renderers format remaining protein independently; only one clamps and rephrases negatives.
- Affected components: noam_coach/bot/meals.py, noam_coach/bot/ui.py
- Proposed regression test: Unit test: overshoot renders the same phrasing in the save summary as in the status screen.
- Implementation: scope small · eligibility eligible

Inspect: `python scripts/review_session.py show-finding 2026-07-18_1 F-09`

---

**Engineering Review Required.** Approve findings with
`python scripts/review_findings.py set-status <review> <finding> approved --approver <you> --scope <text>`,
then run the implementation flow. Nothing is implemented automatically.
