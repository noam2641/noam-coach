from __future__ import annotations

from fastapi.testclient import TestClient

import coach_bot


def test_healthz_is_live() -> None:
    client = TestClient(coach_bot.api)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": coach_bot.APP_VERSION}


def test_private_api_rejects_oversized_content_length(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "api_max_body_bytes", 10)
    client = TestClient(coach_bot.api)
    response = client.post(
        "/api/shortcut/health",
        headers={"Content-Length": "11"},
        content=b"{}",
    )
    assert response.status_code == 413


def test_private_api_rejects_oversized_chunked_body(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "api_max_body_bytes", 10)
    client = TestClient(coach_bot.api)

    def chunks():
        yield b"{" + b"x" * 20 + b"}"

    response = client.post("/api/shortcut/health", content=chunks())
    assert response.status_code == 413


def test_device_api_requires_bearer_token(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "enable_healthkit_api", True)
    monkeypatch.setattr(coach_bot.SETTINGS, "healthkit_api_token", "secret-token")
    monkeypatch.setattr(coach_bot.SETTINGS, "api_max_body_bytes", 1024)
    client = TestClient(coach_bot.api)
    payload = {
        "telegram_user_id": coach_bot.SETTINGS.telegram_allowed_user_id,
        "measured_at": "2026-06-18T08:00:00+03:00",
    }
    assert client.post("/api/shortcut/health", json=payload).status_code == 401
    assert (
        client.post(
            "/api/shortcut/health",
            json=payload,
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )


def test_device_api_is_hidden_when_live_sync_disabled(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "enable_healthkit_api", False)
    client = TestClient(coach_bot.api)
    response = client.post(
        "/api/shortcut/health",
        json={
            "telegram_user_id": coach_bot.SETTINGS.telegram_allowed_user_id,
            "measured_at": "2026-06-18T08:00:00+03:00",
        },
    )
    assert response.status_code == 404
