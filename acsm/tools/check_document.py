"""acsm.check_document — one document's fulfillment, cache, quality and corpus state."""

from __future__ import annotations

from pathlib import Path

import structlog
from research_engine_sdk import tool

from . import _common

log = structlog.get_logger(__name__)

DESCRIPTION = (
    "Report fulfillment, cache, review, capture-quality and corpus state for "
    "one document key."
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "document_key": {"type": "string", "description": "The document key."},
    },
    "required": ["document_key"],
}


@tool(id="acsm.check_document", description=DESCRIPTION, input_schema=INPUT_SCHEMA)
async def handler(document_key: str, context=None, ingestion=None, **_clients) -> dict:
    data_dir: Path = context.data_dir
    sidecar, text_path = _common.cache_state(data_dir, document_key)
    documents, warning = await _common.ingested_documents(ingestion, document_key)
    state = _common.document_state(
        has_sidecar=sidecar is not None,
        has_text=text_path is not None,
        reviewed=bool((sidecar or {}).get("reviewed")),
        ingested=bool(documents),
    )

    disk = None
    if text_path is not None:
        stat = text_path.stat()
        disk = {"path": str(text_path), "size_bytes": stat.st_size, "modified_at": stat.st_mtime}

    ended_by = (sidecar or {}).get("ended_by")
    result = {
        "status": _common.STATUS_SUCCESS,
        "document_key": document_key,
        "state": state,
        # A partial capture is a flag that never clears by itself.
        "partial": ended_by in {"stale", "empty", "cancelled"},
        "on_disk": text_path is not None,
        "disk": disk,
        "capture": (
            {
                "title": sidecar.get("title"),
                "container": sidecar.get("container"),
                "page_count": sidecar.get("page_count"),
                "ended_by": ended_by,
                "restarts": sidecar.get("restarts"),
                "scraped_at": sidecar.get("scraped_at"),
                "key_source": sidecar.get("key_source"),
            }
            if sidecar
            else None
        ),
        "quality": (
            {
                "reviewed": bool(sidecar.get("reviewed")),
                "suspect_pages": sidecar.get("suspect_pages", []),
                **(sidecar.get("ocr") or {}),
            }
            if sidecar
            else None
        ),
        "ingested": bool(documents),
        "documents": documents,
    }
    if warning:
        # The disk-side answer is still useful, so a corpus failure is a warning.
        result["warning"] = warning
    return result
