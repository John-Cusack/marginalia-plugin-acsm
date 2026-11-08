"""Orchestration: cache publishing, quality accounting, and the no-image-writes rule."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from acsm import _paths, acquire
from acsm.acquire import text_quality
from acsm.ocr_config import OcrSettings


class FakeContextDisplay:
    display = ":99"

    async def __aenter__(self) -> FakeContextDisplay:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeClient:
    """Stands in for the vendored OCR client, recording request sizes."""

    def __init__(self, texts: dict[int, str] | None = None, *, remote: bool = True) -> None:
        self.remote_enabled = remote
        self.texts = texts or {}
        self.batch_sizes: list[int] = []

    async def recognize(self, pages):
        self.batch_sizes.append(len(pages))
        return [self.texts.get(page.page_id, f"page {page.page_id} text") for page in pages]


class FakeCapture:
    """Replaces the X-facing half of :mod:`acsm.capture`."""

    def __init__(self, frames: list[bytes]) -> None:
        self.frames = list(frames)
        self.position = 0
        self.turns = 0
        self.starts = 0

    async def grab_png(self, display, window_id, **kwargs):
        token = self.frames[min(self.position, len(self.frames) - 1)]
        self.position += 1
        return token

    def crop_png(self, png, box=None):
        return png

    def frame_digest(self, png):
        return png.decode()

    def is_blank(self, png):
        return png == b"BLANK"

    async def send_turn(self, display, window_id):
        self.turns += 1

    async def send_start(self, display, window_id):
        self.starts += 1


@pytest.fixture
def wired(monkeypatch, tmp_path: Path):
    """Wire a scripted capture into :mod:`acsm.acquire` without any X server."""
    fake = FakeCapture([b"p1", b"p2", b"p3"])

    async def open_document() -> str:
        return "12345"

    monkeypatch.setattr(acquire.capture_mod, "Display", FakeContextDisplay)
    monkeypatch.setattr(acquire.capture_mod, "grab_png", fake.grab_png)
    monkeypatch.setattr(acquire.capture_mod, "crop_png", fake.crop_png)
    monkeypatch.setattr(acquire.capture_mod, "frame_digest", fake.frame_digest)
    monkeypatch.setattr(acquire.capture_mod, "is_blank", fake.is_blank)
    monkeypatch.setattr(acquire.capture_mod, "send_turn", fake.send_turn)
    monkeypatch.setattr(acquire.capture_mod, "send_start", fake.send_start)
    return fake, open_document


async def _instant(_seconds: float) -> None:
    """Collapse the loop's settle delays so tests do not spend minutes sleeping."""

    return None


def _frozen() -> float:
    """A clock that never advances: no stall detection, no restart path."""

    return 0.0


FAST: dict = {"sleep": _instant, "clock": _frozen}


async def test_capture_writes_text_and_a_complete_sidecar(wired, tmp_path: Path):
    fake, open_document = wired
    client = FakeClient()
    container = tmp_path / "book.epub"
    container.write_bytes(b"container")

    outcome = await acquire.scrape_to_disk(
        key="k1",
        container=container,
        data_dir=tmp_path,
        settings=OcrSettings(),
        title="A Book",
        client=client,
        open_document=open_document,
        driver_overrides=FAST,
        max_consecutive_stale=2,
    )

    assert outcome.page_count == 3
    assert outcome.ended_by == "stale"
    assert outcome.complete is False
    assert outcome.text == "page 0 text\n\npage 1 text\n\npage 2 text"
    assert outcome.output_path is not None
    assert outcome.output_path.read_text(encoding="utf-8") == outcome.text

    sidecar = json.loads(_paths.sidecar_path_for(tmp_path, "k1").read_text(encoding="utf-8"))
    assert sidecar["title"] == "A Book"
    assert sidecar["page_count"] == 3
    assert sidecar["ended_by"] == "stale"
    assert sidecar["container"] == str(container)
    assert sidecar["ocr"]["quality_metric"] == "printable_char_ratio"
    assert sidecar["ocr"]["remote"] is True
    assert sidecar["suspect_pages"] == []
    assert sidecar["reviewed"] is False


async def test_ocr_is_requested_one_page_at_a_time(wired, tmp_path: Path):
    # research D10: per-page requests keep the loop's empty-page counter exact.
    _, open_document = wired
    client = FakeClient()
    await acquire.scrape_to_disk(
        key="k2",
        container=tmp_path / "book.epub",
        data_dir=tmp_path,
        settings=OcrSettings(),
        client=client,
        open_document=open_document,
        driver_overrides=FAST,
        max_consecutive_stale=2,
    )
    assert client.batch_sizes == [1, 1, 1]


