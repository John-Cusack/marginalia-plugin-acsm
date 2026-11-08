"""acsm.check_document and acsm.list_documents — inspection without work."""

from __future__ import annotations

from pathlib import Path

from acsm import _paths
from acsm.tools import check_document, list_documents


class Ctx:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class FakeIngestion:
    def __init__(self, *, existing=None, fail: bool = False) -> None:
        self._existing = existing or []
        self.fail = fail
        self.calls = 0

    async def find_existing(self, *, source=None, source_pattern=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("database is down")
        return list(self._existing)


def publish(
    data_dir: Path,
    key: str,
    *,
    ended_by: str = "observed_end",
    reviewed: bool = False,
    suspect: list[int] | None = None,
    scraped_at: str | None = None,
) -> None:
    record = _paths.build_sidecar(
        key=key,
        key_source="rights_metadata",
        title=f"Title {key}",
        page_count=3,
        ended_by=ended_by,
        restarts=0,
        ocr={"remote": True, "quality_min": 0.5, "quality_mean": 0.97},
        suspect_pages=suspect or [],
        container="/prefix/library/book.pdf",
        reviewed=reviewed,
    )
    if scraped_at:
        record["scraped_at"] = scraped_at
    _paths.write_cache(data_dir, key, "text", record)


async def test_unfulfilled_document_is_reported_without_error(tmp_path: Path):
    result = await check_document.handler(document_key="urn:uuid:absent", context=Ctx(tmp_path))

    assert result["status"] == "success"
    assert result["state"] == "unfulfilled"
    assert result["on_disk"] is False
    assert result["partial"] is False
    assert result["capture"] is None
    assert result["ingested"] is False


async def test_unreviewed_partial_capture_surfaces_quality_and_the_partial_flag(tmp_path: Path):
    key = "urn:uuid:partial"
    publish(tmp_path, key, ended_by="stale", suspect=[1, 2])

    result = await check_document.handler(document_key=key, context=Ctx(tmp_path))

    assert result["state"] == "captured_unreviewed"
    assert result["partial"] is True
    assert result["on_disk"] is True
    assert result["capture"]["ended_by"] == "stale"
    assert result["quality"]["suspect_pages"] == [1, 2]
    assert result["quality"]["reviewed"] is False
    assert result["disk"]["size_bytes"] == 4


async def test_reviewed_capture_moves_state(tmp_path: Path):
    key = "urn:uuid:reviewed"
    publish(tmp_path, key, reviewed=True)
    result = await check_document.handler(document_key=key, context=Ctx(tmp_path))
    assert result["state"] == "captured_reviewed"
    assert result["partial"] is False


async def test_ingested_document_wins_the_state(tmp_path: Path):
    key = "urn:uuid:ingested"
    publish(tmp_path, key)
    ingestion = FakeIngestion(existing=[{"document_id": "doc-9", "title": "Title"}])

    result = await check_document.handler(
        document_key=key, context=Ctx(tmp_path), ingestion=ingestion
    )

    assert result["state"] == "ingested"
    assert result["ingested"] is True
    assert result["documents"][0]["document_id"] == "doc-9"
    assert ingestion.calls == 1


async def test_a_corpus_failure_is_a_warning_not_an_error(tmp_path: Path):
    key = "urn:uuid:dbfail"
    publish(tmp_path, key)

    result = await check_document.handler(
        document_key=key, context=Ctx(tmp_path), ingestion=FakeIngestion(fail=True)
    )

    assert result["status"] == "success"
    assert result["ingested"] is False
    assert "Corpus lookup failed" in result["warning"]
    assert result["on_disk"] is True  # the disk answer is still useful


async def test_list_reports_rows_newest_first_and_flags_unreadable_sidecars(tmp_path: Path):
    publish(tmp_path, "urn:old", scraped_at="2026-01-01T00:00:00+00:00")
    publish(tmp_path, "urn:new", scraped_at="2026-06-01T00:00:00+00:00")

    folder = _paths.extracted_dir(tmp_path)
    (folder / "urn:broken.json").write_text("{not json", encoding="utf-8")

    result = await list_documents.handler(include_ingested=False, context=Ctx(tmp_path))

    assert result["status"] == "success"
    assert result["count"] == 3
    keys = [row["document_key"] for row in result["documents"]]
    assert keys[0] == "urn:new"
    assert keys[1] == "urn:old"
    broken = next(row for row in result["documents"] if row["document_key"] == "urn:broken")
    assert broken["unreadable"] is True


async def test_list_joins_the_corpus_only_when_asked(tmp_path: Path):
    publish(tmp_path, "urn:one")
    ingestion = FakeIngestion(existing=[{"document_id": "doc-1"}])

    without = await list_documents.handler(
        include_ingested=False, context=Ctx(tmp_path), ingestion=ingestion
    )
    assert without["documents"][0]["ingested"] is False
    assert ingestion.calls == 0

    with_corpus = await list_documents.handler(
        include_ingested=True, context=Ctx(tmp_path), ingestion=ingestion
    )
    assert with_corpus["documents"][0]["ingested"] is True
    assert with_corpus["documents"][0]["document_id"] == "doc-1"


async def test_list_reports_partial_and_complete_flags(tmp_path: Path):
    publish(tmp_path, "urn:done", ended_by="observed_end")
    publish(tmp_path, "urn:part", ended_by="empty")

    result = await list_documents.handler(include_ingested=False, context=Ctx(tmp_path))
    rows = {row["document_key"]: row for row in result["documents"]}

    assert rows["urn:done"]["complete"] is True and rows["urn:done"]["partial"] is False
    assert rows["urn:part"]["partial"] is True and rows["urn:part"]["complete"] is False


async def test_list_on_an_empty_data_dir_is_an_empty_list(tmp_path: Path):
    result = await list_documents.handler(context=Ctx(tmp_path))
    assert result == {"status": "success", "count": 0, "documents": []}
