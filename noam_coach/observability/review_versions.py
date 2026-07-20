"""Version identity of the continuous-improvement review workflow (R4).

Every review package records the exact versions that produced it, so a
package built last month remains interpretable after the workflow evolves
and a rebuild can be compared honestly against the original.

Bump rules:
- PACKAGE_SCHEMA_VERSION — the package directory layout or manifest
  contract changes incompatibly.
- REDACTION_POLICY_VERSION — what the export policy redacts, pseudonymizes
  or refuses changes.
- REVIEW_PROTOCOL_VERSION — the Claude review protocol document
  (perspectives, evidence rules, output contract) changes materially.
- FINDINGS_SCHEMA_VERSION — the canonical findings.json contract changes.
(DETECTOR_SET_VERSION lives with the detectors in ``session_review``.)
"""

from __future__ import annotations

PACKAGE_SCHEMA_VERSION = "1.0"
REDACTION_POLICY_VERSION = "1.0"
REVIEW_PROTOCOL_VERSION = "1.0"
FINDINGS_SCHEMA_VERSION = "1.0"
