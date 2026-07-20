"""Self-contained review-package builder (Review batch R4).

One read-only pass over the canonical stream produces an immutable
evidence bundle under ``reviews/<review_id>/``:

- ``manifest.json``  — identity, versions, requested + resolved selection,
  source commit, artifact hashes. Committable.
- ``events.jsonl``   — the window's raw (write-time-redacted) events, one
  per line, canonical append order. GITIGNORED raw evidence.
- ``timeline.md``    — the deterministic human timeline (existing O9
  renderer). GITIGNORED raw evidence (contains conversation text).
- ``stats.json``     — counts/durations only. Committable.
- ``signals.json``   — R3 detector output (ids + digests). Committable.
- ``signals.md``     — human summary of the signals. Committable.

Guarantees: no AI call, no production-data mutation, no cursor movement;
atomic promotion (built in a hidden staging dir, ``os.replace``d into
place only when complete); committable artifacts fail closed when they
would carry anything the canonical redactor recognizes as sensitive; the
selected user appears in committable artifacts only as a pseudonym; a
completed package's evidence artifacts are never silently rewritten — a
rebuild mints a new review id linked via ``rebuilt_from``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_log import ProductEvent
from noam_coach.observability.redaction import redact
from noam_coach.observability.review_versions import (
    PACKAGE_SCHEMA_VERSION,
    REDACTION_POLICY_VERSION,
    REVIEW_PROTOCOL_VERSION,
)
from noam_coach.observability.review_window import (
    RequestedSelection,
    ResolvedSelection,
    iter_window_events,
)
from noam_coach.observability.session_review import DETECTOR_SET_VERSION, run_detectors
from noam_coach.observability.session_trace import build_session_trace, render_timeline

MANIFEST_NAME = "manifest.json"
EVENTS_NAME = "events.jsonl"
TIMELINE_NAME = "timeline.md"
STATS_NAME = "stats.json"
SIGNALS_NAME = "signals.json"
SIGNALS_MD_NAME = "signals.md"

# Evidence artifacts are immutable once the package is complete; their
# hashes anchor findings to exactly this evidence.
HASHED_ARTIFACTS = (EVENTS_NAME, TIMELINE_NAME, STATS_NAME, SIGNALS_NAME)

# Raw evidence that must never be committed (documented in .gitignore).
GITIGNORED_ARTIFACTS = (EVENTS_NAME, TIMELINE_NAME)


class PackageError(RuntimeError):
    pass


class SensitiveExportError(PackageError):
    """A committable artifact would have carried sensitive content."""


@dataclass(frozen=True)
class PackageBuildResult:
    review_id: str
    path: Path
    manifest: dict[str, Any]
    event_count: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def user_pseudonym(user_id: int) -> str:
    return "u_" + hashlib.sha256(str(user_id).encode()).hexdigest()[:8]


def _git_identity(repo_root: Path) -> dict[str, Any]:
    def _run(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=repo_root, capture_output=True, text=True, timeout=20,
            )
            return proc.stdout.strip() if proc.returncode == 0 else None
        except Exception:  # noqa: BLE001 — package identity degrades explicitly.
            return None

    sha = _run("rev-parse", "HEAD")
    status = _run("status", "--porcelain")
    return {
        "source_commit": sha or "unknown",
        "working_tree_dirty": bool(status) if status is not None else None,
    }


def _assert_committable_clean(name: str, payload: Any) -> None:
    """Fail closed: a committable artifact must be a FIXED POINT of the
    canonical redactor (nothing in it is recognizable as sensitive) and
    must not carry raw user identity or content blocks."""
    if redact(payload) != payload:
        raise SensitiveExportError(
            f"{name}: content the canonical redactor would alter cannot be exported "
            "in a committable artifact"
        )
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in ('"user_id"', '"content"'):
        if forbidden in serialized:
            raise SensitiveExportError(
                f"{name}: committable artifacts must not carry {forbidden} fields"
            )


# ---------------------------------------------------------------------------
# Statistics (counts and durations only — committable by construction)
# ---------------------------------------------------------------------------


def _family(event_name: str) -> str:
    return event_name.split(".", 1)[0] if "." in event_name else "legacy_domain"


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def build_stats(events: list[ProductEvent], trace: Any) -> dict[str, Any]:
    by_family: dict[str, int] = {}
    for event in events:
        by_family[_family(event.event)] = by_family.get(_family(event.event), 0) + 1

    interactions_by_kind: dict[str, int] = {}
    interactions_by_surface: dict[str, int] = {}
    ai_by_purpose: dict[str, dict[str, Any]] = {}
    ai_durations: dict[str, list[int]] = {}
    renders = {"prepared": 0, "delivered": 0, "not_modified": 0, "failed_terminal": 0, "unknown": 0}
    ai_totals = {"calls": 0, "failed": 0, "unresolved": 0}

    for interaction in trace.interactions:
        received = interaction.received
        if received is not None:
            kind = received.properties.get("kind") or "unknown"
            interactions_by_kind[kind] = interactions_by_kind.get(kind, 0) + 1
            surface = received.surface or "unknown"
            interactions_by_surface[surface] = interactions_by_surface.get(surface, 0) + 1
        for ai in interaction.ai_calls:
            purpose = ai.purpose or "unknown"
            entry = ai_by_purpose.setdefault(purpose, {"count": 0, "failed": 0})
            entry["count"] += 1
            ai_totals["calls"] += 1
            if ai.failed is not None:
                entry["failed"] += 1
                ai_totals["failed"] += 1
            elif ai.completed is None:
                ai_totals["unresolved"] += 1
            duration = ai.duration_ms
            if isinstance(duration, int):
                ai_durations.setdefault(purpose, []).append(duration)
        for render in interaction.renders:
            renders["prepared"] += 1
            result = render.delivery_result
            if result == "delivered":
                renders["delivered"] += 1
            elif result == "not_modified":
                renders["not_modified"] += 1
            elif result == "failed":
                renders["failed_terminal"] += 1
            else:
                renders["unknown"] += 1

    for purpose, durations in ai_durations.items():
        ai_by_purpose[purpose]["p50_ms"] = _percentile(durations, 0.50)
        ai_by_purpose[purpose]["p95_ms"] = _percentile(durations, 0.95)

    flow_counts = {
        "started": sum(1 for e in events if e.event == "flow.started"),
        "completed": sum(1 for e in events if e.event == "flow.completed"),
        "expired": sum(1 for e in events if e.event == "flow.expired"),
        "suspended": sum(1 for e in events if e.event == "flow.suspended"),
        "resumed": sum(1 for e in events if e.event == "flow.resumed"),
    }
    return {
        "events_total": len(events),
        "events_by_family": dict(sorted(by_family.items())),
        "first_event_at": events[0].created_at if events else None,
        "last_event_at": events[-1].created_at if events else None,
        "interactions_total": len(trace.interactions),
        "interactions_by_kind": interactions_by_kind,
        "interactions_by_surface": interactions_by_surface,
        "ai": {**ai_totals, "by_purpose": ai_by_purpose},
        "renders": renders,
        "flows": flow_counts,
        "legacy_uncorrelated_events": len(trace.legacy_events),
    }


def _signals_markdown(signals_payload: dict[str, Any]) -> str:
    lines = ["# Deterministic signal scan", ""]
    lines.append(f"Detector set version: {signals_payload['detector_set_version']}")
    lines.append("")
    signals = signals_payload["signals"]
    if not signals:
        lines.append("No deterministic signals in this window.")
        return "\n".join(lines) + "\n"
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for signal in sorted(signals, key=lambda s: (order.get(s["severity_hint"], 9), s["detector_id"])):
        lines.append(
            f"## [{signal['severity_hint'].upper()}] {signal['detector_id']} × {signal['count']}"
        )
        lines.append(signal["summary"])
        for occurrence in signal["occurrences"][:20]:
            ref = f"events {occurrence['event_ids']}"
            if occurrence.get("interaction_id"):
                ref += f" · interaction {occurrence['interaction_id']}"
            detail = occurrence.get("detail") or {}
            if detail:
                ref += f" · {json.dumps(detail, ensure_ascii=False)}"
            lines.append(f"- {ref}")
        if signal["count"] > 20:
            lines.append(f"- … {signal['count'] - 20} more occurrences (see signals.json)")
        lines.append("")
    lines.append(
        "_Signals are a floor and a navigation aid — repetition alone never "
        "proves a shared root cause; the review protocol owns judgment._"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Review ids and directory management
# ---------------------------------------------------------------------------


def mint_review_id(reviews_dir: Path, *, rebuilt_from: str | None = None) -> str:
    if rebuilt_from:
        base = f"{rebuilt_from}_rb"
        sequence = 1
        while (reviews_dir / f"{base}{sequence}").exists():
            sequence += 1
        return f"{base}{sequence}"
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sequence = 1
    while (reviews_dir / f"{day}_{sequence}").exists():
        sequence += 1
    return f"{day}_{sequence}"


def package_dir(reviews_dir: Path | str, review_id: str) -> Path:
    return Path(reviews_dir) / review_id


def load_manifest(reviews_dir: Path | str, review_id: str) -> dict[str, Any]:
    path = package_dir(reviews_dir, review_id) / MANIFEST_NAME
    if not path.exists():
        raise PackageError(f"no manifest for review {review_id!r} under {reviews_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_package(reviews_dir: Path | str, review_id: str) -> list[str]:
    """Integrity problems (empty list ⇔ package is intact and complete)."""
    problems: list[str] = []
    try:
        manifest = load_manifest(reviews_dir, review_id)
    except (PackageError, ValueError) as exc:
        return [str(exc)]
    if manifest.get("status") != "complete":
        problems.append(f"package status is {manifest.get('status')!r}, not 'complete'")
    if manifest.get("package_schema_version") != PACKAGE_SCHEMA_VERSION:
        problems.append(
            "package schema version "
            f"{manifest.get('package_schema_version')!r} != current {PACKAGE_SCHEMA_VERSION!r}"
        )
    directory = package_dir(reviews_dir, review_id)
    for name, expected in (manifest.get("artifact_hashes") or {}).items():
        artifact = directory / name
        if not artifact.exists():
            problems.append(f"missing evidence artifact: {name}")
        elif _sha256_file(artifact) != expected:
            problems.append(f"evidence artifact was rewritten: {name}")
    return problems


def list_packages(reviews_dir: Path | str) -> list[dict[str, Any]]:
    root = Path(reviews_dir)
    if not root.exists():
        return []
    summaries: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        manifest_path = child / MANIFEST_NAME
        if not child.is_dir() or not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            summaries.append({"review_id": child.name, "status": "unreadable"})
            continue
        summaries.append({
            "review_id": manifest.get("review_id", child.name),
            "status": manifest.get("status"),
            "created_at": manifest.get("created_at"),
            "event_count": manifest.get("event_count"),
            "first_event_id": manifest.get("first_event_id"),
            "last_event_id": manifest.get("last_event_id"),
            "rebuilt_from": manifest.get("rebuilt_from"),
        })
    return summaries


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


async def build_package(
    db: Any,
    user_id: int,
    requested: RequestedSelection,
    resolved: ResolvedSelection,
    *,
    reviews_dir: Path | str,
    review_id: str | None = None,
    rebuilt_from: str | None = None,
    repo_root: Path | str | None = None,
    page_size: int = 500,
) -> PackageBuildResult:
    """Assemble one immutable review package (read-only against the DB).

    The caller resolves the selection first (R2) — resolution and building
    are separate so a recorded selection can be re-resolved for rebuilds.
    On any failure the staging directory is removed and nothing is
    promoted; an existing complete package is never overwritten.
    """
    reviews_root = Path(reviews_dir)
    reviews_root.mkdir(parents=True, exist_ok=True)
    resolved_id = review_id or mint_review_id(reviews_root, rebuilt_from=rebuilt_from)
    final_dir = reviews_root / resolved_id
    if final_dir.exists():
        raise PackageError(
            f"review {resolved_id!r} already exists — evidence is immutable; "
            "rebuild under a new id (build --rebuild) instead of overwriting"
        )
    staging = reviews_root / f".building_{resolved_id}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        events: list[ProductEvent] = []
        async for event in iter_window_events(db, resolved, page_size=page_size):
            events.append(event)
        trace = build_session_trace(user_id, events)

        # events.jsonl — write-time-redacted rows, defensively re-redacted.
        with (staging / EVENTS_NAME).open("w", encoding="utf-8") as handle:
            for event in events:
                row = asdict(event)
                row["properties"] = redact(row["properties"])
                row["before"] = redact(row["before"]) if row["before"] is not None else None
                row["after"] = redact(row["after"]) if row["after"] is not None else None
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        (staging / TIMELINE_NAME).write_text(render_timeline(trace), encoding="utf-8")

        stats = build_stats(events, trace)
        _assert_committable_clean(STATS_NAME, stats)
        (staging / STATS_NAME).write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        signals = run_detectors(trace)
        _assert_committable_clean(SIGNALS_NAME, signals)
        (staging / SIGNALS_NAME).write_text(
            json.dumps(signals, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (staging / SIGNALS_MD_NAME).write_text(_signals_markdown(signals), encoding="utf-8")

        resolved_public = resolved.to_dict()
        resolved_public.pop("user_id", None)  # pseudonymized below
        manifest: dict[str, Any] = {
            "review_id": resolved_id,
            "status": "complete",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "package_schema_version": PACKAGE_SCHEMA_VERSION,
            "detector_set_version": DETECTOR_SET_VERSION,
            "redaction_policy_version": REDACTION_POLICY_VERSION,
            "review_protocol_version": REVIEW_PROTOCOL_VERSION,
            "user_scope": user_pseudonym(user_id),
            "requested_selection": requested.to_dict(),
            "resolved_selection": resolved_public,
            "first_event_id": events[0].id if events else None,
            "last_event_id": events[-1].id if events else None,
            "event_count": len(events),
            "trace_ids": sorted({e.trace_id for e in events if e.trace_id}),
            "interaction_ids": sorted({e.interaction_id for e in events if e.interaction_id}),
            "rebuilt_from": rebuilt_from,
            **_git_identity(Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]),
            "artifact_hashes": {
                name: _sha256_file(staging / name) for name in HASHED_ARTIFACTS
            },
            "gitignored_artifacts": list(GITIGNORED_ARTIFACTS),
        }
        _assert_committable_clean(MANIFEST_NAME, manifest)
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    staging.replace(final_dir)  # atomic promotion — complete or absent
    return PackageBuildResult(
        review_id=resolved_id, path=final_dir, manifest=manifest, event_count=len(events)
    )
