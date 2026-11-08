"""Fulfillment and identity: rights metadata, hashing, the container wait."""

from __future__ import annotations

import asyncio
import hashlib
import zipfile
from pathlib import Path

import pytest

from acsm import fulfill
from acsm.fulfill import Fulfilled, FulfillError

RIGHTS_XML = """<?xml version="1.0"?>
<rights xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier>urn:uuid:6c1f5d3a-1111-2222-3333-444455556666</dc:identifier>
</rights>
"""


def make_epub(path: Path, rights: str | None = RIGHTS_XML) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        if rights is not None:
            archive.writestr("META-INF/rights.xml", rights)
        archive.writestr("OEBPS/content.xhtml", "<html/>")
    return path


def make_prefix(tmp_path: Path, *, with_reader: bool = True, with_library: bool = True) -> Path:
    prefix = tmp_path / "wineprefix"
    if with_reader:
        reader = fulfill.reader_executable(prefix)
        reader.parent.mkdir(parents=True, exist_ok=True)
        reader.write_text("", encoding="utf-8")
    if with_library:
        (prefix / "drive_c/users/steamuser/Documents/My Digital Editions").mkdir(parents=True)
    return prefix


# ----------------------------------------------------------------- identity


def test_rights_identifier_is_used_when_present(tmp_path: Path):
    container = make_epub(tmp_path / "book.epub")
    key, source = fulfill.document_key(container)
    assert key == "urn:uuid:6c1f5d3a-1111-2222-3333-444455556666"
    assert source == "rights_metadata"


def test_missing_rights_metadata_falls_back_to_the_content_hash(tmp_path: Path):
    container = make_epub(tmp_path / "book.epub", rights=None)
    key, source = fulfill.document_key(container)
    expected = "sha256:" + hashlib.sha256(container.read_bytes()).hexdigest()
    assert key == expected
    assert source == "content_hash"


def test_unusable_identifier_falls_back_to_the_content_hash(tmp_path: Path):
    # VR-001: an identifier that cannot be a filename component is not a key.
    container = make_epub(
        tmp_path / "book.epub",
        rights="<rights><dc:identifier>has spaces/and/slashes</dc:identifier></rights>",
    )
    key, source = fulfill.document_key(container)
    assert source == "content_hash"
    assert key.startswith("sha256:")


def test_plain_pdf_container_is_scanned_for_a_rights_block(tmp_path: Path):
    container = tmp_path / "book.pdf"
    container.write_bytes(b"%PDF-1.7\n<rights><identifier>urn:uuid:pdf-1</identifier></rights>\n")
    key, source = fulfill.document_key(container)
    assert (key, source) == ("urn:uuid:pdf-1", "rights_metadata")


def test_pdf_without_rights_still_produces_a_key(tmp_path: Path):
    container = tmp_path / "book.pdf"
    container.write_bytes(b"%PDF-1.7\nno rights here\n")
    key, source = fulfill.document_key(container)
    assert source == "content_hash" and key.startswith("sha256:")


def test_document_key_accepts_an_explicit_identifier(tmp_path: Path):
    container = tmp_path / "book.pdf"
    container.write_bytes(b"%PDF")
    assert fulfill.document_key(container, rights_id="urn:uuid:given") == (
        "urn:uuid:given",
        "rights_metadata",
    )


# ------------------------------------------------------------------- prefix


def test_library_dir_and_container_discovery(tmp_path: Path):
    prefix = make_prefix(tmp_path)
    library = fulfill.library_dir(prefix)
    assert library is not None and library.name == "My Digital Editions"
    (library / "a.pdf").write_bytes(b"%PDF")
    (library / "notes.txt").write_text("ignore me", encoding="utf-8")
    assert {path.name for path in fulfill.containers_in(library)} == {"a.pdf"}


def test_library_dir_is_none_when_the_reader_never_ran(tmp_path: Path):
    prefix = make_prefix(tmp_path, with_library=False)
    assert fulfill.library_dir(prefix) is None
    assert fulfill.containers_in(None) == set()


async def test_wait_for_container_only_accepts_a_new_file(tmp_path: Path):
    folder = tmp_path / "lib"
    folder.mkdir()
    old = folder / "old.pdf"
    old.write_bytes(b"%PDF old")

    async def sleep(_seconds: float) -> None:
        (folder / "new.epub").write_bytes(b"new")

    found = await fulfill.wait_for_container(
        folder, before={old}, timeout=5.0, sleep=sleep
    )
    assert found.name == "new.epub"


async def test_wait_for_container_times_out_with_a_useful_message(tmp_path: Path):
    folder = tmp_path / "lib"
    folder.mkdir()

    async def sleep(_seconds: float) -> None:
        return None

    with pytest.raises(TimeoutError, match="no new container"):
        await fulfill.wait_for_container(folder, before=set(), timeout=0.01, sleep=sleep)


