"""Tests for helpers.py — utility functions."""

from __future__ import annotations

from datetime import datetime

from helpers import _safe_html_block, esc, friendly_error, today_bounds_utc, utc_now


def test_utc_now_format():
    result = utc_now()
    # Should be a valid ISO format string with timezone
    parsed = datetime.fromisoformat(result)
    assert parsed.tzinfo is not None


def test_esc_html_entities():
    assert esc("<b>bold</b>") == "&lt;b&gt;bold&lt;/b&gt;"
    assert esc("a & b") == "a &amp; b"
    assert esc("plain") == "plain"


def test_esc_non_string():
    assert esc(42) == "42"
    assert esc(None) == "None"


def test_safe_html_block_escapes_script():
    result = _safe_html_block("<script>alert(1)</script>")
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_safe_html_block_preserves_formatting():
    result = _safe_html_block("<b>bold</b> and <i>italic</i>")
    assert "<b>bold</b>" in result
    assert "<i>italic</i>" in result


def test_safe_html_block_newlines():
    result = _safe_html_block("line1\nline2")
    assert "<br>" in result


def test_friendly_error_hides_secrets():
    msg = friendly_error(RuntimeError("password=secret123"), "test")
    assert "secret123" not in msg
    assert "קוד תקלה" in msg


def test_today_bounds_utc():
    start, end = today_bounds_utc()
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    assert start_dt.tzinfo is not None
    assert end_dt.tzinfo is not None
    diff = end_dt - start_dt
    assert diff.total_seconds() == 86400  # exactly 1 day
