# Export Integrity — Session Audit 2026-07-18

## Read-only and no-mutation guarantees

- The production database was accessed **read-only** for this task:
  every access was a SELECT (via the existing `trace_inspect` tooling and
  the review-window query layer). No INSERT/UPDATE/DELETE, no migration,
  no vacuum, no schema change was executed by the audit.
- No production code was modified; no bot state was changed; the bot was
  not restarted; nothing was fetched from Telegram.
- The pre-existing unrelated uncommitted working-tree changes (18 paths)
  were left untouched and are not part of the audit commit.

## Private/public separation

- The complete private evidence package (full JSON trace, human timeline,
  session metadata, private image manifest, and the recovered original
  meal photos) was written **outside** the repository working tree and is
  not tracked, not staged, and not pushed. No `.gitignore` exception was
  added and `git add -f` was not used.
- Private ZIP contents: 5 documents + 2 recovered images
  (7 files total).
- Images referenced by the session: **3** · confidently recovered: **2**
  (both verified byte-for-byte by SHA-256 after copying) · unresolved:
  **1** (documented in `IMAGE_REFERENCE_MANIFEST.md`, finding F-A9).
- Private ZIP SHA-256:
  `ca984d6a769e505bfaeb0d374a16295223e61e966555db54a574097e14956b68`

## Committed sanitized files (exact list)

- `docs/session_audits/2026-07-18/SANITIZED_SESSION_AUDIT.md`
- `docs/session_audits/2026-07-18/SANITIZED_EVENT_INDEX.json`
- `docs/session_audits/2026-07-18/IMAGE_REFERENCE_MANIFEST.md`
- `docs/session_audits/2026-07-18/EXPORT_INTEGRITY.md`

## Sanitization checks performed

1. All Telegram user/chat/message ids, trace/interaction/span ids,
   AI-call ids, media ids, approval ids and meal ids replaced by stable
   aliases (USER_1 / CHAT_1 / MSG_nnn / TRACE_nnn / INTERACTION_nnn /
   AI_CALL_nnn / MEDIA_nnn / MEAL_nnn) — consistently, so causal
   reconstruction still works.
2. The event index carries only allowlisted, bounded properties (event
   names, routing handler/action/reason, AI purpose/duration/error class,
   flow name+step, delivery operation, media kind/byte size); **no**
   `content` payloads, no free text, no before/after fact values.
3. Content hashes truncated to 12 hex chars (identity continuity without
   retrievability); the only full hash published is of the private ZIP
   itself, whose content is not distributed.
4. Grep sweep over the four staged files for: provider secret-key shapes,
   authorization-header shapes, bot-token shapes, base64 data-URL
   prefixes, the real user id, raw correlation identifiers (trace /
   interaction / span / render / AI-call / media prefixes), raw approval
   ids, provider file ids, absolute local paths, dot-env values, email
   addresses and phone-number patterns — zero matches (the sweep itself
   caught and eliminated an approval-id leak in meal-flow step fields
   before commit).
5. Health measurements and personal free text excluded; the one
   medication report is described with the name redacted; body metrics
   viewed during the session are not reproduced anywhere in the packet.
6. `SANITIZED_EVENT_INDEX.json` parses as valid JSON (verified
   programmatically) and contains 905 events, matching the audited
   window exactly.
7. `git status` verified before commit: no database, WAL/SHM, dot-env,
   log, ZIP, image, storage or export artifact staged; only the four
   files above.
