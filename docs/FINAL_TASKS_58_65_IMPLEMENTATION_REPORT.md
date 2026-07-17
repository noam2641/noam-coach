# Final Report — Product Tasks 58–65 Implementation Program

Branch `codex/post-observability-architecture`, executed autonomously from
`689e03a` on top of the completed observability (O1–O10) and architecture
(B1–B13) programs. Order executed: 58 → 63 → 64 → 65 → 59 → 62 → 60; TASK_61
was audited only (not authorized for implementation — decision packet below).
The protected working tree (19 fingerprints) stayed byte-identical throughout;
every change in protected paths shipped as install-time wraps or editable-module
changes, reusing the canonical flow/reference/memory/observability architecture.

## Implementation summary

| Task | What shipped | Commit |
|---|---|---|
| 58 | Meal-image identity corrections as deterministic item-scoped constraints; evidence-aware Israeli-food canonicalization; portion/bone-in prompt evidence; high-impact uncertainty gate | `a823196` |
| 63 | Plan-completion answer invariant: persist-before-classify, resumable classification sub-question, none-answer resolution | `7d2a3c7` |
| 64 | Multi-fact free-text profile updates during active flows (label-proximity parser + canonical writers + flow continuation) | `31df487` |
| 65 | Answer-first "מה לאכול עכשיו" (details behind nextmeal:why / menu:status) | `1c07ae1` |
| 59 | One chronological remaining-day timeline (shared day_timeline service, allocation-split budgets, B12 lifecycle reuse) | `35fe5fc` |
| 62 | Concise scheduled morning briefing + opt-in (default OFF) full daily menu, Telegram toggle | `72d2db1` |
| 60 | Per-weekday workout-time evidence, mean-of-day-means default, entity-addressed outlier-day approvals | `99d1e47` |

## Per-task architecture

### TASK 58 — meal identity correction & portion estimation
Root causes: (1) negation-first corrections ("לא טחינה חציל במיונז") were
unparseable and fell to whole-meal AI reinterpretation; (2) nothing enforced a
corrected identity after the AI + deterministic normalization; (3) the
Israeli-food override canonicalized the ambiguous generic "טחינה" to raw-tahini
macros (595 kcal/100g) — the lookup let a generic query match a longer,
more-specific alias; (4) portion prompts let the model jump from identity to
token gram counts.
Shipped: negation-first replacement patterns in `meal_intelligence` (the
incident resolves deterministically, item-scoped — unrelated items keep
identity AND quantity); `noam_coach/services/meal_identity.py` — IdentityConstraints
parsed from current + locked corrections and enforced deterministically AFTER
every reanalysis (a rejected identity cannot return for the meal lifecycle;
the approval row's `locked_corrections` is the meal-instance memory per the
B10 taxonomy — no second store); evidence-aware `israeli_foods.lookup`
(specificity must come from the item name; raw tahini keeps specific aliases;
prepared tahini and חציל במיונז added as curated entries); replacement macros
recalculated from the curated table for the CONFIRMED food at preserved grams;
portion/bone-in/ambiguous-spread evidence blocks in the shared prompt module
(no multipliers); deterministic high-impact uncertainty gate (confidence ≤0.6,
≥30% of a ≥250-kcal meal → one targeted question naming the item). Trace chain:
`state.mutated(entity=meal_identity_constraint, memory_class=meal_instance)` →
reanalysis → `decision.finalized(entity=identity_enforcement)`.

### TASK 63 — plan-completion question deduplication
Root cause: the dietary classification card was asked WITHOUT persisting the
answer and WITHOUT pending state — typed replies were lost, restarts forgot it,
non-allergy outcomes lost the item, and the target fact stayed missing, so the
wizard re-rendered the same question. Also "אין אלרגיות" parsed as a food item.
Shipped (`noam_coach/services/question_dedup.py`): the invariant "after a
successful answer save, the next rendered question must not have the same
fact_key unless a structured clarification for that answer is still
unresolved". No-answers resolve to none and advance; classification-bound
answers are persisted FIRST (fail-closed in diet_restrictions, allergies gap
resolved pending classification); the classification is a canonical pending
question (`__diet_classify__:<item>`) — restart-safe, text-answerable, mapped
to the SAME `qa:diet_type` handler the buttons use. Required trace pinned:
question A answered → restart (real `load_pending_state`) → typed resume →
question B, item preserved.

