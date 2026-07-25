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
| **Local-only (untracked, outside repo)** at `C:\coach_bot\`: `FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md`, `MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT.md`, `MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT.md`, `SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT.md`, `UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN.md`, `UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT.md` | ARCHIVE_CANDIDATE (untracked, **only copies**) | No git history — **backup before any move**. Proposed: copy into `docs/archive/` (redacted) or the backup store. Risk if deleted: permanent loss. |
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

Reachability tested with `git merge-base --is-ancestor <ref> origin/develop`.

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
"local-only audit documents" the Ledger supersedes (archive candidates, only copies).

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

Secret scan of all six: **0 hits** → archived verbatim (no redaction). Strays:
`C:\coach_bot\noam_coach.db` = 0 bytes; `C:\coach_bot\.git\` = only `info\exclude`
(301 B); `C:\coach_bot\.agents\` = empty.

Ordering: **A → B → C.** Each is a separate reviewed commit/PR; verify after each.

---

### Batch A — Backups + archival copies ONLY (non-destructive, reversible)

**A1. Back up the three stray/local items** (copy, never move):
```bash
BK="/c/coach_bot_BACKUP_20260721_150908/cleanup_20260725"
mkdir -p "$BK/local_docs" "$BK/stray_root_git" "$BK/stray_root_db"
# six local-only audit docs
for f in FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT \
         MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT \
         UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT; do
  cp -p "/c/coach_bot/$f.md" "$BK/local_docs/$f.md"
done
cp -p "/c/coach_bot/.git/info/exclude" "$BK/stray_root_git/exclude"   # the only file in the stray .git
cp -p "/c/coach_bot/noam_coach.db"      "$BK/stray_root_db/noam_coach.db"  # 0-byte
```
**A2. Archive the six docs INTO the repo (copy; originals untouched):**
```bash
for f in FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN MEAL_INTERACTION_ENGINEERING_ROOT_CAUSE_AUDIT \
         MEAL_INTERACTION_LOG_AND_IMAGE_AUDIT SYSTEM_DATA_SOURCE_OF_TRUTH_AND_OBSERVABILITY_AUDIT \
         UNIFIED_NOAM_COACH_CONSOLIDATION_PLAN UNIFIED_NOAM_COACH_AUDIT_CHECKPOINT; do
  cp -p "/c/coach_bot/$f.md" "/c/coach_bot/noam-coach/docs/archive/$f.md"
done
```
- **Preconditions:** the six sha256 above still match; `docs/archive/<name>.md` free (verified — no collision); backup store writable.
- **Verify (byte-equality, both copies):**
```bash
for f in <the six basenames>; do
  a=$(sha256sum "/c/coach_bot/$f.md" | cut -d' ' -f1)
  b=$(sha256sum "$BK/local_docs/$f.md" | cut -d' ' -f1)
  c=$(sha256sum "/c/coach_bot/noam-coach/docs/archive/$f.md" | cut -d' ' -f1)
  [ "$a" = "$b" ] && [ "$a" = "$c" ] && echo "OK $f" || echo "MISMATCH $f"
