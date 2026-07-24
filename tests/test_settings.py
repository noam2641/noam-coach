from __future__ import annotations

import os

import pytest

import coach_bot


def valid_settings(**overrides):
    values = {
        "app_env": "production",
        "telegram_bot_token": "telegram-" + "a" * 40,
        "telegram_allowed_user_id": 1,
        "openai_api_key": "openai-" + "b" * 40,
        "healthkit_api_token": "health-" + "c" * 40,
        "mini_app_secret": "mini-" + "d" * 40,
        "public_base_url": "https://coach.example.com",
    }
    values.update(overrides)
    return coach_bot.Settings(**values)


def test_production_settings_validate() -> None:
    valid_settings().validate_runtime()


def test_production_rejects_http_mini_app() -> None:
    with pytest.raises(RuntimeError, match="https"):
        valid_settings(public_base_url="http://coach.example.com").validate_runtime()


def test_rejects_reused_secrets() -> None:
    token = "same-" + "x" * 40
    with pytest.raises(RuntimeError, match="ייחודי"):
        valid_settings(
            telegram_bot_token=token,
            healthkit_api_token=token,
        ).validate_runtime()


def test_production_does_not_require_healthkit_token_when_live_sync_disabled() -> None:
    valid_settings(
        enable_healthkit_api=False,
        healthkit_api_token="",
    ).validate_runtime()


def test_production_requires_healthkit_token_when_live_sync_enabled() -> None:
    with pytest.raises(RuntimeError, match="HEALTHKIT_API_TOKEN"):
        valid_settings(
            enable_healthkit_api=True,
            healthkit_api_token="",
        ).validate_runtime()


def test_local_import_rejects_relative_allowed_root() -> None:
    with pytest.raises(RuntimeError, match="LOCAL_HEALTH_IMPORT_ALLOWED_ROOTS"):
        valid_settings(local_health_import_allowed_roots="relative/path").validate_runtime()


# --- Startup-boundary database-path guard -----------------------------------
# assert_safe_database_path is the explicit guard called by the real runtime
# entrypoint (before DB.init) and by preflight. It is NOT wired into generic
# Settings construction, so the tests above keep using the relative default.


def test_guard_rejects_empty_database_path() -> None:
    for value in (None, "", "   "):
        with pytest.raises(RuntimeError, match="DATABASE_PATH חסר"):
            coach_bot.assert_safe_database_path(value)


def test_guard_rejects_relative_database_path() -> None:
    for value in ("./noam_coach.db", "noam_coach.db", "data/noam_coach.db"):
        with pytest.raises(RuntimeError, match="מוחלט"):
            coach_bot.assert_safe_database_path(value)


def test_guard_rejects_default_relative_fallback() -> None:
    # The exact dangerous default that let a stray DB appear in the repo root.
    # (Use the class default explicitly rather than Settings() so an ambient
    # .env with an absolute DATABASE_PATH cannot mask the check.)
    default_path = type(coach_bot.SETTINGS).model_fields["database_path"].default
    assert default_path == "./noam_coach.db"
    with pytest.raises(RuntimeError, match="מוחלט"):
        coach_bot.assert_safe_database_path(default_path)


def test_guard_rejects_unnormalized_absolute_path(tmp_path) -> None:
    messy = str(tmp_path / "a" / ".." / "noam_coach.db")
    with pytest.raises(RuntimeError, match="מנורמל"):
        coach_bot.assert_safe_database_path(messy)


@pytest.mark.skipif(
    os.name != "nt",
    reason="A drive-letter path (C:\\...) is only an absolute path on Windows; "
    "the cross-platform acceptance case is covered by "
    "test_guard_accepts_temporary_absolute_path via tmp_path.",
)
def test_guard_accepts_canonical_windows_path() -> None:
    from pathlib import Path

    canonical = r"C:\coach_bot\noam-coach\data\noam_coach.db"
    resolved = coach_bot.assert_safe_database_path(canonical)
    assert isinstance(resolved, Path)
    assert resolved.is_absolute()


def test_guard_accepts_temporary_absolute_path(tmp_path) -> None:
    # Tests use absolute tmp DB paths; the guard must not reject them, and it
    # must not create the file or its parent.
    target = tmp_path / "sub" / "temp.db"
    resolved = coach_bot.assert_safe_database_path(str(target))
    assert resolved.is_absolute()
    assert not target.exists()
    assert not target.parent.exists()
