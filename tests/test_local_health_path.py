from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from noam_coach.services.local_health_path import (
    LocalHealthPathError,
    allowed_roots_from_text,
    looks_like_local_health_path,
    normalize_path_text,
    resolve_local_health_export,
)


def test_normalize_and_detect_windows_path() -> None:
    text = r'/importpath "C:\Users\noam\Desktop\Health Export"'
    assert normalize_path_text(text) == r"C:\Users\noam\Desktop\Health Export"
    assert looks_like_local_health_path(text) is True
    assert looks_like_local_health_path("מה לאכול עכשיו?") is False


def test_allowed_roots_default_to_home(tmp_path: Path) -> None:
    assert allowed_roots_from_text("", default_home=tmp_path) == (tmp_path.resolve(),)


def test_resolve_direct_export(tmp_path: Path) -> None:
    export = tmp_path / "export.zip"
    export.write_bytes(b"zip-placeholder")
    selection = resolve_local_health_export(
        str(export),
        allowed_roots=(tmp_path.resolve(),),
        max_bytes=1024,
    )
    assert selection.path == export.resolve()
    assert selection.from_directory is False
    assert selection.candidate_count == 1


def test_directory_selects_newest_supported_export(tmp_path: Path) -> None:
    older = tmp_path / "older.zip"
    newer = tmp_path / "newer.xml"
    older.write_bytes(b"old")
    newer.write_text("<HealthData/>", encoding="utf-8")
    old_time = time.time() - 60
    os.utime(older, (old_time, old_time))

    selection = resolve_local_health_export(
        str(tmp_path),
        allowed_roots=(tmp_path.resolve(),),
        max_bytes=1024,
    )
    assert selection.path == newer.resolve()
    assert selection.from_directory is True
    assert selection.candidate_count == 2


def test_path_outside_allowed_root_is_rejected(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    export = outside / "export.zip"
    export.write_bytes(b"zip")
    with pytest.raises(LocalHealthPathError, match="מחוץ"):
        resolve_local_health_export(
            str(export),
            allowed_roots=(allowed.resolve(),),
            max_bytes=1024,
        )


def test_directory_without_export_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    with pytest.raises(LocalHealthPathError, match="לא נמצא"):
        resolve_local_health_export(
            str(tmp_path),
            allowed_roots=(tmp_path.resolve(),),
            max_bytes=1024,
        )


def test_oversized_local_export_is_rejected(tmp_path: Path) -> None:
    export = tmp_path / "export.zip"
    export.write_bytes(b"x" * 20)
    with pytest.raises(LocalHealthPathError, match="גדול"):
        resolve_local_health_export(
            str(export),
            allowed_roots=(tmp_path.resolve(),),
            max_bytes=10,
        )
