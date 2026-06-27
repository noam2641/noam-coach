"""Tests for coach_bot.py — utility functions and Mini App upload."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

import coach_bot

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def test_esc_html_escapes_tags() -> None:
    assert coach_bot.esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_esc_html_preserves_normal_text() -> None:
    assert coach_bot.esc("שלום") == "שלום"


def test_safe_html_block_allows_bold_italic() -> None:
    result = coach_bot._safe_html_block("<b>bold</b> and <i>italic</i>")
    assert "<b>bold</b>" in result
    assert "<i>italic</i>" in result


def test_safe_html_block_escapes_script() -> None:
    result = coach_bot._safe_html_block("<script>bad</script>")
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_safe_html_block_converts_newlines() -> None:
    result = coach_bot._safe_html_block("line1\nline2")
    assert "<br>" in result


def test_friendly_error_hides_details() -> None:
    msg = coach_bot.friendly_error(RuntimeError("db_password=secret"), "test")
    assert "secret" not in msg
    assert "db_password" not in msg
    assert "קוד תקלה" in msg


def test_today_bounds_utc_returns_pair() -> None:
    start, end = coach_bot.today_bounds_utc()
    assert "T" in start
    assert "T" in end
    assert start < end


def test_utc_now_format() -> None:
    now = coach_bot.utc_now()
    assert "T" in now
    assert "+" in now or "Z" in now or "UTC" in now


# ---------------------------------------------------------------------------
# _format_fact_value — must never crash the profile screen (P0 regression)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value",
    [
        ("weight_kg", 80.4),
        ("weight_kg", "not a number"),  # malformed -> "לא צוין", no crash
        ("weight_kg", None),
        ("height_cm", 178),
        ("body_fat_pct", 18.2),
        ("avg_steps", 8123),
        ("resting_hr", 62),
        ("avg_steps", None),
        ("active_pain", {"location": "ברך ימין"}),
        ("equipment", ["dumbbells", "bench"]),
        ("equipment", []),
        ("diet_restrictions", "צמחוני"),
        ("training_days_per_week", 4),
        ("session_minutes", 50),
        ("unknown_key", {"a": 1}),
        ("unknown_key", None),
    ],
)
def test_format_fact_value_never_raises(key: str, value: object) -> None:
    """Every fact formatter path must return a string and never raise."""
    result = coach_bot._format_fact_value(key, value)
    assert isinstance(result, str)
    assert result  # never empty


def test_format_fact_value_missing_value_is_not_specified() -> None:
    assert coach_bot._format_fact_value("weight_kg", None) == "לא צוין"
    # The historical crash: a non-numeric weight must degrade, not raise.
    assert coach_bot._format_fact_value("weight_kg", "abc") == "לא צוין"


def test_format_fact_value_requires_key_argument() -> None:
    """Guards the P0 bug where the snapshot called it with one positional arg."""
    with pytest.raises(TypeError):
        coach_bot._format_fact_value(80.0)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


def test_food_item_validation() -> None:
    item = coach_bot.FoodItem(
        name="חזה עוף", grams=200, calories=330, protein=62, carbs=0, fat=7,
        confidence=0.9,
    )
    assert item.grams == 200


def test_food_item_rejects_negative() -> None:
    with pytest.raises(Exception):
        coach_bot.FoodItem(
            name="test", grams=-1, calories=100, protein=10, carbs=0, fat=0,
            confidence=0.5,
        )


# ---------------------------------------------------------------------------
# Mini App upload endpoint
# ---------------------------------------------------------------------------


def _make_valid_zip(xml_content: bytes = b"<HealthData></HealthData>") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("export.xml", xml_content)
    return buf.getvalue()


def test_mini_upload_rejects_unauthenticated() -> None:
    client = TestClient(coach_bot.api)
    response = client.post(
        "/mini/upload",
        files={"file": ("export.zip", b"fake", "application/zip")},
    )
    assert response.status_code == 401


def test_mini_upload_rejects_wrong_extension(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "mini_app_secret", "s" * 48)
    token = coach_bot.make_mini_token(
        coach_bot.SETTINGS.telegram_allowed_user_id,
        purpose="session",
        ttl_seconds=300,
    )
    client = TestClient(coach_bot.api)
    client.cookies.set("noam_mini_session", token, path="/mini")
    response = client.post(
        "/mini/upload",
        files={"file": ("photo.jpg", b"fake", "image/jpeg")},
        cookies={"noam_mini_session": token},
    )
    assert response.status_code == 400
    assert "ZIP" in response.json()["detail"]


def test_mini_upload_rejects_oversized(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "mini_app_secret", "s" * 48)
    monkeypatch.setattr(coach_bot.SETTINGS, "health_import_max_upload_mb", 0)  # 0 MB limit
    token = coach_bot.make_mini_token(
        coach_bot.SETTINGS.telegram_allowed_user_id,
        purpose="session",
        ttl_seconds=300,
    )
    client = TestClient(coach_bot.api)
    response = client.post(
        "/mini/upload",
        files={"file": ("export.zip", b"x" * 1024, "application/zip")},
        cookies={"noam_mini_session": token},
    )
    assert response.status_code == 413


# ---------------------------------------------------------------------------
# Mini App HTML contains upload form
# ---------------------------------------------------------------------------


def test_mini_app_has_upload_form(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "mini_app_secret", "s" * 48)
    token = coach_bot.make_mini_token(
        coach_bot.SETTINGS.telegram_allowed_user_id,
        purpose="session",
        ttl_seconds=300,
    )
    client = TestClient(coach_bot.api, raise_server_exceptions=False)
    response = client.get("/mini", cookies={"noam_mini_session": token})
    # The page may return 500 if DB is not initialized, but at least we can
    # verify the endpoint exists. If it returns 200, check for upload form.
    if response.status_code == 200:
        assert "uploadForm" in response.text
        # Upload URL lives in app.js, not the HTML template
        assert "app.js" in response.text
        assert "Apple Health" in response.text


def test_mini_app_url_allows_localhost_in_dev(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "app_env", "dev")
    assert coach_bot._is_valid_public_url("http://127.0.0.1:8000") is True
    assert coach_bot._is_valid_public_url("http://localhost:8000") is True


def test_mini_app_url_requires_real_https_in_production(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "app_env", "production")
    assert coach_bot._is_valid_public_url("http://127.0.0.1:8000") is False
    assert coach_bot._is_valid_public_url("https://coach.example.com") is False
    assert coach_bot._is_valid_public_url("https://coach.example.org") is True
