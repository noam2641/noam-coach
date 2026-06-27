from __future__ import annotations

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
