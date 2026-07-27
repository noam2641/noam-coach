# Process improvement ledger

Durable record of **process-level** failures — not code defects. A code defect
belongs in a PR description; an entry belongs here when the work *system*
allowed the defect: a task brief that permitted a wrong reading, a gate that
did not exist, a dependency that was not sequenced, a report that claimed more
than it proved.

An entry is only complete when its preventive change is **implemented and
verifiable**, not merely recommended. "Written in a report" is not implemented.

**Escalation rule.** A repeat of the same failure class is evidence the
previous fix was too weak: first occurrence → checklist item; second →
automated test or gate; third → redesign the workflow, task boundary, or agent
responsibility.

---

## PIL-001 · A helper passed every test while the handler crashed

| | |
|---|---|
| **Date** | 2026-07-27 |
| **Work item** | Taxonomy: meals-per-day intent (PR #51) |

**Observed failure.** The handler referenced `button` without importing it — a
`NameError` on its first render. The agent's own tests were green.

**Immediate impact.** Would have shipped a dead intent: the classifier routes
correctly, then the handler raises on every invocation.

**Technical root cause.** Missing import.

**Process root cause.** The agent validated the *helper* and inspected the
handler's **source text** rather than executing it. `assert "persist_..." in
inspect.getsource(handler)` passes whether or not the function can run.

**Why existing controls missed it.** No brief required executing the real
handler. Source-inspection assertions look like integration coverage and are
not.

**Corrective action.** Import added; the handler test now drives the real
function.

**Preventive process change.** Agent rule 1 — *exercise the real runtime path*.
Source inspection is explicitly a supplement, never a substitute.

**Agent briefs updated.** `docs/WORK_MANAGER_AGENTS.md` → "Mandatory rules",
rule 1.

**Validation evidence.** The defect surfaced *only* when the test called the
handler; the same suite was green before that change.

**Status.** Implemented.

---

## PIL-002 · A correction marker that was not a split marker

| | |
|---|---|
| **Date** | 2026-07-27 |
| **Work item** | W1-8 schedule correction (PR #52) |

**Observed failure.** `במקום` ("instead of") was accepted as a *correction*
marker but was absent from the *negation split* regex. So
`"מתאמן בשישי במקום בשבת"` — Friday **instead of** Saturday — parsed to
`asserted [4, 5], removed []`: both days asserted, and the day the user was
giving up silently kept.

**Immediate impact.** Would have shipped a fix that appears to work on the one
tested phrasing while reproducing the original defect on another.

**Technical root cause.** Two phrase lists governing one concept, kept in sync
by hand.

**Process root cause.** The task brief named the live utterance as the
acceptance case. The agent satisfied it exactly. Nothing required covering
*other phrasings of the same intent*, so a single-example brief produced
single-example coverage.

**Why existing controls missed it.** The agent's revert-verification passed —
it proved the fix worked for the phrasing that was specified.

**Corrective action.** `_NEGATION_SPLIT_RE` now carries every displacement
marker. The regression is **parametrized across all three phrasings**, so a
marker added to one list without the other fails immediately.

**Preventive process change.** Agent rule 4 — *corrections are deltas*, with
the explicit requirement that every phrase gating recognition must also split.
Manager practice: a brief that names one example utterance must also require
coverage of alternative phrasings of the same intent.

**Validation evidence.** Reverting the split regex fails exactly the `במקום`
case and no other — the correct signature for a phrasing-specific gap.

**Status.** Implemented.

---

## PIL-003 · An architecture gate was the design review

| | |
|---|---|
| **Date** | 2026-07-27 |
| **Work item** | W1-8 schedule correction (PR #52) |

**Observed failure.** CI rejected the branch: `health_jobs.py` became a direct
reader of the `active_workout_plan` fact, outside the approved allowlist.

**Immediate impact.** None shipped — the gate held.

**Technical root cause.** Realigning the stored plan required reading a fact
whose access is restricted to `workout_catalog`, which exposes **readers only**
and has no supported mutation path.

**Process root cause.** The task asked "which facts must move together?" — a
correct question — without asking "is each of them *mine* to move?". Data
coherence and ownership are different questions and the brief conflated them.

**Why existing controls missed it.** They did not: the gate fired, and its
message explicitly said *"a genuinely new legitimate reader is itself a signal
worth stopping and reporting on — do not silently broaden
`_ALLOWED_READER_FILES`"*. **This entry records a control that worked.**

**Corrective action.** The realignment was removed rather than the allowlist
broadened. The resulting gap — availability corrected, stored plan possibly
still showing the dropped day — is documented in code and pinned by a test that
asserts the plan is *unchanged*.

**Preventive process change.** Agent rule 3 — *never weaken a gate to pass CI*.
Manager practice: a brief that lists facts to update must also state which
domain owns each, so ownership is checked at planning time rather than by CI.

**Validation evidence.** `_ALLOWED_READER_FILES` is unchanged; the guard passes
without exception; production code contains no non-comment reference to the
fact.

**Status.** Implemented. Follow-up capability scoped separately (see
`docs/WAVE1_WORK_PLAN.md`).

---

## PIL-004 · Two stalled agents, all work uncommitted

| | |
|---|---|
| **Date** | 2026-07-27 |
| **Work item** | Taxonomy + W1-8, first attempts |

**Observed failure.** One agent lost its network connection, another stalled.
Both had substantial validated work in the working tree and **zero commits**.

**Immediate impact.** Recoverable only because the manager preserved the
worktrees before touching anything. Had a worktree been reset first, the work
was gone.

**Process root cause.** Briefs described the deliverable as a single commit at
the end, so agents treated committing as a final step rather than a checkpoint.

**Corrective action.** Both worktrees were diffed to a scratch location before
any recovery. The taxonomy work was ~90% complete and was **finished rather
than restarted**; W1-8 had barely begun and restarting was correct.

**Preventive process change.** Agent rule 5 — *commit every independently
passing unit*. Manager recovery procedure: **preserve before recovering** —
diff every affected worktree to a scratch location, then assess
finish-vs-restart per agent rather than uniformly.

**Validation evidence.** The relaunched W1-8 agent produced **two** commits
(implementation, then tests) instead of one, so a later stall would have cost
at most the final increment.

**Status.** Implemented.

---

## PIL-005 · A function that nothing called

| | |
|---|---|
| **Date** | 2026-07-27 |
| **Work item** | W1-8 → wiring branch |

**Observed failure.** `apply_schedule_correction` was implemented, tested, and
had **zero callers**. The user-visible defect reproduced exactly as before.

**Immediate impact.** None — the agent *reported* the gap rather than reaching
outside its file scope, which is the desired behaviour.

**Process root cause.** The manager scoped the task to `health_jobs.py` because
another agent held `assistant.py` concurrently, and did not schedule the
integration as an explicit dependent step. The scope boundary was right; the
missing piece was the follow-on task.

**Why this is a manager failure, not an agent failure.** The agent honoured its
boundary and reported precisely what was needed. The plan had no slot for that
work.

**Corrective action.** A dedicated wiring branch, sequenced *after* the
function merges.

**Preventive process change.** Manager planning rule: when a task's dispatch
site falls outside its file scope, **the integration is created as a dependent
task at planning time**, not discovered at report time. Agent rule 2 — *a
function is not done until it is called* — makes the agent surface it even if
the manager forgets.

**Validation evidence.** The wiring branch exists with a test asserting the
service is invoked from the real handler and that the correction is attempted
**before** any acknowledgement.

**Status.** Implemented.

---

## PIL-006 · The same stray-database defect reached CI twice

| | |
|---|---|
| **Date** | 2026-07-27 |
| **Work item** | PR #36, then PR #49 → root fix in PR #50 |

**Observed failure.** A read against a non-existent database created it, and
`database_path` defaults to the **repo root**. Two unrelated features tripped
the forbidden-files guard: a pain-region lookup while rendering a plan, then a
plan-confirmation gate.

**Immediate impact.** Two red CI runs and two rounds of diagnosis.

**Technical root cause.** sqlite creates a database on connect; ~67 read sites
each had to remember to guard.

**Process root cause.** The first occurrence was fixed **at its call site**.
That treated a systemic property of the data layer as a local bug.

**Why existing controls missed the second one.** It cannot reproduce locally: a
developer with `.env` pointing at `data/` never sees it, and CI has no `.env`.
The environment gap made local testing structurally unable to catch it.

**Corrective action (escalated).** The guard moved into `Database.fetch_one` /
`fetch_all` — the single chokepoint every read passes. Reads against a
non-existent database return `None`/`[]`; **writes are deliberately unguarded**
so a genuine bug is not silently swallowed.

**Preventive process change.** This is the escalation rule in action: first
occurrence → call-site fix; second → a gate at the boundary. Manager practice:
**a defect recurring in a second unrelated feature is by definition systemic**
and must be fixed at the layer, not the site.

**Validation evidence.** Six tests pin it, including one reproducing the exact
CI failure against the default path. Every read path in the product passes
through those two methods, so the full suite is the regression test — it
passes.

**Status.** Implemented.

---

## Manager self-review — WAVE-1 batch 3

**What worked.** Pre-flight inspection before writing a brief reshaped two
tasks from "build X" to "wire the X that already exists", preventing duplicate
implementations of a meal-count persistence chain and a weekday parser.
Worktree isolation eliminated the branch collisions of the previous batch.
Preserving stalled agents' work before recovery saved ~90% of one task.

**What did not.** Briefs named a single acceptance utterance and got
single-utterance coverage (PIL-002). The wiring dependency was left implicit
(PIL-005). A recurring defect was fixed twice at call sites before being fixed
at its layer (PIL-006) — one round of rework that a systemic-recurrence rule
would have avoided.

**Standing changes to manager practice**, now reflected in briefs:
1. Pre-flight every task: locate what already exists, and state it in the brief.
2. Name the required tests **in advance**, including alternative phrasings.
3. Create the integration step as a dependent task whenever the dispatch site
   is out of scope.
4. Treat a second occurrence of any defect class as systemic on sight.
5. Preserve before recovering; decide finish-vs-restart per agent.
