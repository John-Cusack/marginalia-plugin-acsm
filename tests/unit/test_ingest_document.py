"""acsm.ingest_document — idempotency, metadata, and the config-error paths."""

from __future__ import annotations

from pathlib import Path

from acsm import _paths
from acsm.acquire import ScrapeOutcome
from acsm.tools import ingest_document


class Ctx:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.plugin_id = "acsm"


class FakeIngestion:
    def __init__(self, *, existing=None, result=None) -> None:
        self._existing = existing or []
        self._result = result or {"document_id": "doc-new", "passage_count": 42}
        self.find_calls: list[dict] = []
        self.ingest_calls: list[dict] = []

    async def find_existing(self, *, source=None, source_pattern=None):
        self.find_calls.append({"source": source, "source_pattern": source_pattern})
        return list(self._existing)

    async def ingest_document(self, **kwargs):
        self.ingest_calls.append(kwargs)
        return dict(self._result)


def cache_text(data_dir: Path, key: str, text: str = "Body text of the book.") -> Path:
    record = _paths.build_sidecar(
        key=key,
        key_source="rights_metadata",
        title="A Book",
        page_count=2,
        ended_by="observed_end",
        restarts=0,
        ocr={"model": "english_g2", "remote": True},
        suspect_pages=[1],
        container="/prefix/library/book.pdf",
    )
    return _paths.write_cache(data_dir, key, text, record)


async def test_already_ingested_short_circuits_before_any_work(tmp_path: Path, monkeypatch):
    key = "urn:uuid:existing"
    cache_text(tmp_path, key)
    fake = FakeIngestion(existing=[{"document_id": "doc-1", "title": "Existing"}])

    def forbidden(*args, **kwargs):
        raise AssertionError("an already-ingested document must not be re-captured")

    monkeypatch.setattr(ingest_document, "scrape_to_disk", forbidden)

    result = await ingest_document.handler(
        document_key=key, context=Ctx(tmp_path), ingestion=fake
    )

    assert result["status"] == "already_ingested"
    assert result["document_id"] == "doc-1"
    assert fake.ingest_calls == []
    assert fake.find_calls == [{"source": str(_paths.text_path_for(tmp_path, key).resolve()),
                                "source_pattern": None}]


async def test_cached_text_is_ingested_with_the_full_contract(tmp_path: Path):
    key = "urn:uuid:new"
    path = cache_text(tmp_path, key, "First paragraph.\n\nSecond paragraph.")
    fake = FakeIngestion()

    result = await ingest_document.handler(
        document_key=key,
        title="Override Title",
        edition_key="978-0-000000-00-0",
        reviewed=True,
        context=Ctx(tmp_path),
        ingestion=fake,
    )

    assert result["status"] == "success"
    assert result["document_id"] == "doc-new"
    assert len(fake.ingest_calls) == 1
    call = fake.ingest_calls[0]
    assert call["document_type"] == "drm_pdf"
    assert call["source"] == str(path.resolve())
    assert call["text"] == path.read_text(encoding="utf-8")  # handed over unmodified
    assert call["language"] == "en"
    assert call["title"] == "Override Title"

    metadata = call["metadata"]
    assert metadata["acsm_key"] == key
    assert metadata["key_source"] == "rights_metadata"
    assert metadata["page_count"] == 2
    assert metadata["edition_key"] == "978-0-000000-00-0"
    assert metadata["reviewed"] is True
    assert metadata["source_format"] == "adobe_drm"
    assert metadata["complete"] is True
    assert metadata["container"] == "/prefix/library/book.pdf"


async def test_review_flag_defaults_to_false_and_is_never_forced(tmp_path: Path):
    key = "urn:uuid:unreviewed"
    cache_text(tmp_path, key)
    fake = FakeIngestion()

    result = await ingest_document.handler(document_key=key, context=Ctx(tmp_path), ingestion=fake)

    assert result["reviewed"] is False
    assert fake.ingest_calls[0]["metadata"]["reviewed"] is False