done
```
- **Expected git diff:** 6 new tracked files under `docs/archive/` (no other change).
  Confirm CI forbidden-files gate stays clean (`git ls-files | grep -E '\.env$|\.db$|/\.claude/'` empty).
- **Rollback:** `git rm docs/archive/<the six>.md && git commit` (originals + backups remain). Backups are copies; deleting them is harmless.
- **Guards/tests:** run `ruff check .` (docs-only, no code) + the forbidden-files grep; open a PR to develop; CI must be green.
- **Planned commit:** `docs(archive): preserve six local-only audit documents (Batch A)`.
- **Untouched confirmation:** originals at `C:\coach_bot\` remain (copy only); protected worktree, historical DB, PII, runtime data, unmerged branches — untouched.

### Batch B — Remove ONLY verified 0-byte / malformed / empty strays

**Only after Batch A backups exist and are byte-verified.** These live OUTSIDE
the repo, so there is no git diff.
```bash
# preconditions: backups exist under $BK; re-verify emptiness right before removal
[ ! -s /c/coach_bot/noam_coach.db ] && echo "confirmed 0-byte" || { echo "ABORT: not empty"; exit 1; }
git -C /c/coach_bot rev-parse 2>/dev/null && { echo "ABORT: real repo"; exit 1; } || echo "confirmed .git not a repo"
[ -z "$(ls -A /c/coach_bot/.agents 2>/dev/null)" ] && echo "confirmed .agents empty" || { echo "ABORT: .agents not empty"; exit 1; }
# removals
rm -f  /c/coach_bot/noam_coach.db
rm -rf /c/coach_bot/.git            # only ./info/exclude, backed up in A1
rmdir  /c/coach_bot/.agents
```
- **Verify:** `ls /c/coach_bot/noam_coach.db /c/coach_bot/.agents 2>&1` → "No such file"; `ls /c/coach_bot/.git 2>&1` → absent. The canonical repo still resolves: `git -C /c/coach_bot/noam-coach rev-parse HEAD`.
- **Expected git diff:** NONE (all three are outside the repo).
- **Rollback:** `touch /c/coach_bot/noam_coach.db`; `mkdir -p /c/coach_bot/.git/info && cp "$BK/stray_root_git/exclude" /c/coach_bot/.git/info/exclude`; `mkdir /c/coach_bot/.agents`.
- **Guards:** re-run PHASE-0 verification (`git -C noam-coach status`, protected-DB hash unchanged).
- **Planned commit:** none (no tracked change) — record the action in `WORK_MANAGER_STATE.md`.
- **Untouched:** the canonical `noam-coach` repo, protected repo/worktrees/DB, PII, runtime data — all untouched.

### Batch C — Reassign `origin/HEAD` + prune ONLY merged branches with 0 unique commits

**Only after re-verifying reachability at execution time.**
```bash
cd /c/coach_bot/noam-coach
git fetch --prune origin
# C1: fix the stale local remote-HEAD symref (GitHub default is already develop)
git remote set-head origin develop
# C2: precondition re-check — prune ONLY branches proven merged with 0 unique commits
for b in consolidation/unified-noam-coach review/meal-observability-batch7 review/workout-selection-architecture; do
  git merge-base --is-ancestor "origin/$b" origin/develop && [ "$(git rev-list --count origin/develop..origin/$b)" = "0" ] \
    && echo "PRUNE-OK origin/$b" || echo "SKIP origin/$b (unique commits or unreachable)"
done
```
- **Scope note:** `feature/dayplan-phase1` and `feature/dayplan-residuals` are merged but **intentionally retained** (prior instruction) — NOT pruned. `review/2026-07-18_1` is the protected-repo's branch and origin/HEAD's old target — retained. The three `codex/*` + `audit/latest-manual-session` branches have **unique unmerged commits** — **excluded** (must not delete; if ever retiring `audit/…`, first preserve its unique doc `2cf3de5` into `docs/archive/`).
- **Actual deletion command (run ONLY for refs that printed PRUNE-OK):**
  `git push origin --delete <branch>` (remote) and `git branch -D <local-if-any>`.
- **Verify:** `git ls-remote origin | grep -E '<pruned branch>'` → empty; `git symbolic-ref refs/remotes/origin/HEAD` → `refs/remotes/origin/develop`.
- **Rollback:** branch tips are recorded here (`consolidation/unified-noam-coach`=`608f60d`; `review/meal-observability-batch7`=`f31f047`; `review/workout-selection-architecture`=`7d256fa`) — recreate with `git push origin <sha>:refs/heads/<branch>`. `origin/HEAD` rollback: `git remote set-head origin <old>` (cosmetic).
- **Guards:** each pruned branch's content is proven reachable from develop (0 unique commits) → no work is lost.
- **Planned commit:** none (ref-only changes) — record in `WORK_MANAGER_STATE.md`.
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
