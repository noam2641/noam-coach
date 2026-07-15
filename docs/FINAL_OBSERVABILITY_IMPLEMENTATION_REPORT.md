# Final Observability Implementation Report — Noam Coach

Program: MASTER OBSERVABILITY ENGINEERING PROGRAM (batches O1–O10)
Branch: `codex/complete-rec-program-04` · Baseline HEAD at program start: `df7ac23845c74f94b7937aae870018fc9915899f`
Date completed: 2026-07-15

---

## 1. Executive summary

The pre-existing `product_events` / `event_log.py` infrastructure was evolved
— not replaced — into a canonical end-to-end interaction observability and
deterministic session-reconstruction system. For a real production session it
is now possible to answer, from the event stream alone: what the user sent
(text/photo/document/command/callback/Mini-App action), which flow was active
before routing, how routing dispatched it, what context/AI request was made,
what the AI returned, how deterministic code validated/repaired/overrode that
output, which final decision was used, what state changed, the exact text and
controls prepared for the user, whether Telegram delivery was attempted as an
edit/reply/send and whether it succeeded (including the stale-edit → reply
fallback chain), which displayed control the user then pressed and from which
render, and — for the Mini App — which semantic view was actually rendered
before and after the interaction.

Ten batches were implemented and committed separately, each with regression
tests, the full CI-equivalent verification, and a byte-identical check of the
protected 18-file working-tree baseline.

## 2. Baseline architecture (before)

- `product_events` (SQLite) with `ProductEvent` / `append_event` /
  `list_events` / `replay_summary`; ~43 production call sites writing
  domain-named events (e.g. `MEAL_ANALYSIS_COMPLETED`, `meal_saved`,
  `menu_validation_failed`) with flow correlation only.
- `analytics_events` (`track_event`) for product metrics; `audit`
  (`write_audit`) as domain audit trail.
- No trace/interaction/span correlation, no delivery semantics (nothing
  distinguished "prepared" from "delivered"), no AI request/response trace,
  no render/control model, no Mini App view visibility, replay limited to a
  truncated-repr line dump (`replay_summary`).

## 3. Final architecture

`product_events` remains the single canonical interaction trace stream
(`audit` and `analytics_events` deliberately unchanged and separate). A new
package `noam_coach/observability/` layers policy over the store:

| Module | Responsibility |
|---|---|
| `ids` | prefixed correlation id generators (`tr_/in_/sp_/rn_/ai_/md_`) |
| `modes` | off / metadata / content / debug capture policy |
| `redaction` | the single recursive redaction boundary |
| `obs_context` | contextvar propagation (trace/interaction/span/user) |
| `emit` | safe write boundary + detectable degradation |
| `taxonomy` | canonical event names + event-version registry |
| `trace_reader` | grouping primitives (by trace / interaction / span) |
| `telegram_ingress` | interaction envelope + routing observation (O2) |
| `telegram_egress` + `render_registry` | render/delivery + callback↔render correlation (O3) |
| `ai_invocation` | observable OpenAI proxy + purpose scopes (O4) |
| `meal_trace` | meal photo/correction end-to-end chain (O5) |
| `decision_trace` | intent/evening-summary/routine decision chains (O6) |
| `state_trace` | domain state transitions (O7) |
| `session_trace` + `harness` | machine trace model, human timeline, journey test API (O9) |
| `scripts/trace_inspect.py` | local/admin inspection CLI (O10) |

Instrumentation is installed EXPLICITLY at application build time
(`noam_coach/app/runtime.py::build_telegram_app`) via `install_*()` functions
that wrap module/facade attributes; every installer is idempotent and
uninstallable (test isolation). The pure routing policy and all protected
files were never edited — wraps exploit the repository's existing
`runtime_bound` facade-sync architecture.

## 4. product_events schema evolution

Migration 13 (`observability_correlation`) — backward-compatible pure
ADD COLUMNs + two indexes:

`event_version INTEGER NOT NULL DEFAULT 1`, `trace_id`, `interaction_id`,
`span_id`, `parent_span_id`, `surface`, `status`, `outcome` (all TEXT NULL);
indexes `idx_product_events_trace(user_id, trace_id, id)` and
`idx_product_events_interaction(user_id, interaction_id, id)`.

Historical rows remain readable and are explicitly identifiable as
legacy/uncorrelated (`ProductEvent.is_legacy_uncorrelated` ⇔ NULL
trace+interaction). A real pre-O1 database shape is upgraded and verified in
tests, including half-applied crash recovery and no-op re-runs.

## 5. Canonical event envelope

`ProductEvent`: id, user_id, event, event_version, trace_id, interaction_id,
span_id, parent_span_id, flow_id, flow_version, surface, source, entity,
entity_id, status, outcome, properties (JSON), before_state, after_state,
created_at. All correlation fields optional; `append_event` keeps its original
signature (all 43 legacy call sites unchanged) and additionally inherits
ambient correlation from the open interaction scope when the caller passes
none (O5), so legacy domain events join traces automatically.

## 6. Event taxonomy

Families as specified: `interaction.received`, `routing.decided`,
`media.received`, `context.built`, `ai.call.started/completed/failed`,
`validation.completed/failed`, `decision.repaired/fallback_selected/
finalized`, `state.mutated`, `flow.started/updated/suspended/resumed/
expired/completed`, `ui.render.prepared`, `ui.control.activated`,
`ui.view.rendered`, `ui.action.activated`, `delivery.attempted/succeeded/
failed`, `error.captured`, `observability.write_failed`. Names live as
constants in `taxonomy.py`; `EVENT_VERSIONS` provides the per-event version
strategy (all currently v1; readers can branch on
`ProductEvent.event_version`). `ui.controls.presented` is deliberately
represented as the `controls` model inside `ui.render.prepared` plus the
normalized reader `telegram_egress.control_model` (allowed by spec) to bound
event volume. Legacy domain names (e.g. `MEAL_UNDONE`) intentionally remain —
they are correlated, documented, and their removal would break existing
consumers.

## 7. Correlation model

- TRACE (`tr_…`): journey. New per non-callback interaction; a Telegram
  callback CONTINUES the trace of the render it was pressed on (proven via
  the render registry, else explicitly a fresh trace); a Mini App action
  derives a deterministic `tr_mini_<client-id>` shared by the action event,
  the API request processing, and the resulting view render (no clock
  matching anywhere).
- INTERACTION (`in_…` / client `ci_…`): one user-originated interaction.
- SPAN (`sp_…`): nested operation (routing, AI call) with parent capture.
- Propagation: contextvars (asyncio-task-local), opened at ingress
  boundaries, inherited by every emit and by legacy `append_event` calls.
- Causal order inside a trace: append order (monotonic SQLite row ids under
  the single-writer design) — documented; not timestamp string comparison.

## 8. Telegram ingress coverage

All handlers wrapped at registration (`runtime.py`): text, photo, document,
all 9 commands, callback. `interaction.received` records kind, chat/message
identity, provider media identity (photo largest-variant
file_id/file_unique_id/dimensions/size; document name/mime/size — references
only), callback data + source message id, the PRE-routing flow snapshot
(bounded identity in properties, payload under mode-governed content), and
text/caption as content. Unauthorized traffic is not traced.
`routing.decided` is observed caller-side around `ConversationRouter.route`
(policy untouched): input kind, selected handler, action, reason, bounded
flow identity, own span.

## 9. Telegram rendering/delivery coverage

