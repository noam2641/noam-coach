# Image Reference Manifest — Session 2026-07-18

Three photos were sent during the session. No image bytes, provider file
ids, or local paths appear here; content hashes are truncated to 12 hex
characters (sufficient for continuity, useless for retrieval). The
recovered originals live only in the local private package.

| Media alias | Interaction | Trace | Event time (UTC) | Recovered locally | Matching method | Confidence | Size | Related events | Visual review needed |
|---|---|---|---|---|---|---|---|---|---|
| MEDIA_001 | INTERACTION_077 | TRACE_008 | 06:22:xx | ✅ yes | exact SHA-256 match against a storage file **and** the persisted meal record's own image path (MEAL_001) | high | ~106 KB | AI image analysis → identity correction → reanalysis (F-A2) → approved as MEAL_001 | Yes — needed to judge the plausible schnitzel portion for F-A2 |
| MEDIA_002 | INTERACTION_102 | TRACE_011 | 06:56:xx | ✅ yes | exact SHA-256 match against a storage file written seconds after the event | high | ~171 KB | AI image analysis (a packaged protein drink, MEAL_002); flow suspended by the next photo; never approved (F-A5) | Optional — label analysis looked correct |
| MEDIA_003 | INTERACTION_103 | TRACE_012 | 06:58:xx | ❌ no | — | — | ~141 KB (from event metadata) | AI image analysis → identity correction (MEAL_003) → rejected on the stale card → 11 silent approve presses (F-A1) | Cannot be performed — see F-A9 |

Unresolved detail for MEDIA_003: the trace records its full content hash
and byte size, but no file with that hash exists anywhere under the
configured storage tree (verified by a full recursive hash scan), no meal
record references it (the meal was never saved), and the media event
carries no provider file reference that would allow a safe re-fetch.
Recorded as unresolved rather than guessed; tracked as finding F-A9.
