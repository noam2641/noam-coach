# Repository Cleanup Ledger — `noam-coach`

Cleanup/disposition **source of truth**. Read-only audit; **nothing has been
deleted, moved, archived, or retired.** Execution is gated on the human token
`APPROVE REPOSITORY CLEANUP EXECUTION` and proceeds only in small reversible
batches (see `CLAUDE.md`).

- **Audited:** whole `C:\coach_bot\` tree + registered worktrees + branches.
- **Baseline:** canonical repo @ `origin/develop` = `fbff6f1` (verified 2026-07-25).
- Sensitive files: metadata/size/hash only — contents never printed.

Classifications: `CANONICAL_ACTIVE` · `HISTORICAL_REFERENCE` · `PROTECTED` ·
`GENERATED` · `RUNTIME_DATA` · `PII_SENSITIVE` · `DUPLICATE` · `SUPERSEDED` ·
`ARCHIVE_CANDIDATE` · `SAFE_DELETE_CANDIDATE` · `UNKNOWN`.

### Deferred: observed CI flake (separate hardening task — NOT fixed in cleanup)
During Batch A CI, the **pull_request** context on `b6db987` failed once, then
**passed on re-run (attempt 2, same commit, no changes)** — flaky, not a
regression. Failing test:
`tests/test_daily_menu_refresh.py::test_lease_loss_during_generation_fences_persistence`
— a concurrency/timing case (0.001 s renewal interval + fixed `asyncio.sleep(0.02)`)
around heartbeat/lease-takeover/CAS fencing; scheduling contention is a plausible
non-determinism source. Observed once (CI summary: 1 failed, 2425 passed, 2
skipped). **Deferred as CI-hardening work (make the lease/CAS test deterministic);
NOT addressed during this cleanup** — no production code or test was modified to
obtain green CI. If it recurs, stop and root-cause before continuing.

---

## A. Keep / Canonical (no action)

| Path / Ref | Class | Evidence |
|---|---|---|
| `noam-coach/` (this repo) | CANONICAL_ACTIVE | primary worktree; origin `github.com/noam2641/noam-coach.git` |
| `onboarding.py` (root, 167 L, sha `af20f966`) | CANONICAL_ACTIVE | `import onboarding` by 20+ live modules (`coach_bot.py`, `app/runtime.py`, `bot/*`). **NOT a duplicate** of the package file. |
| `noam_coach/bot/onboarding.py` (3418 L, sha `cdf6d847`) | CANONICAL_ACTIVE | `from noam_coach.bot.onboarding import …` by 60+ modules. Distinct hash/content/callers. |
| `docs/CANONICAL_IMPLEMENTATION_LEDGER.md` | CANONICAL_ACTIVE | self-declared single source of truth for implementation status |
| `docs/SOURCE_COVERAGE_REGISTRY.md` | CANONICAL_ACTIVE | canonical source/gap accounting (G4) |
| `docs/MASTER_TASKS.md` | CANONICAL_ACTIVE (task register) | product task register; **status deferred to the Ledger** |
| `CLAUDE.md`, `docs/WORK_MANAGER_STATE.md`, `docs/WORK_MANAGER_AGENTS.md` | CANONICAL_ACTIVE | Work Manager governance (committed `8ac4bf4`) |
| `reviews/2026-07-18_1/` (7 tracked files) | CANONICAL_ACTIVE | tracked review deliverables |

**No forbidden files are tracked** (`git ls-files | grep -E '\.env$|\.db$|(^|/)\.claude/'` → empty). `scripts/generate_secrets.py` (generator) and `tests/test_mini_tokens.py` (test) contain no secret payloads.

## B. Archive candidates (keep tracked/backed-up; not canonical)

| Path | Class | Evidence / note |
|---|---|---|
| `docs/FINAL_TASKS_58_65_IMPLEMENTATION_REPORT.md` | HISTORICAL_REFERENCE / SUPERSEDED | Ledger: "historical partial report" |
| `docs/MASTER_CORRECTION_BACKLOG_ADDENDUM_38_57.md` | HISTORICAL_REFERENCE / SUPERSEDED | Ledger supersedes the FIX 38–57 addendum |
| `docs/archive/*`, `docs/audits/*`, `docs/reports/*` | HISTORICAL_REFERENCE | point-in-time reports; correctly filed |
| **Local-only (untracked, outside repo)** at `C:\coach_bot\`: `FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md`, `MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT.md`, `MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT.md`, `SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT.md`, `UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN.md`, `UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT.md` | ARCHIVE_CANDIDATE (untracked, **only copies**) | No git history — external backup only. **The extended PII scan (§G) found real user id + meal-image refs in ALL SIX → they are NOT added to Git; backed up externally at `…\cleanup_20260725\local_docs_PII\`.** In-repo archival would need a separately-approved redaction pass. Risk if deleted: permanent loss. |
| `C:\coach_bot_BACKUP_20260721_150908\` (3.4 MB, SHA256 manifest) | ARCHIVE (evidence store) | deliberate dated backup — retain as-is |

## C. Delete candidates (each with evidence; backup first)

| Path | Class | Justification | Risk / blocker |
|---|---|---|---|
| `C:\coach_bot\noam_coach.db` | SAFE_DELETE_CANDIDATE | **0 bytes**, outside any repo, no caller (app uses in-repo `data/noam_coach.db`). The A-2 empty-DB decoy. | Low. Backup then delete; trivially reconstituted. |
| `C:\coach_bot\.git\` | SAFE_DELETE_CANDIDATE (stray) | malformed — contains only `info/`, `rev-parse` → "not a git repository"; shadows nothing (real repo is `noam-coach/.git`) | Low, but **verify** it's not a partially-init parent; back up `info/` first |
| `C:\coach_bot\.agents\` | SAFE_DELETE_CANDIDATE | empty directory | Negligible |

**No source module is a delete candidate** — both `onboarding.py` files have proven live callers.

## D. Protected / PII (metadata only — no move/delete/print)

| Path | Class | Metadata |
|---|---|---|
| `C:\coach_bot\noam-coach-private-audit\` (3.0 MB) | PII_SENSITIVE / PROTECTED | session zip (470 KB) + `private_trace_full.json` (2.13 MB) + 2 meal JPGs + manifests. Real user session. **Outside git. Do not touch.** |
| `C:\coach_bot\noam_coach_complete_release\` (526 MB) | PROTECTED (release repo) | separate repo @ `6d57c04`; historical DB sha `5bd8ac1b…`. Do not touch. |
| `…\noam_coach_complete_release.worktrees\{meal-clarification-batch6, workout-selection-architecture}` | PROTECTED (worktrees) | branches merged/reachable, but **inside the protected repo** → no action without approval |
| `C:\coach_bot\.conda\` | RUNTIME_DATA / GENERATED | conda Python env; superseded by `noam-coach/.venv` (3.12.8). Leave. |
| `C:\coach_bot\.claude\` + `noam-coach/.claude\` | RUNTIME_DATA / PROTECTED-CONFIG | local Claude config; gitignored; never track/commit |
| `noam-coach/{data,logs,storage}/`, `data/noam_coach.db` | RUNTIME_DATA (ignored) | `git check-ignore` confirms ignored; live runtime state |

## E. Branch & worktree cleanup (reachability evidence)

Reachability tested with, e.g., `git merge-base --is-ancestor origin/consolidation/unified-noam-coach origin/develop`
(one such check per branch; the exact guarded loop is in §G Batch C).

| Branch | Reachable? | Unique commits | Class | Proposed action |
|---|---|---|---|---|
| `consolidation/unified-noam-coach` | ✅ | 0 | SUPERSEDED (merged) | prune candidate (local+origin) — **after approval** |
| `feature/dayplan-phase1` | ✅ | 0 | merged (PR #4) | **retained intentionally** (prior instruction) — do not delete |
| `feature/dayplan-residuals` | ✅ | 0 | merged (PR #5) | **retained intentionally** — do not delete |
| `review/2026-07-18_1` (local behind 15; = local origin/HEAD) | ✅ | 0 | HISTORICAL (protected-repo branch) | keep; reassign `origin/HEAD`→develop before any origin prune |
| `origin/review/meal-observability-batch7` | ✅ | 0 | SUPERSEDED | prune candidate — after approval |
| `origin/review/workout-selection-architecture` | ✅ | 0 | SUPERSEDED | prune candidate — after approval |
| `origin/audit/latest-manual-session-2026-07-18` | ❌ | **2** (last `e5a0428`; includes unique doc `2cf3de5`) | UNMERGED — unique doc | **DO NOT DELETE**; if retiring, first preserve `2cf3de5` doc into `docs/archive/` |
| `origin/codex/complete-rec-program-04` | ❌ | **2** (`10a97dc` PR#1 merge + `bf12455`) | UNMERGED (content = `.claude` CI-ignore, equivalent to develop) | **DO NOT DELETE** without a merge/close decision |
| `origin/codex/post-observability-architecture` | ❌ | **1** (`bf12455` `.claude` fix) | UNMERGED (content-equivalent) | **DO NOT DELETE** without decision |
| `chore/repository-consolidation-audit` (current) | local, no upstream | — | CANONICAL_ACTIVE (this audit) | keep |

**Zero-risk source-of-truth fix:** local `origin/HEAD` symref is stale
(`review/2026-07-18_1`); GitHub default is `develop` (verified). Fix (local ref
only): `git remote set-head origin develop`.

## F. Document reconciliation

Canonical hierarchy (tracked): **1)** `CANONICAL_IMPLEMENTATION_LEDGER.md`
(implementation status, supersedes all others) · **2)** `SOURCE_COVERAGE_REGISTRY.md`
(source/gap accounting) · **3)** `MASTER_TASKS.md` (task register; status deferred to
the Ledger). Superseded-but-kept: `FINAL_TASKS_58_65…`, `MASTER_CORRECTION_BACKLOG_ADDENDUM_38_57`,
`docs/archive|audits|reports/*`. Local-only `C:\coach_bot` audit docs = the
"local-only audit documents" the Ledger supersedes — **contain PII (real user id +
meal-image refs); kept external-backup-only, NOT in Git** (see §G Batch A status).

---

## G. Execution plan — three reversible batches (NOT YET EXECUTED)

**Deterministic backup destination (single, fixed):**
`C:\coach_bot_BACKUP_20260721_150908\cleanup_20260725\`
(the existing dated backup store; new subfolder for this cleanup). No source code
is retired here — **P2.7/P2.8 decoupling stay future implementation work.**

Fixed inputs (verified 2026-07-25, sha256 truncated to 16):
| Local-only doc (`C:\coach_bot\`) | size | sha256 (16) |
|---|---:|---|
| FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md | 37565 | `c2d6ca3dee9a9f10` |
| MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT.md | 26842 | `59760e196b670f2e` |
| MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT.md | 15588 | `79cd7354bd78619d` |
| SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT.md | 74391 | `aad2590110d16891` |
| UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN.md | 82977 | `855e35756439afff` |
| UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT.md | 81762 | `6a74ea4ec7206310` |

**Secret/PII scan** (exact command, run again immediately before Batch A):
```bash
cd /c/coach_bot
for f in FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT.md \
         MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT.md SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT.md \
         UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN.md UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT.md; do
  hits=$(grep -icE "sk-[a-z0-9]{20}|bearer [a-z0-9]{20}|[0-9]{9,10}:[A-Za-z0-9_-]{35}|password *=|token *= *['\"][A-Za-z0-9]{20}" "$f")
  echo "$f: $hits"
done
```
Result (narrow tokens-only scan, 2026-07-25): all six = 0 token/secret hits.

**⚠️ EXTENDED PII scan at execution time (constraint 2 — tokens, PII, real user/
Health data, production logs, meal-image refs, usernames, emails, phones,
user-specific paths): ALL SIX DOCUMENTS CONTAIN PII.** Verified without printing
content: a real production Telegram **user id (`<REDACTED_USER_ID>`)**, real **meal-image /
approval-id references** (`storage/food/<id>_*.jpg`, `<id>_in_<hash>`,
`<REDACTED_APPROVAL_ID>`), and/or private-audit paths (`noam-coach-private-audit`,
`private_trace`, `C:\Users\user`) appear across all six. (The one "email" flag is
a benign `@users.noreply.github.com`.)

**Batch A revised per constraint 2 → executed as EXTERNAL-BACKUP-ONLY:** the six
docs were copied to `C:\coach_bot_BACKUP_20260721_150908\cleanup_20260725\
local_docs_PII\` (byte-verified) and **NOT added to Git.** No `docs/archive/`
copies were created. This closes the forbidden-content guard: the only copies are
preserved externally, out of version control. Any future in-repo archival would
require a separately-approved redaction pass. Strays:
`C:\coach_bot\noam_coach.db` = 0 bytes; `C:\coach_bot\.git\` = only `info\exclude`
(301 B, sha256 `584f2cca6096463716b1370b772a34dbbc10e2743d0039a6848eea9b98ad06ef`);
`C:\coach_bot\.agents\` = empty.

Ordering: **A → B → C.** Each is a **separate logical commit on PR #6**; after each
batch update the ledgers + `WORK_MANAGER_STATE.md`, push, and wait for CI; pause on
any failed guard. Do not create or merge another PR unless explicitly instructed.
`BK` is redefined at the start of every batch (never relied on across sessions).

---

### Batch A — Backups + archival copies ONLY (non-destructive, reversible)

> **STATUS 2026-07-25: EXECUTED as EXTERNAL-BACKUP-ONLY (A2 archival SKIPPED).**
> The extended PII scan (constraint 2) found the real production user id +
> meal-image/approval references in **all six** docs. Per constraint 2, they were
> backed up externally (byte-verified) and **NOT added to Git**. A2 (copy into
> `docs/archive/`) was correctly **not performed**. See the PII note above.

**A0. Re-run the secret/PII scan above; abort if any file > 0 hits.**

**A1. Back up the local docs + strays** (copy, never move):
```bash
BK="/c/coach_bot_BACKUP_20260721_150908/cleanup_20260725"   # (re)defined here
mkdir -p "$BK/local_docs" "$BK/stray_root_git" "$BK/stray_root_db"
for f in FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT.md \
         MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT.md SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT.md \
         UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN.md UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT.md; do
  cp -p "/c/coach_bot/$f" "$BK/local_docs/$f"
done
cp -p "/c/coach_bot/.git/info/exclude" "$BK/stray_root_git/exclude"
cp -p "/c/coach_bot/noam_coach.db"      "$BK/stray_root_db/noam_coach.db"
```
**A2. Archive the six docs INTO the repo (copy; originals untouched):**
```bash
for f in FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT.md \
         MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT.md SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT.md \
         UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN.md UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT.md; do
  cp -p "/c/coach_bot/$f" "/c/coach_bot/noam-coach/docs/archive/$f"
done
```
- **Preconditions:** secret scan = 0; the six sha256 (§G table) still match; each
  each of the six `docs/archive/<basename>.md` targets free (verified 2026-07-25,
  no collision); backup store writable.
- **Verify (byte-equality, all three copies):**
```bash
BK="/c/coach_bot_BACKUP_20260721_150908/cleanup_20260725"
for f in FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT.md \
         MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT.md SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT.md \
         UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN.md UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT.md; do
  a=$(sha256sum "/c/coach_bot/$f" | cut -d' ' -f1)
  b=$(sha256sum "$BK/local_docs/$f" | cut -d' ' -f1)
  c=$(sha256sum "/c/coach_bot/noam-coach/docs/archive/$f" | cut -d' ' -f1)
  { [ "$a" = "$b" ] && [ "$a" = "$c" ]; } && echo "OK $f" || echo "MISMATCH $f"
done
```
- **Expected git diff:** exactly 6 new tracked files under `docs/archive/`:
  `FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md`, `MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT.md`,
  `MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT.md`, `SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT.md`,
  `UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN.md`, `UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT.md`. No other change.
  Forbidden-files gate stays clean: `git ls-files | grep -E '\.env$|\.db$|/\.claude/'` → empty.
- **Rollback:**
```bash
cd /c/coach_bot/noam-coach
git rm docs/archive/FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md \
       docs/archive/MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT.md \
       docs/archive/MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT.md \
       docs/archive/SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT.md \
       docs/archive/UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN.md \
       docs/archive/UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT.md
git commit -m "revert(archive): remove Batch A archival copies"
```
  (originals at `C:\coach_bot\` and the `$BK` backups remain.)
- **Guards/tests:** `ruff check .` (docs-only) + forbidden-files grep. Commit on PR #6, push, wait for BOTH CI contexts green.
- **Planned commit:** `docs(archive): preserve six local-only audit documents (Batch A)`.
- **Untouched:** originals (copy only); protected worktree, historical DB, PII, runtime data, unmerged branches.

### Batch B — Remove ONLY the verified 0-byte / malformed / empty strays

> **STATUS 2026-07-25: EXECUTED @ `52d6a10`** (both CI contexts green first). The
> stray backups (deferred in Batch A when the PII guard tripped) were created and
> hash-verified — `noam_coach.db` sha `e3b0c442…b855` (canonical empty-file hash),
> `.git/info/exclude` sha `584f2cca…06ef`. All three targets revalidated inline
> (0-byte db · `.git` = exactly `info/exclude`, no HEAD/objects/refs · empty
> `.agents`), then removed via the fail-closed procedure below (rm exclude → rmdir
> info → rmdir .git; no `rm -rf`). Verified absent; canonical repo still resolves
> `52d6a10`; protected worktree/DB, PII, runtime data untouched. **No git diff**
> (all outside the repo). Rollback available from the recorded backups.

**Only after Batch A backups exist and are byte-verified.** These live OUTSIDE the
repo → no git diff. The `.git` removal is **fail-closed** (aborts on any surprise).
```bash
BK="/c/coach_bot_BACKUP_20260721_150908/cleanup_20260725"   # (re)defined here
# --- B1: 0-byte stray DB ---
[ -f /c/coach_bot/noam_coach.db ] && [ ! -s /c/coach_bot/noam_coach.db ] \
  && echo "confirmed 0-byte db" || { echo "ABORT: db missing or not empty"; exit 1; }
[ -f "$BK/stray_root_db/noam_coach.db" ] || { echo "ABORT: backup missing"; exit 1; }
rm -f /c/coach_bot/noam_coach.db

# --- B2: malformed .git — FAIL-CLOSED ---
GITDIR=/c/coach_bot/.git
[ -d "$GITDIR" ] || { echo "ABORT: $GITDIR missing"; exit 1; }
git -C /c/coach_bot rev-parse 2>/dev/null && { echo "ABORT: real repo"; exit 1; } || true
# complete inventory must be EXACTLY: info/ (dir) and info/exclude (file), nothing else
INV=$(cd "$GITDIR" && find . -mindepth 1 -printf '%y %p\n' | sort)
EXPECTED=$'d ./info\nf ./info/exclude'
[ "$INV" = "$EXPECTED" ] || { echo "ABORT: unexpected .git contents:"; echo "$INV"; exit 1; }
# exclude must match its recorded full sha256
EXPECT_SHA=584f2cca6096463716b1370b772a34dbbc10e2743d0039a6848eea9b98ad06ef
GOT_SHA=$(sha256sum "$GITDIR/info/exclude" | cut -d' ' -f1)
[ "$GOT_SHA" = "$EXPECT_SHA" ] || { echo "ABORT: exclude sha mismatch ($GOT_SHA)"; exit 1; }
[ -f "$BK/stray_root_git/exclude" ] || { echo "ABORT: exclude backup missing"; exit 1; }
# remove ONLY info/exclude, then rmdir info, then rmdir .git (no rm -rf)
rm -f "$GITDIR/info/exclude"
rmdir "$GITDIR/info"
rmdir "$GITDIR"

# --- B3: empty .agents ---
[ -d /c/coach_bot/.agents ] && [ -z "$(ls -A /c/coach_bot/.agents)" ] \
  && rmdir /c/coach_bot/.agents || { echo "ABORT: .agents missing or not empty"; exit 1; }
```
- **Verify:** `ls /c/coach_bot/noam_coach.db /c/coach_bot/.git /c/coach_bot/.agents 2>&1` → all "No such file"; canonical repo still resolves `git -C /c/coach_bot/noam-coach rev-parse HEAD`; protected-DB hash unchanged (`sha256sum .../noam_coach_complete_release/noam_coach.db` = `5bd8ac1b…`).
- **Expected git diff:** NONE (all outside the repo).
- **Rollback:**
```bash
BK="/c/coach_bot_BACKUP_20260721_150908/cleanup_20260725"
: > /c/coach_bot/noam_coach.db
mkdir -p /c/coach_bot/.git/info && cp -p "$BK/stray_root_git/exclude" /c/coach_bot/.git/info/exclude
mkdir -p /c/coach_bot/.agents
```
- **Planned commit:** none (no tracked change) — record the action in `WORK_MANAGER_STATE.md`, push that doc update, wait for CI.
- **Untouched:** the canonical repo, protected repo/worktrees/DB, PII, runtime data.

### Batch C — Reassign `origin/HEAD` + prune ONLY merged branches with 0 unique commits

Directly-executable guarded loop; a branch is deleted **only inside** the condition
proving it is an ancestor of `origin/develop` with zero unique commits. Uses
`git branch -d` (safe) and skips local deletion when the branch is absent or
checked out.
```bash
cd /c/coach_bot/noam-coach
git fetch --prune origin
# C1: fix the stale local remote-HEAD symref (GitHub default is already develop)
git remote set-head origin develop
git symbolic-ref refs/remotes/origin/HEAD   # expect refs/remotes/origin/develop

# C2: guarded prune — remote delete + safe local delete, only when proven merged & 0-ahead
CURRENT=$(git rev-parse --abbrev-ref HEAD)
for b in consolidation/unified-noam-coach review/meal-observability-batch7 review/workout-selection-architecture; do
  if git merge-base --is-ancestor "origin/$b" origin/develop \
     && [ "$(git rev-list --count origin/develop..origin/$b)" = "0" ]; then
    echo "PRUNE $b"
    git push origin --delete "$b"
    # local delete only if it exists AND is not the checked-out branch; -d refuses unmerged
    if git show-ref --verify --quiet "refs/heads/$b" && [ "$b" != "$CURRENT" ]; then
      git branch -d "$b" || echo "  (kept local $b: git -d refused / unmerged)"
    else
      echo "  (no local $b or it is checked out — skipped local delete)"
    fi
  else
    echo "SKIP $b (unique commits or unreachable) — NOT deleted"
  fi
done
```
- **Scope note:** `feature/dayplan-phase1`, `feature/dayplan-residuals` = merged but **intentionally retained** (NOT in the loop). `review/2026-07-18_1` = protected-repo branch / old origin/HEAD target — retained. `origin/codex/complete-rec-program-04`, `origin/codex/post-observability-architecture`, `origin/audit/latest-manual-session-2026-07-18` = **unique unmerged commits → excluded** (not in the loop; if ever retiring `audit/…`, first preserve its unique doc `2cf3de5` into `docs/archive/`).
- **Verify:** `git ls-remote origin | grep -E 'consolidation/unified-noam-coach|review/meal-observability-batch7|review/workout-selection-architecture'` → empty; `git symbolic-ref refs/remotes/origin/HEAD` → `refs/remotes/origin/develop`.
- **Rollback (full SHAs):**
```bash
git push origin 608f60db2a7d796fec65141533c389ed78b9c36b:refs/heads/consolidation/unified-noam-coach
git push origin f31f047d74dd68daf8368639dca3fb1ab9a17283:refs/heads/review/meal-observability-batch7
git push origin 7d256fa8c440a08743d2b6282ce515e7f6d8266e:refs/heads/review/workout-selection-architecture
# origin/HEAD (cosmetic): git remote set-head origin review/2026-07-18_1
```
- **Guards:** each pruned branch is proven reachable from develop (0 unique commits) → no work lost.
- **Planned commit:** none (ref-only) — record in `WORK_MANAGER_STATE.md`, push that doc update, wait for CI.
- **Untouched:** all file content, protected assets, PII, unmerged branches.

---

## Approval status

**Nothing above (sections C, E, G) has been executed.** No deletion, move,
archive-copy, branch-prune, worktree-prune, or source retirement will occur until
the human replies exactly:

```
APPROVE REPOSITORY CLEANUP EXECUTION
```

Source retirement (P2.7 / P2.8 `coach_bot`/`runtime_bound` decoupling) is **out of
scope for this cleanup** and remains future implementation work.

*Last updated 2026-07-25 · audit branch `chore/repository-consolidation-audit`.*
