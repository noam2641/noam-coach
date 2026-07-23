# Source-Coverage & Open-Gap Registry

Canonical, deduplicated registry of every historical source identifier and open
gap for the Noam Coach consolidation. Published at Gate G4 of the unified
consolidation. Supersedes the accounting in the audit plan's §9.3 / §11 by
separating the three quantities that were previously conflated (DEFECT-1) and
correcting the severity tally (DEFECT-2).

- **Baseline:** `b5b32e8` (canonical) — every count below is re-derived from that
  commit, not from a prior report.
- **Verification meaning:** *IMPLEMENTED_AND_VERIFIED* = production code exists and
  named tests assert the requirement, and the full suite is green at the current
  consolidation HEAD (G3: 2371 collected, 0 fail; G5: 2377 collected, 0 fail — the
  delta is the G5 guard tests).

---

## 1. Three distinct quantities (DEFECT-1 correction)

Enumeration at `b5b32e8`:

```
git grep -ohE '\b(FIX|TASK|ARCH|BATCH)[-_ ]?[0-9]+(\.[0-9]+)?' b5b32e8 -- docs/ tasks/
```

| Quantity | Value | Definition |
|---|---:|---|
| **Raw discovered tokens** | **116** | Distinct ID tokens exactly as written (FIX 50 · TASK 53 · ARCH 13 · **BATCH 0**). |
| **Unique deduplicated source IDs** | **93** | After collapsing separators + zero-padding (`TASK-01`≡`TASK-1`≡`TASK_1`≡`TASK 1`): FIX 50 · TASK 30 · ARCH 13 · BATCH 0. |
| **Overlapping evidence-group rows** | **137** | The per-group rows in §2, which re-list an ID under every group whose evidence touches it (superseded FIX↔Task, ARCH↔Observability, non-token batch/cluster/deferred groups). A *coverage-tracing* number, **not** a count of distinct IDs. |

**`BATCH` yields zero source tokens.** Meal/Workout "batch IDs" are **inferred
code/test evidence** (test filenames + commit messages), not mapped source
identifiers, and are excluded from the 93.

**Reconciliation:** 116 raw ⊇ 93 unique ⊆ 137 overlapping rows. The 137→93 gap is
the 28 superseded FIX↔Task IDs, the ARCH↔Observability overlap, and the non-token
Groups 5–10.

---

## 2. Coverage by group

| Group | Rows | Kind | Status summary |
|---|---:|---|---|
| 1. Tasks 1–22 | 22 | mapped source IDs | IMPLEMENTED_AND_VERIFIED |
| 2. Tasks 58–65 | 8 | mapped source IDs | IMPLEMENTED_NOT_FULLY_VERIFIED (MASTER_TASKS still lists as backlog; code+tests exist) |
| 3. FIX 1–57 (present: 50) | 50 | mapped source IDs (28 ≡ Groups 1–2, superseded) | 28 SUPERSEDED + 20 IMPLEMENTED_AND_VERIFIED |
| 4. ARCH (13) | 13 | mapped source IDs | 12 IMPLEMENTED_AND_VERIFIED + 1 (ARCH-07B) PLANNED_NOT_IMPLEMENTED |
| 5. Meal batches 6/6.1/7/8 | 4 | **inferred (0 source tokens)** | IMPLEMENTED_NOT_FULLY_VERIFIED |
| 6. Workout batches 1–8 | 8 | **inferred (0 source tokens)** | mostly IMPLEMENTED_NOT_FULLY_VERIFIED; Batch 2 (identity) PARTIALLY_IMPLEMENTED — storage only, no read path |
| 7. Observability O1–O10 + Review R1–R6 | 16 | inferred (test-file; overlaps ARCH) | IMPLEMENTED_AND_VERIFIED (R6 carries the single conditional skip) |
| 8. Deferred/external | 5 | narrative | BLOCKED (4) / PLANNED_NOT_IMPLEMENTED (1) |
| 9. Archived/superseded | 6 | narrative (map back to Tasks) | SUPERSEDED with proof |
| 10. Local clusters U-C1…U-C5 | 5 | narrative (§5 of plan) | PARTIALLY_IMPLEMENTED → recovered in G2 |
| **Overlapping rows** | **137** | — | **UNKNOWN = 0** |

Unique source IDs mapped: **93** (FIX 50 + TASK 30 + ARCH 13). Every one mapped /
superseded-with-proof / deferred-with-rationale. **UNKNOWN_NEEDS_EVIDENCE = 0.**

Full per-ID evidence (code module + test file + status) lives in the audit plan
§9.2; this registry is the corrected accounting layer over it.

---

## 3. Open-gap register (DEFECT-2 correction)

Severity tally derived from the register's Sev / "Blocks deploy?" / "Blocks
local?" columns (all 26 rows):

| Severity | Count | Deploy blockers | Local blockers |
|---|---:|---:|---:|
| CRITICAL | 5 | 5 | 2 |
| HIGH | 10 | 6 | 4 |
| MEDIUM | 9 | 1 | 2 |
| LOW | 2 | 0 | 0 |
| **Total** | **26** | **12** | **8** |

The Revision-2 note ("CRITICAL 4 / HIGH 8 / MEDIUM 9 / LOW 5; local 5") was wrong
on four cells; the grand total 26 and deploy-blocker 12 were correct.

**CRITICAL rows (5):** A-1, A-2, B-1, B-2, C-D1.

**A-2 ↔ C-D1 share one root cause — the empty-DB trap — but are counted
separately:** C-D1 is the *code path* (`config.py` relative `./noam_coach.db`
default that silently creates an empty DB); A-2 is its *physical residue* (0-byte
DB decoy files). Distinct remediations: C-D1 fixed in code (G5 startup guard
`assert_safe_database_path`), A-2 decoy files deleted physically at G10. Merging
them would hide one of the two required actions.

**Local blockers (8):** A-2, C-D1, C-M14, C-D2, C-D3, C-I1, C-E3, C-I2.

### Post-G5 disposition

| Gap | Status after G5 |
|---|---|
| C-D1 (relative-DB empty-DB trap) | **RESOLVED** — startup DB-path guard rejects empty/relative/unresolved paths |
| C-I1 / C-I2 (Python 3.14 vs 3.12; two interpreters) | **RESOLVED** — canonical `.venv` on Python 3.12.8 |
| C-D2 / C-E3 (`.env` paths point at old folder / only one folder has it) | **ADDRESSED** — canonical absolute-path `.env` in the new folder |
| A-1 / A-3 (local clusters + tests uncommitted) | **RESOLVED** in G2 |
| B-1 / B-2 (product-behaviour: duration→planner; free-text correction dropped) | OPEN — later gate |
| A-2 (physical 0-byte DB decoys) | OPEN — physical deletion deferred to G10 |
| C-M14 (Migration 14 pending on old DB) | N/A for cutover — the fresh G5 DB already has migration 14; old DB is historical backup only |

---

*Generated at Gate G4. Corrects DEFECT-1 (source-ID count conflation) and DEFECT-2
(CRITICAL/blocker tally) of the consolidation audit. No production code or runtime
data is affected by this document.*