### TASK 64 — multi-fact free-text updates
Shipped (`noam_coach/services/multi_fact.py`, wrap over the protected
`handle_onboarding_text`, outermost so single-fact answers keep existing
paths): deterministic label-proximity parser (numbers bind to the nearest
label; unlabeled numbers stay ambiguous → targeted clarification, never a
positional save) for height/weight/goal-weight/body-fat/frequency/duration/
steps/HR + sleep windows + weekdays + labeled allergies. Every value
re-validated by the canonical single-fact mechanisms (the protected
`_parse_health_fact_text_edit` ranges, `training_days_per_week` + workout
pattern mirroring, `save_user_training_availability`). Flow continuation: an
active-question answer inside the message completes the question through its
own path; otherwise one concise summary + the question restated. Trace:
`decision.finalized(entity=multi_fact_extraction)`.

### TASK 65 — focused "מה לאכול עכשיו"
Root cause: despite answer-first comments, the first screen rendered the daily
remaining headline, nutrition status, sleep line, workout-status section and
the allocation timeline before the food. Shipped: the first screen is the
recommendation + one-line reason + caveats that qualify THIS meal only
(no-meals-logged honesty, provisional/default goal, safety notices, overage,
workout-clarification hint). Everything else moved to the `nextmeal:why`
explanation surface (after-meal projection, workout/sleep lines, full
allocation timeline with סך התכנון) and `menu:status`; the action rows gained
"❓ למה זה מתאים". Legacy planner-first test contracts were updated to the new
authoritative spec (they encoded the incident's anti-pattern).

### TASK 59 — chronological remaining-day timeline
Root causes in the post-meal continuation: lexicographic HH:MM ordering put an
after-midnight bedtime first; meal slots could render untimed; redundant prose
repeated the timeline. Shipped (`noam_coach/services/day_timeline.py`, the ONE
shared builder): minutes-from-now chronology (00:36 closes the day), concrete
times on every meal slot (same synthesis as the next-meal view), allocation-split
budgets from the canonical allocator (sum ≤ remaining, per-slot < remaining),
future workout at its scheduled time (incl. B12 concrete reschedules), B12
planned meals shown until consumed/expired via their durable lifecycle,
confirmed-only sleep events, no prose below the timeline. Both surfaces
(post-meal continuation, next-meal detail view) delegate to it; recalculation
is inherent (every render derives from the current context).

### TASK 62 — morning briefing + opt-in menu
Root cause: `job_morning` pushed both the check-in AND the full pinnable menu.
Shipped (`noam_coach/services/morning_policy.py`): the scheduled delivery is
the SAME short briefing `menu:morning` renders, with check-in buttons + an
opt-in/out toggle, through the B6 boundary under the original `morning_checkin`
key (claims dedup startup/retry double-sends; failed deliveries stay failed).
The full menu is OPT-IN, default OFF, stored as a confirmed user fact
(decision-grade read — an unconfirmed estimate never drives sends), decided at
the job layer (the boundary's own `morning_menu` semantics stay pinned by
protected tests); without opt-in the menu is never attempted
(`decision.fallback_selected(reason=menu_optin_absent)`); `morningmenu:optin/
optout` take effect immediately and are traced.

### TASK 60 — workout time averages + outlier-day approval
Root cause: `learn_workout_pattern` pooled all workout rows into one typical
hour — a distinct Friday-morning routine vanished into the weekday-evening
average, and heavy weekdays dominated the suggestion. Shipped: the pattern now
carries per-weekday circular means (`weekday_hours`, ≥2 sessions per weekday —
one unusual workout is not a pattern) and derives the DEFAULT from the circular
mean of the per-day means (each recurring day contributes equally); the wizard
hour step shows the per-day breakdown + default + outlier note
(`noam_coach/services/workout_hours.py` wraps); confirming queues each outlier
weekday (circular clock distance ≥ 90 minutes from the default — 23:30 vs 00:30
is 60 minutes) for its OWN entity-addressed approval (`hrout:accept:<day>:<HHMM>`),
persisted in a conversation_state row so restarts resume it; approval stores
the day-specific `start` on the canonical `weekly_availability` slot, declining
keeps the default; a manual global-hour text correction skips the outlier
questions (explicit choice wins); with no per-day evidence the legacy
single-hour behavior is byte-compatible.

## Coaching-memory integration
TASK 58 classifies identity corrections as B10 meal_instance memories persisted
on the meal instance (locked_corrections) with the full provenance trace; the
B10 food-identity observation→proposal→confirmation machinery remains the only
generalization path. No new memory store was created anywhere in the program.

## Telegram UX changes
New/changed controls: `nextmeal:why` on the next-meal card; the classification
sub-question accepts typed answers and re-prompts on unrecognized text;
`morningmenu:optin/optout` on the morning briefing; `hrout:accept/default`
day-specific approvals in the Health wizard; the post-meal continuation renders
the chronological timeline. All flows remain restart-safe through the canonical
active_flow/conversation_state architecture.

## Proactive behavior
The scheduled morning message is concise by default; the full menu is opt-in;
retries/restarts cannot double-send (daily claims); failed deliveries remain
visible as failed. The B6 cancellation/completion suppression semantics are
untouched.

## Workout-time evidence policy
Per-weekday circular means with a ≥2-session floor per weekday; default = mean
of day-means; outlier = circular distance ≥ 90 minutes from the default,
approved days only. Conservative by construction: sparse weekdays get no
average, no override, no question.

## Trace regressions
Every task pinned its required journey against the canonical product events on
real production paths: TASK 58's incident journey through the real correction
handler + adversarial noncompliant-reanalysis enforcement; TASK 63's
answer→restart→resume→question-B; TASK 64's extraction trace; TASK 65's
answer-only first screen; TASK 59's ordering/allocation/lifecycle matrix;
TASK 62's delivered-once/suppressed/failed-visible traces; TASK 60's
evidence→default→outlier-approval→persistence trace.

## AI/prompt changes
Only TASK 58 touched prompts, via the shared editable prompt module (portion
evidence, bone-in semantics, ambiguous-spread discipline) — the three protected
analyzer prompts consume it unchanged. No model, schema, or temperature
changes. Deterministic enforcement backs every prompt instruction the incident
proved unreliable.

## Known limitations
- TASK 58 portion estimation remains model-dependent; the prompt-evidence
  improvements and the uncertainty gate bound the blast radius (a dominant
  uncertain item now asks instead of silently committing), but visual gram
  accuracy itself is not deterministically verifiable.
- TASK 62: a menu delivery that FAILED after an authorized attempt retries
  through the original protected job without re-reading the toggle — one
  already-authorized morning's retry chain (documented in code).
- TASK 63's typed-classification keyword map covers the offered categories;
  genuinely novel phrasings re-prompt with the keyboard (never lost, never
  guessed).
- TASK 64 extracts explicitly labeled facts only — deliberately: unlabeled
  numbers become clarifications, not guesses.
- TASK 60 uses a fixed 90-minute materiality threshold (documented and
  tested) rather than a distribution-adaptive rule; with today's data volumes
  a robust variance estimate per weekday would be underpowered.

## Mini App work explicitly deferred
Nothing in TASK 58–65 required Mini App changes beyond the shared canonical
state it already reads; no Mini App UX/rendering/conflict work was done, per
the program's surface priority. The next-meal Mini App payload continues to
serve the canonical recommendation (its rendering was not restyled to the
TASK-65 focused format — Telegram-only change, listed here for transparency).

## TASK 61 — decision packet (audit only; no code written)

1. **Active exercise inventory**: `training_intelligence.CATALOG` — 56
   `ExerciseProfile` entries covering the plan templates in
   `exercise_plans.PLANS` (A/B/C splits + alternatives).
2. **Existing metadata per exercise** (already structured):
   `movement` (e.g. horizontal_push, vertical_pull), `primary_muscles`,
   `secondary_muscles`, `equipment`, `joint_load` (regions: shoulder/elbow/
   back/knee/wrist/hip), `skill`, `regressions`, `progressions`,
   `technique_cues`, `contraindications`, `common_mistakes`,
   `safe_range_notes`. Pain regions: `PAIN_REGION_TOKENS` (6 regions, Hebrew +
   English tokens incl. "טניס אלבו"), TTL'd active-pain resolution
   (`active_pain_regions`, 14-day TTL, severity-aware).
3. **Current adaptation model** (the gap TASK 61 addresses): binary —
   `exercise_allowed` excludes an exercise when any active pain region
   intersects `joint_load`; `_replacement_exercise` substitutes same-movement,
   then `_pain_safe_backfill_candidates` refills from other patterns. There is
   no KEEP-with-reduced-load middle ground, no grip/elbow-demand distinction
   (a squat loads "knee/back" but a *barbell* squat also grips — not modeled),
   and no per-(exercise, region) "reduction acceptable vs replacement
   required" flag.
4. **Proposed metadata schema (additions)**: `grip_demand`
   (none/low/high-static/high-dynamic), `elbow_flexion_load` (none/low/high),
   `wrist_load`, and per-region `adaptation`: `{region: KEEP | REDUCE |
   MODIFY | REPLACE | OMIT}` with an optional `reduce_band`.
5. **Safely inferable from existing data**: movement pattern, muscles,
   equipment, same-pattern substitution sets, regression/progression chains —
   all already authored. Grip/elbow/wrist demand is *mechanically derivable*
   for most catalog entries (barbell/dumbbell rows and curls → high grip +
   elbow flexion; leg press → none) but SHOULD BE REVIEWED by a human because
   it drives medical-adjacent decisions.
6. **Requires manual authoring / product sign-off**: the per-(exercise,
   region) adaptation matrix (≈56 exercises × 6 regions, sparse — most cells
   default to KEEP), and the reduction bands.
7. **Proposed conservative load/volume bands** (for sign-off):
   REDUCE = −30–50% load OR −1–2 sets, choose ONE axis per session, never
   below the empty-bar/lightest-increment floor; MODIFY = swap within the
   same movement to a lower-demand variant (barbell→machine/neutral-grip);
   REPLACE = same-pattern substitute with region-safe profile; OMIT = drop
   with an explicit user-visible note. Never increase intensity on a limited
   region within the pain TTL window.
8. **Example mappings for the four scenarios**:
   - tennis elbow: KEEP leg work; REDUCE/MODIFY pulls (neutral grip, cable);
     REPLACE high-grip rows/curls; OMIT heavy barbell curls — today's binary
     model instead removes ALL upper-body work and backfills with legs (the
     incident: squats appearing on arm days).
   - knee pain: REDUCE squat/leg-press depth+load; REPLACE with hip-dominant
     (RDL) where tolerated; KEEP all upper work.
   - shoulder pain: MODIFY presses (incline→neutral machine), REDUCE lateral
     work, KEEP hinge/legs.
   - lower back: REPLACE barbell hinge with supported variants, REDUCE axial
     loading, KEEP machine-supported work.
9. **Safety boundaries**: adaptations only within the existing TTL'd
   active-pain model and only from user-explicit reports (B10 safety class:
   no behavioral inference); every limitation-driven change rendered with the
   existing non-diagnostic guidance line; severity ≥ high → prefer
   REPLACE/OMIT over REDUCE; nothing here is medical advice and the existing
   disclaimer stays on every adapted card.
10. **Exact product decisions required**: (a) approve the adaptation-matrix
    authoring approach (who reviews the mechanical inference); (b) approve
    the REDUCE bands in §7; (c) decide whether severity modulates the
    operation choice automatically or is always REPLACE at high severity;
    (d) decide the user-facing copy style for "why this exercise changed".

## Full verification results
Every task ran the complete gate: `compileall` clean, `ruff` clean, full
`pytest --cov` with **zero deselections** (suite growth 1618 → 1694,
all passing), evaluations **33/33**, `build_release` clean, and the protected
**19/19 fingerprints byte-identical** after every commit. Tests added by this
program: 76 across 7 new test modules (20+9+12+7+10+7+11) (+ contract updates listed in
each commit).

## Protected baseline verification
Verified after every task commit and once more after this report: **PASS** —
no protected file was edited, staged, restored, or deleted; all product fixes
in protected paths are install-time wraps registered in
`noam_coach/app/runtime.py`.
