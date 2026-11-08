"""acsm.scrape_document — cache reuse, force, and the failure envelopes."""

from __future__ import annotations

from pathlib import Path

import pytest

from acsm import _paths
from acsm.acquire import ScrapeOutcome
from acsm.tools import scrape_document


class Ctx:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


def publish(data_dir: Path, key: str, *, ended_by: str = "observed_end") -> Path:
    record = _paths.build_sidecar(
        key=key,
        key_source="rights_metadata",
        title="A Book",
        page_count=3,
        ended_by=ended_by,
        restarts=1,
        ocr={"model": "english_g2", "remote": True, "quality_floor": 0.9},
        suspect_pages=[2],
        container="/prefix/library/book.pdf",
    )
    return _paths.write_cache(data_dir, key, "page one\n\npage two\n\npage three", record)


async def test_existing_cache_is_reused_without_touching_the_reader(tmp_path: Path, monkeypatch):
    key = "urn:uuid:cached"
    publish(tmp_path, key, ended_by="stale")

    def forbidden(*args, **kwargs):
        raise AssertionError("a cached capture must not be re-run without force_rescrape")

    monkeypatch.setattr(scrape_document, "scrape_to_disk", forbidden)

    result = await scrape_document.handler(document_key=key, context=Ctx(tmp_path))

    assert result["status"] == "success"
    assert result["reused_cache"] is True
    assert result["page_count"] == 3
    assert result["ended_by"] == "stale"
    assert result["complete"] is False
    assert result["suspect_pages"] == [2]
    assert result["text_preview"].startswith("page one")


async def test_force_rescrape_runs_the_capture(tmp_path: Path, monkeypatch):
    key = "urn:uuid:forced"
    publish(tmp_path, key)
    called: list[dict] = []

    async def scrape(**kwargs):
        called.append(kwargs)
        return ScrapeOutcome(
            key=key,
            title="A Book",
            text="fresh text",
            page_count=1,
            ended_by="observed_end",
            ocr={"remote": True},
            output_path=_paths.text_path_for(tmp_path, key),
        )

    monkeypatch.setattr(scrape_document, "scrape_to_disk", scrape)
    monkeypatch.setattr(
        scrape_document._common, "resolve_container", lambda *a, **k: tmp_path / "book.pdf"
    )

    result = await scrape_document.handler(
        document_key=key, force_rescrape=True, context=Ctx(tmp_path)
    )

    assert called and called[0]["key"] == key
    assert result["reused_cache"] is False
    assert result["complete"] is True


async def test_a_missing_container_names_the_fulfillment_step(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(scrape_document._common, "resolve_container", lambda *a, **k: None)
    result = await scrape_document.handler(document_key="urn:uuid:absent", context=Ctx(tmp_path))

    assert result["status"] == "error"
    assert result["error_type"] == "config"
    assert "acsm.fulfill_document" in result["message"]


async def test_an_empty_capture_reports_why(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        scrape_document._common, "resolve_container", lambda *a, **k: tmp_path / "book.pdf"
    )

    async def empty(**kwargs):
        return ScrapeOutcome(key="k", page_count=4, ended_by="empty")

    monkeypatch.setattr(scrape_document, "scrape_to_disk", empty)
    result = await scrape_document.handler(document_key="k", context=Ctx(tmp_path))

    assert result["status"] == "error"
    assert result["error_type"] == "empty_text"
    assert (result["page_count"], result["ended_by"]) == (4, "empty")


async def test_reader_failure_becomes_an_operator_action(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        scrape_document._common, "resolve_container", lambda *a, **k: tmp_path / "book.pdf"
    )

    async def boom(**kwargs):
        raise TimeoutError("Xvfb did not create the socket")

    monkeypatch.setattr(scrape_document, "scrape_to_disk", boom)
    monkeypatch.setattr(scrape_document._common, "missing_tool", lambda: "Xvfb")

    result = await scrape_document.handler(document_key="k", context=Ctx(tmp_path))
    assert result["status"] == "operator_action_required"
    assert "Xvfb" in result["detail"]


@pytest.mark.parametrize("ended_by", ["stale", "empty", "max_pages", "cancelled"])
async def test_only_observed_end_is_complete(tmp_path: Path, monkeypatch, ended_by: str):
    monkeypatch.setattr(
        scrape_document._common, "resolve_container", lambda *a, **k: tmp_path / "book.pdf"
    )

    async def scrape(**kwargs):
        return ScrapeOutcome(
            key="k", text="some text", page_count=1, ended_by=ended_by,
            output_path=tmp_path / "k.txt",
        )

    monkeypatch.setattr(scrape_document, "scrape_to_disk", scrape)
    result = await scrape_document.handler(document_key="k", context=Ctx(tmp_path))
    assert result["complete"] is False
