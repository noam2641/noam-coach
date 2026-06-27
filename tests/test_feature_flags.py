"""Tests for feature_flags.py — env-based flag parsing."""

from __future__ import annotations

import os

import feature_flags


def test_env_bool_truthy_values():
    for val in ("1", "true", "True", "YES", "on", "enabled"):
        assert feature_flags._env_bool("__TEST", False) is False  # baseline
        os.environ["__TEST"] = val
        assert feature_flags._env_bool("__TEST", False) is True, f"failed for {val!r}"
        del os.environ["__TEST"]


def test_env_bool_falsy_values():
    for val in ("0", "false", "no", "off", "disabled", ""):
        os.environ["__TEST"] = val
        assert feature_flags._env_bool("__TEST", True) is False, f"failed for {val!r}"
        del os.environ["__TEST"]


def test_env_bool_missing_uses_default():
    os.environ.pop("__TEST", None)
    assert feature_flags._env_bool("__TEST", True) is True
    assert feature_flags._env_bool("__TEST", False) is False


def test_from_environment_defaults():
    flags = feature_flags.FeatureFlags.from_environment()
    assert flags.conversation_router_v2 is True
    assert flags.smart_plans_v2 is True


def test_from_environment_override(monkeypatch):
    monkeypatch.setenv("FEATURE_SMART_PLANS_V2", "0")
    flags = feature_flags.FeatureFlags.from_environment()
    assert flags.smart_plans_v2 is False


def test_flags_frozen():
    flags = feature_flags.FeatureFlags()
    try:
        flags.smart_plans_v2 = False  # type: ignore[misc]
        assert False, "should be frozen"
    except AttributeError:
        pass
