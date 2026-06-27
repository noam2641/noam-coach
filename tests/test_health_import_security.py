from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import health_import


def write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def test_extracts_only_main_export_xml(tmp_path: Path) -> None:
    archive = tmp_path / "health.zip"
    xml = b"<?xml version='1.0'?><HealthData></HealthData>"
    write_zip(
        archive,
        {
            "apple_health_export/export.xml": xml,
            "apple_health_export/export_cda.xml": b"cda",
            "apple_health_export/clinical-records/secret.txt": b"unused",
        },
    )
    output = tmp_path / "out"
    extracted = health_import.extract_zip(archive, output)
    assert extracted == output / "export.xml"
    assert extracted.read_bytes() == xml
    assert sorted(path.name for path in output.iterdir()) == ["export.xml"]


def test_rejects_path_traversal_member(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    write_zip(archive, {"../export.xml": b"<HealthData/>"})
    output = tmp_path / "out"
    assert health_import.extract_zip(archive, output) is None
    assert not (tmp_path / "export.xml").exists()


def test_rejects_suspicious_compression_ratio(tmp_path: Path) -> None:
    archive = tmp_path / "bomb.zip"
    write_zip(archive, {"export.xml": b"A" * 2_000_000})
    with pytest.raises(ValueError, match="compression ratio"):
        health_import.extract_zip(
            archive,
            tmp_path / "out",
            max_compression_ratio=10,
        )


def test_respects_actual_output_limit(tmp_path: Path) -> None:
    archive = tmp_path / "large.zip"
    write_zip(archive, {"export.xml": b"<HealthData>123456</HealthData>"})
    with pytest.raises(ValueError, match="too large"):
        health_import.extract_zip(archive, tmp_path / "out", max_xml_bytes=10)


def test_magic_checks(tmp_path: Path) -> None:
    xml = tmp_path / "export.xml"
    xml.write_bytes(b"\xef\xbb\xbf  <?xml version='1.0'?><HealthData/>")
    assert health_import.looks_like_xml(xml)
    fake_xml = tmp_path / "fake.xml"
    fake_xml.write_bytes(b"not xml")
    assert not health_import.looks_like_xml(fake_xml)
    assert health_import.is_valid_zip(tmp_path / "health.zip") is False