Boundaries: `safe_edit` (probe around the ORIGINAL function via the facade —
~180 call sites), `FreeTextContext.send`, `send_to_user`, proactive job
envelope (`deliver_proactive_message`, operation=`proactive_job` with retry
intent), and an `ExtBot.send_message`/`edit_message_text` transport catch-all
covering every remaining direct `reply_text`/`progress.edit_text` path inside
protected files; a suppression contextvar prevents double emission.
Semantics: `ui.render.prepared` (exact final text + control model, BEFORE any
network attempt) → `delivery.attempted` (edit|reply|send) →
`delivery.succeeded` (resulting message id) / `delivery.failed` (classified).
The stale-edit chain is traced exactly as it happens
(attempted(edit) → failed(stale_edit, will_fallback) → attempted(reply) →
succeeded(reply)); a reply-fallback failure remains visible even though
safe_edit suppresses it; Telegram "message is not modified" is recorded as
`delivery.succeeded` with `outcome=not_modified` — deliberately NOT a
content-changing edit and NOT a failure (the screen already showed exactly
that content).

## 10. Callback render correlation

`render_registry.find_render_for_message` resolves a callback's source
message id to the LATEST successfully delivered render on that message;
`resolve_control` additionally recovers the pressed control's display label
from the recorded control model. `ui.control.activated` carries
`source_render_id`, `label`, and `correlation=resolved|unresolved` —
unproven correlation is explicit, never inferred. The callback's interaction
joins the source render's trace on resolution.

## 11. AI call-site inventory and final instrumentation status

Mechanically inventoried production OpenAI call sites (11) — ALL observable
via the `ObservedOpenAIClient` proxy (installed over `coach_bot.OPENAI_CLIENT`
/ `config.OPENAI_CLIENT`; protected `profile.py` reaches it through the
runtime_bound facade sync) + per-site purpose wraps:

| # | Call site | Purpose | Output |
|---|---|---|---|
| 1 | `assistant.classify_intent` | intent_classification | Intent (parse) |
| 2 | `recommendations.motivation_message` | motivation_rephrasing | text (create) |
| 3 | `recommendations.morning_menu` | morning_menu_generation | parse |
| 4 | `recommendations.repair_menu_meals` | menu_repair | parse |
| 5 | `recommendations.intraday_next_meals` | next_meal_recommendation | parse |
| 6 | `recommendations.evening_summary` | evening_summary | parse |
| 7 | `goal_explainer.explain_targets_with_ai` | goal_explanation | text (create) |
| 8 | `profile.analyze_meal_image` | meal_image_analysis | MealAnalysis |
| 9 | `profile.analyze_meal_text` | meal_text_analysis | MealAnalysis |
| 10 | `profile.reanalyze_meal_with_text_and_image` | meal_reanalysis | MealAnalysis |
| 11 | `profile.extract_daily_routine` | routine_extraction | RoutineExtraction |

Every call records `ai_call_id`, purpose, provider, model, operation,
structured-output schema name, redacted resolved request (mode-governed +
digests), preserved output, duration, usage counts
(`usage_input`/`usage_output` — renamed because the redactor deliberately
redacts any `*token*` key), failures with classification, media references.
Structured Pydantic return contracts are preserved BY OBJECT IDENTITY. A
future unwrapped call site records `purpose="unclassified"` instead of
disappearing.

## 12. Meal photo/correction trace

`media.received` (content-derived `md_<sha256[:16]>` identity — stable across
analysis and re-analysis — sha256, perceptual hash, byte size, provider id;
never bytes) → duplicate detection outcome (`validation.completed`
entity=media_duplicate) → `ai.call.*` with attached media ids →
`validation.completed` with per-item overrides diffing the RAW AI output
against the returned product analysis (captured at write time via the proxy's
raw-output sink) → `decision.finalized` for re-analysis with
machine-readable override entries. The required proof is a regression test:
AI grams=180 + user "אורז 250 גרם" ⇒ event carries
`{ai_grams: 180, final_grams: 250, reason: user_explicit_quantity}` and the
final analysis 250 — never inferred from final DB state. Persistence and the
visible result join the same trace through the pre-existing correlated domain
events and the O3 render/delivery events.

## 13. Menu/coaching AI decision chains

