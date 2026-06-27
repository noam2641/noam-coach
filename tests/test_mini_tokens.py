from __future__ import annotations

import coach_bot


def test_mini_tokens_are_purpose_bound(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "mini_app_secret", "x" * 48)
    token = coach_bot.make_mini_token(123, purpose="login", ttl_seconds=60)
    assert coach_bot.verify_mini_token(token, purpose="login") == 123
    assert coach_bot.verify_mini_token(token, purpose="session") is None
    assert coach_bot.verify_mini_token(token + "x", purpose="login") is None


async def test_login_token_is_one_time(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "mini_app_secret", "y" * 48)
    db = coach_bot.Database(str(tmp_path / "tokens.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, ?, ?, ?)",
        (456, "Test", "test", "2026-01-01T00:00:00+00:00"),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    token = coach_bot.make_mini_token(456, purpose="login", ttl_seconds=60)
    assert await coach_bot.consume_mini_login_token(token) == 456
    assert await coach_bot.consume_mini_login_token(token) is None
