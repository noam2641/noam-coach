"""Review batch R4 — immutable, redacted, atomic review packages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import event_log
from db import Database
from noam_coach.observability.review_package import (
    PackageError,
    SensitiveExportError,
    _assert_committable_clean,
    build_package,
    list_packages,
    load_manifest,
    mint_review_id,
    verify_package,
)
from noam_coach.observability.review_versions import PACKAGE_SCHEMA_VERSION
from noam_coach.observability.review_window import (
    RequestedSelection,
    load_cursor,
    resolve_selection,
)

USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(str(tmp_path / "r4.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, "2026-07-01T00:00:00+00:00"),
    )
    return database


async def _seed_session(db: Database, *, interactions: int = 3) -> None:
    """A small realistic window: received → AI → render → delivered, plus
    one crash and one fallback for signal coverage."""
    for index in range(interactions):
        trace, interaction = f"tr_{index}", f"in_{index}"
        await event_log.append_event(
            db, USER_ID, "interaction.received",
            trace_id=trace, interaction_id=interaction, surface="telegram",
            properties={"kind": "text", "content": {"text": f"הודעה {index}"}},
        )
        await event_log.append_event(
            db, USER_ID, "ai.call.started",
            trace_id=trace, interaction_id=interaction,
            properties={"ai_call_id": f"ai_{index}", "purpose": "intent_classification"},
        )
        await event_log.append_event(
            db, USER_ID, "ai.call.completed",
            trace_id=trace, interaction_id=interaction,
            properties={"ai_call_id": f"ai_{index}", "purpose": "intent_classification",
                        "duration_ms": 120 + index},
        )
        await event_log.append_event(
            db, USER_ID, "ui.render.prepared",
            trace_id=trace, interaction_id=interaction,
            properties={"render_id": f"rn_{index}", "content": {"text": "תשובה"}},
        )
        await event_log.append_event(
            db, USER_ID, "delivery.succeeded",
            trace_id=trace, interaction_id=interaction, outcome="delivered",
            properties={"render_id": f"rn_{index}", "operation": "reply"},
        )
    await event_log.append_event(
        db, USER_ID, "error.captured",
        trace_id="tr_crash", interaction_id="in_crash", status="failed",
        outcome="unhandled_exception",
        properties={"boundary": "telegram_handler:text", "error_type": "ValueError"},
    )
    await event_log.append_event(
        db, USER_ID, "decision.fallback_selected",
        trace_id="tr_0", interaction_id="in_0", entity="daily_menu",
        properties={"reason": "ai_repair_failed_meal_splice"},
    )


async def _build(db: Database, tmp_path: Path, **kwargs):
    reviews = tmp_path / "reviews"
    requested = RequestedSelection(mode="event_id_range", after_event_id=0)
    resolved = await resolve_selection(db, USER_ID, requested, reviews_dir=reviews)
    return await build_package(
        db, USER_ID, requested, resolved, reviews_dir=reviews, **kwargs
    ), reviews


async def test_package_layout_manifest_and_hashes(db: Database, tmp_path: Path) -> None:
    await _seed_session(db)
    result, reviews = await _build(db, tmp_path, review_id="2026-07-18_1")

    directory = result.path
    for name in ("manifest.json", "events.jsonl", "timeline.md", "stats.json",
                 "signals.json", "signals.md"):
        assert (directory / name).exists(), name

    manifest = load_manifest(reviews, "2026-07-18_1")
    assert manifest["status"] == "complete"
    assert manifest["package_schema_version"] == PACKAGE_SCHEMA_VERSION
    assert manifest["event_count"] == 17
    assert manifest["first_event_id"] == 1 and manifest["last_event_id"] == 17
    assert manifest["requested_selection"]["mode"] == "event_id_range"
    assert "in_crash" in manifest["interaction_ids"]
    assert manifest["detector_set_version"] and manifest["redaction_policy_version"]
    assert manifest["review_protocol_version"]
    assert set(manifest["artifact_hashes"]) == {
        "events.jsonl", "timeline.md", "stats.json", "signals.json",
    }
    assert manifest["gitignored_artifacts"] == ["events.jsonl", "timeline.md"]
    # Pseudonymized user scope — no raw user id anywhere in the manifest.
    assert manifest["user_scope"].startswith("u_")
    assert '"user_id"' not in json.dumps(manifest)

    assert verify_package(reviews, "2026-07-18_1") == []


async def test_events_are_canonical_order_and_stats_signals_correct(
    db: Database, tmp_path: Path
) -> None:
    await _seed_session(db)
    result, reviews = await _build(db, tmp_path)
    rows = [
        json.loads(line)
        for line in (result.path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    ids = [row["id"] for row in rows]
    assert ids == sorted(ids)

    stats = json.loads((result.path / "stats.json").read_text(encoding="utf-8"))
    assert stats["events_total"] == 17
    assert stats["interactions_total"] == 4  # 3 + crash interaction
    assert stats["ai"]["calls"] == 3 and stats["ai"]["failed"] == 0
    assert stats["ai"]["by_purpose"]["intent_classification"]["p50_ms"] == 121
    assert stats["renders"]["delivered"] == 3

    signals = json.loads((result.path / "signals.json").read_text(encoding="utf-8"))
    ids_found = {signal["detector_id"] for signal in signals["signals"]}
    assert "error_captured" in ids_found
    assert "decision_fallback" in ids_found

    timeline = (result.path / "timeline.md").read_text(encoding="utf-8")
    assert "interaction in_0" in timeline


async def test_committable_artifacts_carry_no_conversation_content(
    db: Database, tmp_path: Path
) -> None:
    await _seed_session(db)
    result, _reviews = await _build(db, tmp_path)
    for name in ("manifest.json", "stats.json", "signals.json", "signals.md"):
        text = (result.path / name).read_text(encoding="utf-8")
        assert "הודעה" not in text, name  # user text stays in gitignored artifacts
        assert "תשובה" not in text, name


async def test_sensitive_export_fails_closed() -> None:
    with pytest.raises(SensitiveExportError):
        _assert_committable_clean("stats.json", {"api_key": "sk-abcdef1234567890abcd"})
    with pytest.raises(SensitiveExportError):
        _assert_committable_clean("stats.json", {"note": "Bearer abcdefgh12345678"})
    with pytest.raises(SensitiveExportError):
        _assert_committable_clean("manifest.json", {"user_id": 12345})
    _assert_committable_clean("stats.json", {"events_total": 5})  # clean passes


async def test_existing_package_is_never_overwritten(db: Database, tmp_path: Path) -> None:
    await _seed_session(db, interactions=1)
    result, reviews = await _build(db, tmp_path, review_id="fixed_id")
    original_manifest = (result.path / "manifest.json").read_bytes()

    requested = RequestedSelection(mode="event_id_range", after_event_id=0)
    resolved = await resolve_selection(db, USER_ID, requested, reviews_dir=reviews)
    with pytest.raises(PackageError, match="immutable"):
        await build_package(
            db, USER_ID, requested, resolved, reviews_dir=reviews, review_id="fixed_id"
        )
    assert (result.path / "manifest.json").read_bytes() == original_manifest


async def test_rebuild_mints_linked_id(db: Database, tmp_path: Path) -> None:
    await _seed_session(db, interactions=1)
    _result, reviews = await _build(db, tmp_path, review_id="2026-07-18_1")
    rebuild_id = mint_review_id(reviews, rebuilt_from="2026-07-18_1")
    assert rebuild_id == "2026-07-18_1_rb1"

    requested = RequestedSelection(mode="event_id_range", after_event_id=0)
    resolved = await resolve_selection(db, USER_ID, requested, reviews_dir=reviews)
    rebuilt = await build_package(
        db, USER_ID, requested, resolved,
        reviews_dir=reviews, review_id=rebuild_id, rebuilt_from="2026-07-18_1",
    )
    assert rebuilt.manifest["rebuilt_from"] == "2026-07-18_1"
    # Same evidence window ⇒ identical evidence hashes (deterministic rebuild).
    original = load_manifest(reviews, "2026-07-18_1")
    assert rebuilt.manifest["artifact_hashes"]["events.jsonl"] == \
        original["artifact_hashes"]["events.jsonl"]

    listed = {item["review_id"]: item for item in list_packages(reviews)}
    assert listed["2026-07-18_1_rb1"]["rebuilt_from"] == "2026-07-18_1"


async def test_failed_build_promotes_nothing_and_moves_no_cursor(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_session(db, interactions=1)
    reviews = tmp_path / "reviews"
    requested = RequestedSelection(mode="event_id_range", after_event_id=0)
    resolved = await resolve_selection(db, USER_ID, requested, reviews_dir=reviews)

    import noam_coach.observability.review_package as package_module

    def _boom(trace):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(package_module, "run_detectors", _boom)
    with pytest.raises(RuntimeError, match="detector exploded"):
        await build_package(
            db, USER_ID, requested, resolved, reviews_dir=reviews, review_id="doomed"
        )
    assert not (reviews / "doomed").exists()
    assert not any(reviews.glob(".building_*"))
    assert load_cursor(reviews) is None  # cursor untouched by building, ever


async def test_tampered_evidence_detected(db: Database, tmp_path: Path) -> None:
    await _seed_session(db, interactions=1)
    result, reviews = await _build(db, tmp_path, review_id="tamper")
    (result.path / "events.jsonl").write_text("{}\n", encoding="utf-8")
    problems = verify_package(reviews, "tamper")
    assert any("rewritten" in problem for problem in problems)


async def test_empty_window_builds_explicit_empty_package(db: Database, tmp_path: Path) -> None:
    result, reviews = await _build(db, tmp_path, review_id="empty")
    assert result.event_count == 0
    manifest = load_manifest(reviews, "empty")
    assert manifest["event_count"] == 0
    assert manifest["first_event_id"] is None
    assert verify_package(reviews, "empty") == []


async def test_package_larger_than_default_loader_limit(db: Database, tmp_path: Path) -> None:
    for index in range(2300):
        await event_log.append_event(
            db, USER_ID, "state.mutated", trace_id="tr_big",
            interaction_id=f"in_{index % 7}", properties={"n": index},
        )
    result, reviews = await _build(db, tmp_path, review_id="big")
    assert result.event_count == 2300  # beyond list_events' 2000 ceiling
    rows = (result.path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2300
    assert verify_package(reviews, "big") == []


async def test_builder_is_read_only_against_snapshot(db: Database, tmp_path: Path) -> None:
    """Building from a copied database file must not change a single byte."""
    import shutil

    await _seed_session(db, interactions=2)
    snapshot = tmp_path / "snapshot.db"
    shutil.copyfile(tmp_path / "r4.db", snapshot)
    before = snapshot.read_bytes()

    snap_db = Database(str(snapshot))
    requested = RequestedSelection(mode="event_id_range", after_event_id=0)
    resolved = await resolve_selection(snap_db, USER_ID, requested, reviews_dir=tmp_path / "rv")
    result = await build_package(
        snap_db, USER_ID, requested, resolved, reviews_dir=tmp_path / "rv", review_id="snap"
    )
    assert result.event_count > 0
    assert snapshot.read_bytes() == before


def test_cli_build_list_show_validate(tmp_path: Path, capsys) -> None:
    import asyncio as _asyncio

    from scripts.review_session import main

    async def _prepare() -> None:
        database = Database(str(tmp_path / "cli.db"))
        await database.init()
        await database.execute(
            "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
            (USER_ID, "2026-07-01T00:00:00+00:00"),
        )
        await _seed_session(database, interactions=1)

    _asyncio.run(_prepare())
    db_path = str(tmp_path / "cli.db")
    reviews = str(tmp_path / "reviews")
    assert main([
        "build", "--db", db_path, "--user", str(USER_ID),
        "--after-id", "0", "--reviews-dir", reviews, "--review-id", "cli_1",
    ]) == 0
    out = capsys.readouterr().out
    assert "review package: cli_1" in out
    assert "cursor NOT advanced" in out

    assert main(["list", "--reviews-dir", reviews]) == 0
    assert "cli_1" in capsys.readouterr().out
    assert main(["show", "cli_1", "--reviews-dir", reviews]) == 0
    assert '"status": "complete"' in capsys.readouterr().out
    assert main(["validate", "cli_1", "--reviews-dir", reviews]) == 0
    assert "intact" in capsys.readouterr().out


def test_cli_requires_exactly_one_selection(tmp_path: Path) -> None:
    from scripts.review_session import main

    with pytest.raises(SystemExit, match="exactly one selection"):
        main(["build", "--reviews-dir", str(tmp_path)])
