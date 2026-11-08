"""Paths, sidecars, and the atomic cache write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acsm import _paths


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "plugin-data" / "acsm"


def test_paths_derive_from_the_injected_data_dir(data_dir: Path):
    assert _paths.text_path_for(data_dir, "abc") == data_dir / "extracted" / "abc.txt"
    assert _paths.sidecar_path_for(data_dir, "abc") == data_dir / "extracted" / "abc.json"
    assert _paths.inbox_dir(data_dir) == data_dir / "inbox"


def test_module_does_not_hard_code_a_home_directory():
    source = Path(_paths.__file__).read_text(encoding="utf-8")
    assert "Path.home()" not in source
    assert "~/.marginalia" not in source


def test_write_cache_publishes_both_files(data_dir: Path):
    record = _paths.build_sidecar(
        key="urn:uuid:1",
        key_source="rights_metadata",
        title="A Book",
        page_count=2,
        ended_by="observed_end",
        restarts=0,
        ocr={"model": "english_g2"},
        suspect_pages=[1],
    )
    path = _paths.write_cache(data_dir, "urn:uuid:1", "page one\npage two\n", record)

    assert path.read_text(encoding="utf-8") == "page one\npage two\n"
    sidecar = json.loads(_paths.sidecar_path_for(data_dir, "urn:uuid:1").read_text(encoding="utf-8"))
    assert sidecar["schema"] == _paths.SIDECAR_SCHEMA
    assert sidecar["page_count"] == 2
    assert sidecar["suspect_pages"] == [1]


def test_write_cache_leaves_no_temporary_files_behind(data_dir: Path):
    record = _paths.build_sidecar(
        key="k",
        key_source="content_hash",
        title="t",
        page_count=1,
        ended_by="stale",
        restarts=0,
        ocr={},
        suspect_pages=[],
    )
    _paths.write_cache(data_dir, "k", "text", record)
    leftovers = sorted(p.name for p in _paths.extracted_dir(data_dir).iterdir())
    assert leftovers == ["k.json", "k.txt"]


def test_write_cache_refuses_an_empty_capture(data_dir: Path):
    with pytest.raises(ValueError):
        _paths.write_cache(data_dir, "k", "   \n\n", {})
    assert not _paths.text_path_for(data_dir, "k").exists()
    assert not _paths.sidecar_path_for(data_dir, "k").exists()


def test_read_sidecar_is_none_for_missing_malformed_and_newer_schema(data_dir: Path):
    assert _paths.read_sidecar(data_dir, "absent") is None

    path = _paths.sidecar_path_for(data_dir, "broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert _paths.read_sidecar(data_dir, "broken") is None

    path.write_text(json.dumps({"schema": _paths.SIDECAR_SCHEMA + 1}), encoding="utf-8")
    assert _paths.read_sidecar(data_dir, "broken") is None

    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert _paths.read_sidecar(data_dir, "broken") is None


def test_read_sidecar_round_trips_a_valid_record(data_dir: Path):
    record = _paths.build_sidecar(
        key="k",
        key_source="rights_metadata",
        title="t",
        page_count=3,
        ended_by="empty",
        restarts=1,
        ocr={"remote": False},
        suspect_pages=[],
    )
    _paths.write_cache(data_dir, "k", "text", record)
    assert _paths.read_sidecar(data_dir, "k") == record


def test_sidecar_rejects_impossible_page_counts_and_indices():
    with pytest.raises(ValueError):
        _paths.build_sidecar(
            key="k",
            key_source="content_hash",
            title="t",
            page_count=0,
            ended_by="stale",
            restarts=0,
            ocr={},
            suspect_pages=[],
        )
    with pytest.raises(ValueError):
        _paths.build_sidecar(
            key="k",
            key_source="content_hash",
            title="t",
            page_count=2,
            ended_by="stale",
            restarts=0,
            ocr={},
            suspect_pages=[2],  # VR-004: must be < page_count
        )


def test_sidecar_deduplicates_and_sorts_suspect_pages():
    record = _paths.build_sidecar(
        key="k",
        key_source="content_hash",
        title="t",
        page_count=5,
        ended_by="stale",
        restarts=0,
        ocr={},
        suspect_pages=[3, 1, 3, 0],
    )
    assert record["suspect_pages"] == [0, 1, 3]


@pytest.mark.parametrize(
    ("key", "valid"),
    [
        ("urn:uuid:6c1f", True),
        ("sha256:" + "a" * 64, True),
        ("B003NX6Z3W", True),
        ("", False),
        ("has space", False),
        ("slash/and/path", False),
        ("../escape", False),
        ("x" * 200, False),
    ],
)
def test_key_validation(key: str, valid: bool):
    assert _paths.is_valid_key(key) is valid
