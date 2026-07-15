"""Observability O10 — trace inspection CLI acceptance tests.

- a useful trace can be inspected without direct SQLite queries
- JSON output is structured; human output is readable
- secrets stay redacted; media stay references
- invalid identifiers fail cleanly (non-zero exit, no traceback)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, interaction_scope, set_mode
from noam_coach.observability.emit import emit_event, reset_observability_health
from noam_coach.observability.modes import reset_mode
from scripts.trace_inspect import main_async

USER_ID = 1


@pytest.fixture(autouse=True)
def _obs_isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    yield
    reset_mode()
    reset_observability_health()


@pytest.fixture
async def seeded_db(tmp_path: Path) -> tuple[str, str, str]:
    """A DB with one correlated journey; returns (path, trace_id, interaction_id)."""
    path = str(tmp_path / "cli.db")
    db = Database(path)
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    with interaction_scope(user_id=USER_ID) as scope:
        await emit_event(
            db, USER_ID, "interaction.received", entity="interaction",
            surface="telegram", status="received",
            properties={"kind": "text"},
            content={"text": "מה לאכול עכשיו? הטוקן שלי sk-SECRETSECRET123456"},
        )
        await emit_event(
            db, USER_ID, "ai.call.completed", entity="ai_call", entity_id="ai_test123",
            status="completed", outcome="Intent",
            properties={"ai_call_id": "ai_test123", "purpose": "intent_classification",
                        "model": "gpt-test", "duration_ms": 42,
                        "media_ids": ["md_abc"]},
            content={"output": {"action": "next_meal"}},
        )
    return path, scope.trace_id, scope.interaction_id


@pytest.mark.asyncio
async def test_cli_human_timeline_and_json(seeded_db, capsys) -> None:
    path, trace_id, interaction_id = seeded_db

    assert await main_async(["user", str(USER_ID), "--db", path]) == 0
    human = capsys.readouterr().out
    assert "מה לאכול עכשיו?" in human
    assert "purpose=intent_classification" in human
    # Secrets were redacted at write time and stay redacted here.
    assert "sk-SECRETSECRET123456" not in human
    assert "[REDACTED]" in human

    assert await main_async(["trace-id", trace_id, "--db", path, "--user", str(USER_ID)]) == 0
    assert trace_id in capsys.readouterr().out

    assert await main_async(["interaction", interaction_id, "--db", path, "--user", str(USER_ID), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["interactions"][0]["interaction_id"] == interaction_id
    events = payload["interactions"][0]["events"]
    assert events[0]["event"] == "interaction.received"
    assert "sk-SECRETSECRET123456" not in json.dumps(payload)

    assert await main_async(["last", "--db", path, "--user", str(USER_ID)]) == 0
    assert "interaction.received" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_cli_ai_lookup_and_media_reference_only(seeded_db, capsys) -> None:
    path, _trace_id, _interaction_id = seeded_db
    assert await main_async(["ai", "ai_test123", "--db", path, "--user", str(USER_ID)]) == 0
    out = capsys.readouterr().out
    assert "intent_classification" in out
    assert "md_abc" in out  # media stays a reference
    assert await main_async(["ai", "ai_test123", "--db", path, "--user", str(USER_ID), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["properties"]["ai_call_id"] == "ai_test123"


@pytest.mark.asyncio
async def test_cli_invalid_identifiers_fail_cleanly(seeded_db, capsys) -> None:
    path, _t, _i = seeded_db
    assert await main_async(["interaction", "in_does_not_exist", "--db", path, "--user", str(USER_ID)]) == 2
    assert "no events" in capsys.readouterr().out
    assert await main_async(["trace-id", "tr_nope", "--db", path, "--user", str(USER_ID)]) == 2
    assert await main_async(["ai", "ai_nope", "--db", path, "--user", str(USER_ID)]) == 2
    assert await main_async(["user", "999", "--db", path]) == 2
    with pytest.raises(SystemExit):
        await main_async(["user", "1", "--db", str(Path(path).parent / "missing.db")])
