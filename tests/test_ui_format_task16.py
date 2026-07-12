"""TASK-16 — shared coach-UI formatting helpers and consistent macro formatting."""
from __future__ import annotations

from datetime import datetime

from config import TZ
from noam_coach.bot import ui_format as uf


def test_macro_units_are_canonical() -> None:
    assert uf.CAL_UNIT == "קל׳"
    assert uf.PROTEIN_UNIT == "ג׳ חלבון"
    # No legacy variants.
    assert "קק" not in uf.CAL_UNIT
    assert "קלוריות" not in uf.CAL_UNIT
    assert "גרם" not in uf.PROTEIN_UNIT


def test_cal_and_protein_formatting() -> None:
    assert uf.cal(1316) == "1,316 קל׳"
    assert uf.protein(98) == "98 ג׳ חלבון"
    assert uf.macros(1316, 98) == "1,316 קל׳ | 98 ג׳ חלבון"
    assert uf.macros(600, 50, approx=True) == "כ-600 קל׳ | כ-50 ג׳ חלבון"


def test_heading_format() -> None:
    assert uf.heading("📊", "מצב היום", suffix="00:49") == "<b>📊 מצב היום</b> — 00:49"
    assert uf.heading("🍽️", "נאכל היום") == "<b>🍽️ נאכל היום</b>"


def test_local_hhmm_never_returns_raw_iso() -> None:
    out = uf.local_hhmm("2026-07-12T19:09:00+03:00")
    assert out == "19:09"
    assert "2026-" not in out
    assert uf.local_hhmm(datetime(2026, 7, 12, 8, 5, tzinfo=TZ)) == "08:05"
    assert uf.local_hhmm(None) == ""
    assert uf.local_hhmm("not-a-date") == ""
