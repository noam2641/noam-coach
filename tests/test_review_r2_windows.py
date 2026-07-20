"""Review batch R2 — deterministic, reproducible review-window selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import event_log
from db import Database
from noam_coach.observability.review_window import (
    MalformedCursorError,
    NoCursorError,
    RequestedSelection,
    ResolvedSelection,
    ReviewWindowError,
    advance_cursor,
    collect_window_events,
    load_cursor,
    resolve_selection,
)
from noam_coach.observability.session_trace import build_session_trace

USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(str(tmp_path / "r2.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, "2026-07-01T00:00:00+00:00"),
    )
    return database


async def _seed(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int,
    start_hour: int = 8,
    day: str = "2026-07-10",
    trace_prefix: str = "tr_a",
) -> list[int]:
    """Append ``count`` correlated events with deterministic timestamps."""
    ids: list[int] = []
    for index in range(count):
        minute = index % 60
        hour = start_hour + index // 60
        stamp = f"{day}T{hour:02d}:{minute:02d}:00+00:00"
        monkeypatch.setattr(event_log, "utc_now", lambda s=stamp: s)
        ids.append(
            await event_log.append_event(
                db,
                USER_ID,
                "interaction.received",
                trace_id=f"{trace_prefix}_{index // 3}",
                interaction_id=f"in_{trace_prefix}_{index // 3}",
                properties={"n": index},
            )
        )
    return ids


# ---------------------------------------------------------------------------
# Time-window resolution → canonical id range
# ---------------------------------------------------------------------------


async def test_time_window_resolves_to_id_range(db: Database, monkeypatch) -> None:
    ids = await _seed(db, monkeypatch, count=10)  # 08:00..08:09
    requested = RequestedSelection(
        mode="time_window",
        start_time="2026-07-10T08:03:00+00:00",
        end_time="2026-07-10T08:07:00+00:00",
    )
    resolved = await resolve_selection(db, USER_ID, requested)
    events = await collect_window_events(db, resolved)
    # start inclusive (08:03), end exclusive (08:07 excluded) → 03,04,05,06
    assert [e.properties["n"] for e in events] == [3, 4, 5, 6]
    assert events[0].id == ids[3] and events[-1].id == ids[6]


async def test_exact_boundary_semantics(db: Database, monkeypatch) -> None:
    await _seed(db, monkeypatch, count=3)  # 08:00, 08:01, 08:02
    resolved = await resolve_selection(
        db,
        USER_ID,
        RequestedSelection(
            mode="time_window",
            start_time="2026-07-10T08:01:00+00:00",
            end_time="2026-07-10T08:02:00+00:00",
        ),
    )
    events = await collect_window_events(db, resolved)
    assert [e.properties["n"] for e in events] == [1]  # start in, end out


async def test_naive_timestamps_are_utc(db: Database, monkeypatch) -> None:
    await _seed(db, monkeypatch, count=3)
    resolved = await resolve_selection(
        db,
        USER_ID,
        RequestedSelection(mode="time_window", start_time="2026-07-10T08:01:00"),
    )
    events = await collect_window_events(db, resolved)
    assert [e.properties["n"] for e in events] == [1, 2]


async def test_inverted_window_rejected(db: Database) -> None:
    with pytest.raises(ReviewWindowError):
        await resolve_selection(
            db,
            USER_ID,
            RequestedSelection(
                mode="time_window",
                start_time="2026-07-10T09:00:00",
                end_time="2026-07-10T08:00:00",
            ),
        )


async def test_empty_window_is_explicit_not_an_error(db: Database, monkeypatch) -> None:
    await _seed(db, monkeypatch, count=3)
    resolved = await resolve_selection(
        db,
        USER_ID,
        RequestedSelection(mode="time_window", start_time="2026-07-11T00:00:00"),
    )
    assert resolved.is_empty
    assert await collect_window_events(db, resolved) == []


# ---------------------------------------------------------------------------
# Pagination beyond the 2000-row loader ceiling
# ---------------------------------------------------------------------------


async def test_window_larger_than_2000_events_pages_deterministically(
    db: Database, monkeypatch
) -> None:
    for index in range(2500):
        await event_log.append_event(
            db, USER_ID, "state.mutated", trace_id="tr_big", properties={"n": index}
        )
    resolved = await resolve_selection(
        db, USER_ID, RequestedSelection(mode="event_id_range", after_event_id=0)
    )
    events = await collect_window_events(db, resolved, page_size=500)
    assert len(events) == 2500
    ids = [e.id for e in events]
    assert ids == sorted(ids) and len(set(ids)) == 2500  # ascending, no dupes
    # Deterministic: a second pass yields the identical sequence.
    again = await collect_window_events(db, resolved, page_size=333)
    assert [e.id for e in again] == ids
    # And the trace model works over the full window.
    trace = build_session_trace(USER_ID, events)
    assert len(trace.events) == 2500


# ---------------------------------------------------------------------------
# Explicit traces / interactions and legacy rows
# ---------------------------------------------------------------------------


async def test_explicit_trace_selection(db: Database, monkeypatch) -> None:
    await _seed(db, monkeypatch, count=6, trace_prefix="tr_x")  # traces tr_x_0, tr_x_1
    resolved = await resolve_selection(
        db, USER_ID, RequestedSelection(mode="traces", trace_ids=("tr_x_1",))
    )
    events = await collect_window_events(db, resolved)
    assert {e.trace_id for e in events} == {"tr_x_1"}
    assert [e.properties["n"] for e in events] == [3, 4, 5]


async def test_explicit_interaction_selection(db: Database, monkeypatch) -> None:
    await _seed(db, monkeypatch, count=6, trace_prefix="tr_y")
    resolved = await resolve_selection(
        db,
        USER_ID,
        RequestedSelection(mode="interactions", interaction_ids=("in_tr_y_0",)),
    )
    events = await collect_window_events(db, resolved)
    assert [e.properties["n"] for e in events] == [0, 1, 2]


async def test_legacy_uncorrelated_events_included_in_id_windows(
    db: Database, monkeypatch
) -> None:
    monkeypatch.setattr(event_log, "utc_now", lambda: "2026-07-10T08:00:00+00:00")
    await event_log.append_event(db, USER_ID, "meal_saved")  # no correlation at all
    resolved = await resolve_selection(
        db, USER_ID, RequestedSelection(mode="event_id_range", after_event_id=0)
    )
    events = await collect_window_events(db, resolved)
    assert len(events) == 1 and events[0].is_legacy_uncorrelated
    trace = build_session_trace(USER_ID, events)
    assert len(trace.legacy_events) == 1  # explicit, never merged into a trace


# ---------------------------------------------------------------------------
# Cursor lifecycle
# ---------------------------------------------------------------------------


async def test_first_review_has_no_cursor_and_says_so(db: Database, tmp_path: Path) -> None:
    with pytest.raises(NoCursorError):
        await resolve_selection(
            db,
            USER_ID,
            RequestedSelection(mode="since_last_review"),
            reviews_dir=tmp_path / "reviews",
        )


async def test_cursor_advance_and_since_last_review(
    db: Database, monkeypatch, tmp_path: Path
) -> None:
    reviews = tmp_path / "reviews"
    ids = await _seed(db, monkeypatch, count=6)
    advance_cursor(reviews, last_reviewed_event_id=ids[2], review_id="2026-07-10_1")

    resolved = await resolve_selection(
        db, USER_ID, RequestedSelection(mode="since_last_review"), reviews_dir=reviews
    )
    events = await collect_window_events(db, resolved)
    assert [e.properties["n"] for e in events] == [3, 4, 5]

    state = load_cursor(reviews)
    assert state is not None
    assert state["last_reviewed_event_id"] == ids[2]
    assert state["review_id"] == "2026-07-10_1"


async def test_interrupted_review_leaves_cursor_unchanged(
    db: Database, monkeypatch, tmp_path: Path
) -> None:
    """Building/selecting never advances anything — only the explicit
    completion call does."""
    reviews = tmp_path / "reviews"
    ids = await _seed(db, monkeypatch, count=4)
    advance_cursor(reviews, last_reviewed_event_id=ids[0], review_id="r0")
    before = json.loads((reviews / "state.json").read_text(encoding="utf-8"))

    resolved = await resolve_selection(
        db, USER_ID, RequestedSelection(mode="since_last_review"), reviews_dir=reviews
    )
    await collect_window_events(db, resolved)  # ... and the process dies here

    after = json.loads((reviews / "state.json").read_text(encoding="utf-8"))
    assert after == before
    # Re-running resolves the SAME window again.
    again = await resolve_selection(
        db, USER_ID, RequestedSelection(mode="since_last_review"), reviews_dir=reviews
    )
    assert again.after_event_id == resolved.after_event_id


async def test_malformed_cursor_reports_clearly(db: Database, tmp_path: Path) -> None:
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    (reviews / "state.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(MalformedCursorError, match="state.json"):
        load_cursor(reviews)
    # Explicit selections keep working regardless of the broken cursor.
    resolved = await resolve_selection(
        db, USER_ID, RequestedSelection(mode="event_id_range", after_event_id=0)
    )
    assert resolved.is_empty


async def test_malformed_cursor_bad_value(tmp_path: Path) -> None:
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    (reviews / "state.json").write_text(
        json.dumps({"last_reviewed_event_id": "yesterday"}), encoding="utf-8"
    )
    with pytest.raises(MalformedCursorError):
        load_cursor(reviews)


async def test_overlapping_and_historical_windows_ignore_cursor(
    db: Database, monkeypatch, tmp_path: Path
) -> None:
    reviews = tmp_path / "reviews"
    ids = await _seed(db, monkeypatch, count=6)
    advance_cursor(reviews, last_reviewed_event_id=ids[5], review_id="r1")
    # A historical rerun selects an already-reviewed range explicitly.
    historical = await resolve_selection(
        db,
        USER_ID,
        RequestedSelection(
            mode="event_id_range", after_event_id=ids[0], until_event_id=ids[3]
        ),
    )
    events = await collect_window_events(db, historical)
    assert [e.properties["n"] for e in events] == [1, 2, 3]
    # Round-trips for rebuilds: requested + resolved serialize losslessly.
    requested = RequestedSelection(mode="event_id_range", after_event_id=ids[0], until_event_id=ids[3])
    assert RequestedSelection.from_dict(requested.to_dict()) == requested
    assert ResolvedSelection.from_dict(historical.to_dict()) == historical


# ---------------------------------------------------------------------------
# Requested-selection validation
# ---------------------------------------------------------------------------


def test_requested_selection_validation() -> None:
    with pytest.raises(ReviewWindowError):
        RequestedSelection(mode="everything")
    with pytest.raises(ReviewWindowError):
        RequestedSelection(mode="time_window")
    with pytest.raises(ReviewWindowError):
        RequestedSelection(mode="last_hours", last_hours=0)
    with pytest.raises(ReviewWindowError):
        RequestedSelection(mode="traces")


async def test_last_hours_selection(db: Database, monkeypatch) -> None:
    # Old event far in the past + one "now" event.
    monkeypatch.setattr(event_log, "utc_now", lambda: "2020-01-01T00:00:00+00:00")
    await event_log.append_event(db, USER_ID, "state.mutated", trace_id="tr_old")
    from helpers import utc_now as real_now

    monkeypatch.setattr(event_log, "utc_now", real_now)
    await event_log.append_event(db, USER_ID, "state.mutated", trace_id="tr_new")
    resolved = await resolve_selection(
        db, USER_ID, RequestedSelection(mode="last_hours", last_hours=1)
    )
    events = await collect_window_events(db, resolved)
    assert [e.trace_id for e in events] == ["tr_new"]