async def test_an_empty_capture_writes_nothing_at_all(wired, tmp_path: Path):
    fake, open_document = wired
    client = FakeClient({0: "", 1: "", 2: ""})

    outcome = await acquire.scrape_to_disk(
        key="empty",
        container=tmp_path / "book.epub",
        data_dir=tmp_path,
        settings=OcrSettings(),
        client=client,
        open_document=open_document,
        driver_overrides=FAST,
        max_consecutive_stale=50,
        max_consecutive_empty=50,
    )

    assert outcome.text == ""
    assert outcome.output_path is None
    assert outcome.page_count == 3
    assert outcome.ended_by == "stale"
    assert not _paths.text_path_for(tmp_path, "empty").exists()
    assert not _paths.sidecar_path_for(tmp_path, "empty").exists()


async def test_only_the_cache_paths_are_written_during_a_capture(wired, tmp_path: Path, monkeypatch):
    """SC-007: no rendered page may reach the disk."""
    _, open_document = wired
    written: list[str] = []

    real_replace = os.replace
    real_write_text = Path.write_text

    def spy_replace(src, dst, *args, **kwargs):
        written.append(str(dst))
        return real_replace(src, dst, *args, **kwargs)

    def spy_write_text(self, *args, **kwargs):
        written.append(str(self))
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(Path, "write_text", spy_write_text)

    await acquire.scrape_to_disk(
        key="k3",
        container=tmp_path / "book.epub",
        data_dir=tmp_path,
        settings=OcrSettings(),
        client=FakeClient(),
        open_document=open_document,
        driver_overrides=FAST,
        max_consecutive_stale=2,
    )

    allowed_dir = _paths.extracted_dir(tmp_path)
    assert written, "the capture should have published a cache"
    strays = [path for path in written if Path(path).parent != allowed_dir]
    assert not strays, f"a capture wrote outside its cache directory: {strays}"
    assert any(path.endswith("k3.txt") for path in written)
    assert any(path.endswith("k3.json") for path in written)


async def test_blank_frames_are_skipped_and_do_not_become_pages(wired, tmp_path: Path):
    fake, open_document = wired
    fake.frames = [b"BLANK", b"p1", b"p2"]
    client = FakeClient()

    outcome = await acquire.scrape_to_disk(
        key="k4",
        container=tmp_path / "book.epub",
        data_dir=tmp_path,
        settings=OcrSettings(),
        client=client,
        open_document=open_document,
        driver_overrides={**FAST, "settle_timeout": 60.0},
        max_consecutive_stale=2,
    )

    assert outcome.page_count == 2
    assert outcome.text == "page 0 text\n\npage 1 text"


async def test_suspect_pages_are_recorded_from_the_quality_metric(wired, tmp_path: Path):
    _, open_document = wired
    client = FakeClient({0: "clean text", 1: "\x01\x02\x03\x04\x05", 2: "more clean text"})

    outcome = await acquire.scrape_to_disk(
        key="k5",
        container=tmp_path / "book.epub",
        data_dir=tmp_path,
        settings=OcrSettings(),
        client=client,
        open_document=open_document,
        driver_overrides=FAST,
        max_consecutive_stale=2,
    )

    assert outcome.suspect_pages == [1]
    assert outcome.ocr["quality_min"] == 0.0
    assert outcome.ocr["quality_floor"] == acquire.QUALITY_FLOOR


async def test_local_fallback_is_recorded_in_the_sidecar(wired, tmp_path: Path):
    _, open_document = wired
    outcome = await acquire.scrape_to_disk(
        key="k6",
        container=tmp_path / "book.epub",
        data_dir=tmp_path,
        settings=OcrSettings(),
        client=FakeClient(remote=False),
        open_document=open_document,
        driver_overrides=FAST,
        max_consecutive_stale=2,
    )
    assert outcome.ocr["remote"] is False


async def test_a_prefix_or_an_open_document_is_required(tmp_path: Path):
    with pytest.raises(ValueError, match="prefix or an injected open_document"):
        await acquire.scrape_to_disk(
            key="k7",
            container=tmp_path / "book.epub",
            data_dir=tmp_path,
            settings=OcrSettings(),
            open_document=None,
            prefix=None,
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", 1.0), ("plain words", 1.0), ("100% of $5 (approx.)", 1.0), ("\x01\x02\x03", 0.0)],
)
def test_text_quality_is_the_printable_ratio(text: str, expected: float):
    assert text_quality(text) == pytest.approx(expected)


def test_quality_floor_is_a_fraction():
    assert 0.0 < acquire.QUALITY_FLOOR <= 1.0