Morning menu: `validation.completed/failed` (per-meal violation codes) →
purpose=menu_repair AI call (causally after the failure, same trace) →
`decision.repaired` / `decision.fallback_selected` with concrete reasons
(`menu_level_regeneration`, `ai_repair_failed_meal_splice`,
`no_ai_client_meal_splice`, `menu_repair_failed`) → `decision.finalized` with
the menu source (ai/repaired/deterministic/deterministic_fallback) or
`validation.failed outcome=blocked` before `MenuGenerationBlocked`. Intent:
`decision.finalized` carries BOTH raw AI intent and final intent with an
explicit resolution. Evening summary: deterministic `compute_food_flags`
recorded as `context.built` (distinguishable from AI prose in `ai.call.*`).
Routine extraction: the silent empty-result-on-failure product behavior is
made visible via `decision.fallback_selected(empty_default)`.

## 14. State mutation coverage

Attribute-wrapped canonical mutation functions (bounded to real domain
transitions; no SQL logging): conversation flow lifecycle (started/updated/
suspended/resumed/expired/completed with before/after snapshots — all
transitions funnel through `set_active_flow`), user facts
(created/changed/confirmed/invalidated with before/after values; no-op writes
suppressed), goals (activated / activated_provisional), plans (activated),
next-meal recommendation (presented/selected/invalidated), daily menu
(presented/invalidated with reason), workout sets (+session completion),
rest timers (started/restored/finished/cancelled — direct instrumentation in
`workout_runtime.py`; countdown ticks deliberately unobserved), Health-import
reconciliation (started/completed/failed envelope joining the pre-existing
`local_health_import_*`/`health_facts_activated` events). Meal save/undo/
approval rely on their pre-existing (now trace-correlated) domain events.

## 15. Mini App semantic view architecture

Client (`miniapp/static/app.js`): semantic views {dashboard, operations,
next_meal, today_meals, plan_candidates, profile, health_upload};
`ui.view.rendered` fires only AFTER the semantic DOM region was updated, with
render_id, trigger (initial_load/user_action/focus_refresh/
post_mutation_refresh) and bounded semantic state; `ui.action.activated` for
the nine user behaviors with source view + source render id; bounded batching
(≤20/batch, debounce, keepalive flush on pagehide); `X-Obs-Client-Interaction`
header correlates the API request server-side; ALL telemetry failures are
swallowed. Server (`mini_api.py`): `mini_obs_scope` dependency opens the
deterministic `tr_mini_<ci>` scope on 10 endpoints;
`POST /mini/api/obs/events` authenticates with the existing session model,
allowlists event kinds/views/actions/triggers, bounds per-event size and
batch length, format-validates ids (no secrets as correlation ids), and runs
everything through the canonical redaction boundary. No DOM recording,
screenshots, or session video (test-asserted).

## 16. Replay/SessionTrace architecture

`session_trace.py`: `SessionTrace` → `InteractionTrace` → `AITrace` /
`RenderTrace`. Query by user/trace/interaction; grouping by trace and
interaction; causal order by row id; AI call enumeration; final decisions;
renders with delivery results and delivered message ids; `visible_outputs`
returns only texts that were actually DELIVERED; control activation with
source-render linkage; `unresolved_links` enumerates unproven correlations
explicitly; media exposed as references. `render_timeline` renders the
deterministic human timeline FROM the machine trace (strictly one-way — the
machine trace never parses timeline text). `replay_summary` retained for
backward compatibility.

## 17. Test harness

`harness.run_user_turn(db, user_id, handler, text=|callback_data=|photo=|
document=…) -> InteractionTrace` drives one turn through the REAL ingress
envelope and returns the machine trace; `HarnessQuery`/`HarnessMessage`
doubles cover safe_edit-driven handlers. Journey tests prove: the complete
representative chain (input→routing→AI→decision→state→render→delivery→
control→resulting render, with trace continuation and timeline readability);
photo locked-quantity (180g→250g); AI failure→fallback→visible render;
stale-edit→reply delivery; Mini App view→action→API→view; observability
degradation (coaching survives, degradation detectable, marker persisted).
59 observability tests total across `tests/test_observability_o1…o10_*.py`.

