"""acsm.ingest_document — capture if needed, then ingest into the corpus."""

from __future__ import annotations

from pathlib import Path

import structlog
from research_engine_sdk import tool

from .. import _paths
from ..acquire import QUALITY_FLOOR, scrape_to_disk
from . import _common

log = structlog.get_logger(__name__)

DESCRIPTION = (
    "Capture (if needed) and ingest a fulfilled document into the corpus as "
    "document_type='drm_pdf'. Idempotent: returns the existing document_id "
    "when the same cache path has already been ingested."
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "document_key": {"type": "string", "description": "The document key."},
        "title": {
            "type": "string",
            "description": "Override the title recorded in the capture sidecar.",
        },
        "edition_key": {
            "type": "string",
            "description": (
                "Edition identity for citations, e.g. the print ISBN. Recommended: "
                "loan text is a reading copy, not an authoritative edition."
            ),
        },
        "reviewed": {
            "type": "boolean",
            "default": False,
            "description": "Records that a human checked the captured text.",
        },
        "force_rescrape": {"type": "boolean", "default": False},
        "force_reingest": {
            "type": "boolean",
            "default": False,
            "description": "Ingest a new document even if one exists for this source.",
        },
        "quality_floor": {
            "type": "number",
            "default": 0.9,
            "description": (
                "Pages whose recognized text scores below this printable-character "
                "ratio are listed as suspect in the sidecar. This is a text-quality "
                "proxy: the OCR wire protocol returns no per-page confidence."
            ),
        },
    },
    "required": ["document_key"],
}


@tool(id="acsm.ingest_document", description=DESCRIPTION, input_schema=INPUT_SCHEMA)
async def handler(
    document_key: str,
    title: str | None = None,
    edition_key: str | None = None,
    reviewed: bool = False,
    force_rescrape: bool = False,
    force_reingest: bool = False,
    quality_floor: float = QUALITY_FLOOR,
    context=None,
    ingestion=None,
    **_clients,
) -> dict:
    if ingestion is None:
        return _common.error(
            "config",
            "Ingestion client unavailable; the plugin needs permissions.ingest=true.",
        )

    data_dir: Path = context.data_dir
    prefix = _common.prefix_from_env()
    text_path = _paths.text_path_for(data_dir, document_key)
    source = str(text_path.resolve())

    # Idempotency before any expensive work: a capture can take tens of minutes.
    if not force_reingest:
        existing = await ingestion.find_existing(source=source)
        if existing:
            return {
                "status": _common.STATUS_ALREADY,
                "document_id": existing[0].get("document_id"),
                "document": existing[0],
                "document_key": document_key,
                "source": source,
            }

    sidecar = _paths.read_sidecar(data_dir, document_key)
    if force_rescrape or not text_path.exists():
        container = _common.resolve_container(data_dir, prefix, document_key, sidecar=sidecar)
        if container is None:
            return _common.error(
                "config",
                f"no cached text and no fulfilled container for {document_key}; "
                "call acsm.fulfill_document first",
                document_key=document_key,
            )
        try:
            outcome = await scrape_to_disk(
                key=document_key,
                container=container,
                data_dir=data_dir,
                settings=_common.settings_from_env(),
                title=title or (sidecar or {}).get("title", ""),
                prefix=prefix,
                quality_floor=quality_floor,
            )
        except Exception as exc:
            return _common.classify_runtime_failure(exc)
        if not outcome.text.strip():
            return _common.error(
                "empty_text",
                f"capture produced no text for {document_key}",
                document_key=document_key,
                page_count=outcome.page_count,
                ended_by=outcome.ended_by,
            )
        text = outcome.text
        sidecar = _paths.read_sidecar(data_dir, document_key)
    else:
        text = text_path.read_text(encoding="utf-8")

    if not text.strip():
        return _common.error(
            "empty_text", f"cached text for {document_key} is empty", document_key=document_key
        )

    sidecar = sidecar or {}
    metadata = {
        "acsm_key": document_key,
        "key_source": sidecar.get("key_source", "unknown"),
        "page_count": sidecar.get("page_count"),
        "ocr": sidecar.get("ocr", {}),
        "reviewed": bool(reviewed),
        "source_format": "adobe_drm",
        "ended_by": sidecar.get("ended_by"),
        "complete": sidecar.get("ended_by") == "observed_end",
    }
    if sidecar.get("container"):
        metadata["container"] = sidecar["container"]
    if edition_key:
        metadata["edition_key"] = edition_key

    result = await ingestion.ingest_document(
        title=title or sidecar.get("title") or f"Untitled ({document_key[:8]})",
        document_type="drm_pdf",
        text=text,
        source=source,
        metadata=metadata,
        language="en",
    )

    return {
        "status": _common.STATUS_SUCCESS,
        "document_key": document_key,
        "source": source,
        "chars": len(text),
        "page_count": sidecar.get("page_count"),
        "ended_by": sidecar.get("ended_by"),
        "reviewed": bool(reviewed),
        "suspect_pages": sidecar.get("suspect_pages", []),
        **result,
    }