async def test_force_reingest_skips_the_idempotency_lookup(tmp_path: Path):
    key = "urn:uuid:forced"
    cache_text(tmp_path, key)
    fake = FakeIngestion(existing=[{"document_id": "doc-1"}])

    result = await ingest_document.handler(
        document_key=key, force_reingest=True, context=Ctx(tmp_path), ingestion=fake
    )

    assert result["status"] == "success"
    assert fake.find_calls == []
    assert len(fake.ingest_calls) == 1


async def test_unavailable_ingestion_client_is_a_config_error(tmp_path: Path):
    result = await ingest_document.handler(document_key="k", context=Ctx(tmp_path), ingestion=None)
    assert result["status"] == "error"
    assert result["error_type"] == "config"
    assert "permissions.ingest" in result["message"]


async def test_no_cache_and_no_container_tells_the_operator_to_fulfill(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ingest_document._common, "resolve_container", lambda *a, **k: None)
    result = await ingest_document.handler(
        document_key="k", context=Ctx(tmp_path), ingestion=FakeIngestion()
    )
    assert result["status"] == "error"
    assert result["error_type"] == "config"
    assert "acsm.fulfill_document" in result["message"]


async def test_whitespace_only_cached_text_is_refused(tmp_path: Path):
    key = "urn:uuid:blank"
    path = _paths.text_path_for(tmp_path, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("   \n\n  ", encoding="utf-8")
    fake = FakeIngestion()

    result = await ingest_document.handler(document_key=key, context=Ctx(tmp_path), ingestion=fake)

    assert result["status"] == "error"
    assert result["error_type"] == "empty_text"
    assert fake.ingest_calls == []


async def test_a_capture_that_produces_no_text_is_reported_not_ingested(tmp_path: Path, monkeypatch):
    key = "urn:uuid:nothing"
    monkeypatch.setattr(
        ingest_document._common, "resolve_container", lambda *a, **k: tmp_path / "book.pdf"
    )

    async def empty_scrape(**kwargs):
        return ScrapeOutcome(key=key, page_count=7, ended_by="empty")

    monkeypatch.setattr(ingest_document, "scrape_to_disk", empty_scrape)
    fake = FakeIngestion()

    result = await ingest_document.handler(document_key=key, context=Ctx(tmp_path), ingestion=fake)

    assert result["status"] == "error"
    assert result["error_type"] == "empty_text"
    assert result["page_count"] == 7
    assert result["ended_by"] == "empty"
    assert fake.ingest_calls == []


async def test_capture_is_run_when_no_cache_exists(tmp_path: Path, monkeypatch):
    key = "urn:uuid:capture-first"
    monkeypatch.setattr(
        ingest_document._common, "resolve_container", lambda *a, **k: tmp_path / "book.pdf"
    )
    captured: list[dict] = []

    async def scrape(**kwargs):
        captured.append(kwargs)
        # A real capture publishes the cache; the handler re-reads it.
        cache_text(tmp_path, key, "Freshly captured text.")
        return ScrapeOutcome(
            key=key,
            title="A Book",
            text="Freshly captured text.",
            page_count=2,
            ended_by="observed_end",
            output_path=_paths.text_path_for(tmp_path, key),
        )

    monkeypatch.setattr(ingest_document, "scrape_to_disk", scrape)
    fake = FakeIngestion()

    result = await ingest_document.handler(document_key=key, context=Ctx(tmp_path), ingestion=fake)

    assert captured and captured[0]["key"] == key
    assert result["status"] == "success"
    assert fake.ingest_calls[0]["text"] == "Freshly captured text."


async def test_capture_failure_is_classified_as_an_operator_action(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        ingest_document._common, "resolve_container", lambda *a, **k: tmp_path / "book.pdf"
    )

    async def explode(**kwargs):
        raise RuntimeError("missing external tool")

    monkeypatch.setattr(ingest_document, "scrape_to_disk", explode)
    monkeypatch.setattr(ingest_document._common, "missing_tool", lambda: "xdotool")

    result = await ingest_document.handler(
        document_key="k", context=Ctx(tmp_path), ingestion=FakeIngestion()
    )

    assert result["status"] == "operator_action_required"
    assert result["action"] == "research-engine-acsm-setup"
    assert "xdotool" in result["detail"]