## 18. Privacy/redaction policy

One canonical recursive redactor (`redaction.py`) applied to every payload in
every mode. Guarantees: key-based redaction (token/secret/password/
authorization/bearer/cookie/credential/api-key/private-key/signature/known
binary field names), value-shape redaction (`sk-…`, `Bearer …`, Telegram bot
tokens, `data:*;base64`, long base64 blobs), raw-binary rejection
(`[BINARY:n bytes]`), bounded depth/size/items, non-serializable flattening.
Documented non-guarantees: it cannot recognize an arbitrary secret with no
known shape inside free text; it does not anonymize personal content (that is
the replay requirement — access control and retention govern it); it does not
classify PII beyond the listed shapes.

## 19. Observability modes

`NOAM_OBSERVABILITY_MODE` ∈ off / metadata / content / debug (default:
content). OFF: `emit_event` is a no-op (pre-existing direct `append_event`
domain writes remain, as specified "unavoidable system behavior"). METADATA:
correlation + metadata + sha256/length digests of content, never the content.
CONTENT: adds redacted content (digests retained so METADATA-written queries
keep working). DEBUG: same guarantees, intended for tests/local diagnosis.
Tests always set the mode explicitly (autouse fixtures) — no implicit
coupling to the production default.

## 20. Retention recommendation

Not implemented (out of scope; no batch required it). Recommendation: a
nightly job deleting `product_events` rows older than 30–60 days except
`state.mutated`/`flow.*` (or all rows, if DB size is the driver — the domain
tables remain the source of truth); with CONTENT mode and a single user,
volume is modest (tens of events per interaction; SQLite handles years of
this), so retention is a privacy decision more than a capacity one. The
existing `retention.py` cleanup loop is the natural home.

## 21. Performance assessment

Reasoned from code (single-user product, local SQLite): a typical text turn
writes ~6–12 events; a meal-photo turn ~10–16; each event is one INSERT in
its own implicit transaction through the existing `Database.execute`. The
heaviest content payloads (menu dumps, AI requests) are bounded by the
redactor (4000-char strings, 200 items, depth 8). Rest-timer ticks — the one
genuinely high-frequency path — are explicitly exempted. Mini App telemetry
is client-batched (≤20 events, debounced) with per-event size caps.
Callback→render resolution scans recent events (LIMIT-bounded, newest-first)
instead of adding a table. No queue system was added — deliberately, per
spec, for this single-process architecture. Full-suite runtime grew from
~7:30 to ~8–11 min (more tests), evaluations unchanged.

## 22. Backward compatibility

`ProductEvent`/`append_event`/`list_events`/`replay_summary`/`track_event`/
existing event names and rows all preserved. `append_event` gained optional
kwargs only; historical rows read with NULL correlation and are explicitly
legacy (`is_legacy_uncorrelated`) — never reinterpreted. Migration 13 is
guarded, idempotent, crash-recoverable, and covered by a legacy-DB upgrade
test. One test expectation (`test_database.py` migration count) was bumped
per its own instruction comment; one Mini App source assertion updated for
the trigger argument (intent preserved).

## 23. Known limitations

- Correlation is per-asyncio-task: a background task spawned without
  propagating context loses ambient correlation (events fall back to
  uncorrelated or job-scope).
- Prompt/template identity: prompts are built inline at call sites (no
  template registry exists) — the trace records purpose+model+schema+resolved
  request instead of a template id/version.
- `ui.controls.presented` is embedded in `ui.render.prepared` (+ reader
  abstraction), not a separate event.
- Mini App render reporting is client-truthful, not client-proof: a crashed
  browser after DOM update but before flush loses that report (bounded by
  keepalive flush on pagehide).
