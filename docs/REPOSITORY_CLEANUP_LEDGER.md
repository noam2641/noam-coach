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

## Approval status

**Everything in sections C and E is a *candidate* only.** No deletion, move,
archive, branch-prune, worktree-prune, or source retirement will occur until the
human replies exactly:

```
APPROVE REPOSITORY CLEANUP EXECUTION
```

*Last updated 2026-07-25 · audit branch `chore/repository-consolidation-audit`.*