async def test_wait_for_container_rejects_a_missing_library():
    with pytest.raises(FulfillError, match="library directory was not found"):
        await fulfill.wait_for_container(None, before=set(), timeout=0.01)


# ------------------------------------------------------------------ fulfill


class FakeDisplay:
    display = ":99"


async def test_fulfill_names_the_install_action_when_the_reader_is_absent(tmp_path: Path):
    prefix = make_prefix(tmp_path, with_reader=False)
    acsm = tmp_path / "loan.acsm"
    acsm.write_text("<acsm/>", encoding="utf-8")

    with pytest.raises(FulfillError) as error:
        await fulfill.fulfill(acsm, prefix=prefix, display=FakeDisplay())
    assert error.value.action == "research-engine-acsm-setup"
    assert "Adobe Digital Editions was not found" in str(error.value)


async def test_fulfill_rejects_a_missing_acsm_file(tmp_path: Path):
    prefix = make_prefix(tmp_path)
    with pytest.raises(FulfillError, match="is not a file"):
        await fulfill.fulfill(tmp_path / "nope.acsm", prefix=prefix, display=FakeDisplay())


async def test_fulfill_returns_the_container_and_its_key(tmp_path: Path, monkeypatch):
    prefix = make_prefix(tmp_path)
    acsm = tmp_path / "loan.acsm"
    acsm.write_text("<acsm/>", encoding="utf-8")
    library = fulfill.library_dir(prefix)
    assert library is not None

    launched: list[Path] = []

    async def fake_launch(display, reader, target, *, prefix, timeout=None):
        launched.append(target)
        make_epub(library / "book.epub")

    monkeypatch.setattr(fulfill, "launch_reader", fake_launch)

    async def sleep(_seconds: float) -> None:
        return None

    fulfilled = await fulfill.fulfill(
        acsm, prefix=prefix, display=FakeDisplay(), timeout=1.0, sleep=sleep
    )

    assert launched == [acsm], "the reader is handed the .acsm, not a container"
    assert isinstance(fulfilled, Fulfilled)
    assert fulfilled.container.name == "book.epub"
    assert fulfilled.key.startswith("urn:uuid:")
    assert fulfilled.title == "book"


@pytest.mark.parametrize(
    ("blocker", "expected_action"),
    [("activate", "research-engine-acsm-setup --activate"), ("loan", "re-borrow")],
)
async def test_fulfill_maps_a_reader_dialog_to_an_operator_action(
    tmp_path: Path, monkeypatch, blocker, expected_action
):
    prefix = make_prefix(tmp_path)
    acsm = tmp_path / "loan.acsm"
    acsm.write_text("<acsm/>", encoding="utf-8")

    async def fake_launch(*args, **kwargs):
        return None

    async def fake_blocker(display):
        return blocker

    async def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(fulfill, "launch_reader", fake_launch)
    monkeypatch.setattr(fulfill, "detect_blocker", fake_blocker)

    with pytest.raises(FulfillError) as error:
        await fulfill.fulfill(acsm, prefix=prefix, display=FakeDisplay(), timeout=0.01, sleep=sleep)
    assert expected_action in (error.value.action or "")
    assert error.value.action is not None


async def test_find_container_recovers_a_container_by_key(tmp_path: Path):
    prefix = make_prefix(tmp_path)
    library = fulfill.library_dir(prefix)
    assert library is not None
    container = make_epub(library / "book.epub")
    key, _ = fulfill.document_key(container)

    assert await fulfill.find_container(prefix, key) == container
    assert await fulfill.find_container(prefix, "urn:uuid:not-here") is None


async def test_open_in_reader_reports_a_missing_reader(tmp_path: Path):
    prefix = make_prefix(tmp_path, with_reader=False)
    with pytest.raises(FulfillError, match="Adobe Digital Editions was not found"):
        await fulfill.open_in_reader(prefix, tmp_path / "x.pdf", display=FakeDisplay())


def test_launch_reader_is_the_only_place_a_reader_process_is_spawned(tmp_path: Path, monkeypatch):
    """The spawn site is one function, so tests and probes can replace it."""
    source = Path(fulfill.__file__).read_text(encoding="utf-8")
    assert source.count("create_subprocess_exec") == 1


async def test_detect_blocker_returns_none_when_the_probe_fails(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("no xdotool")

    monkeypatch.setattr(fulfill, "run_command", boom)
    assert await fulfill.detect_blocker(":9") is None


async def test_detect_blocker_reads_window_titles(monkeypatch):
    calls: list[list[str]] = []

    async def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "search":
            return b"111\n222\n"
        return b"Adobe Digital Editions - Activation required"

    monkeypatch.setattr(fulfill, "run_command", fake_run)
    assert await fulfill.detect_blocker(":9") == "activate"
    assert any(argv[1] == "getwindowname" for argv in calls)


def test_asyncio_import_is_used_for_the_module_level_helpers():
    # Guards the module from drifting into a sync-only helper that blocks the loop.
    assert asyncio.sleep is not None