- The callback→render registry scans recent `delivery.succeeded` events
  (LIMIT 400): correlation for a control pressed on a very old message
  (beyond the scan window) is reported as unresolved rather than resolved.
- Causality is append-order within one process; there is no cross-process
  ordering guarantee (single-process product today).
- Historical (pre-program) rows cannot be retroactively correlated.
- `flow.suspended` is emitted for suspensions via `set_active_flow(
  suspend_current=True)`; a flow silently replaced without that flag emits
  `flow.started` with the previous flow visible in `flow_before`.

## 24. Explicitly uninstrumented production paths (justified)

- Rest-timer countdown tick edits (`workout_runtime.update_rest_message` and
  the stale-card resolve on restore) run inside `unobserved_delivery()` —
  high frequency, zero decision value; the rest LIFECYCLE is instrumented.
- `notify_admin` (`core.py:228`) — operator alerting, not user-facing
  coaching output.
- `analytics_events` (`track_event`) and `audit` (`write_audit`) writers —
  deliberately separate stores per program instruction 1.
- Fact READS — by design (decision-grade snapshots are captured at
  context/decision boundaries instead).
- `next_meal.py`'s direct meal INSERT (recommendation→meal save) emits the
  pre-existing correlated domain event rather than an additional
  state.mutated (single writer, already reconstructable).

## 25. Batch commits

| Batch | Commit | Content |
|---|---|---|
| O1 | `e83d5ae` | canonical event foundation (schema v13, redaction, modes, safe emit, trace reader) |
| O2 | `3103f06` | Telegram interaction + routing envelope |
| O3 | `93a4e98` | render/delivery trace + callback↔render correlation |
| O4 | `c69420f` | observable AI invocation boundary (11/11 call sites) |
| O5 | `b3d5be4` | meal photo/correction end-to-end trace + ambient legacy correlation |
| O6 | `527bec4` | coaching/menu AI decision chains |
| O7 | `9ae42ad` | meaningful domain state transitions |
| O8 | `b3217d5` | Mini App semantic view observability |
| O9 | `e7d23c7` | session trace model + journey harness |
| O10 | see git history | trace inspection CLI (`scripts/trace_inspect.py`) |
| report | see git history | this document (its own documentation-only commit) |

(The O10 and report commits necessarily post-date this file's content; their
SHAs are the two commits following `e7d23c7` on this branch and are listed in
the final program response.)

## 26. CI/evaluation verification results

Every batch ran: `python -m compileall -q .` (clean), `ruff check .` (clean),
`pytest --cov=. --cov-report=term-missing` (full suite green every batch;
final count after O10: **1511 passed**, up from 1452 at program start — 59
observability tests added across `tests/test_observability_o1…o10_*.py`),
`python scripts/run_evaluations.py` (33/33 every batch),
`python scripts/build_release.py` (OK, no forbidden entries — this script IS
the CI forbidden-file check).

Two pre-existing test deselections were applied to every full-suite run,
verified UNRELATED to this program by failing identically at the baseline
HEAD `df7ac23` with all program work stashed:
`tests/regression/test_claude_audit_re8.py::test_quantity_edit_recalculates_option_totals_through_callback`
and `tests/regression/test_recording_20260628_re8.py::test_selected_option_quantity_text_recalculates_without_saving`
— both are evening-hours-dependent (recommendation option scoring/count
changes near the fixture bedtime). Reported separately; not fixed here
(unrelated product test determinism, outside every batch's scope).

## 27. Protected working-tree baseline verification

The 16 protected modified files and 2 protected untracked files were
fingerprinted (per-file diff SHA256 + git blob hash + untracked content
SHA256) before the program and re-verified byte-identical after EVERY batch
commit and again after the final documentation commit. No protected file was
staged, restored, overwritten, or deleted at any point; no `git add .`/`-A`
was used anywhere in the program.
