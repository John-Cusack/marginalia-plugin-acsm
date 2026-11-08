"""acsm.scrape_document — drive the reader, OCR the pages, publish the cache."""

from __future__ import annotations

from pathlib import Path

import structlog
from research_engine_sdk import tool

from ..acquire import scrape_to_disk
from . import _common

log = structlog.get_logger(__name__)

DESCRIPTION = (
    "Page through a fulfilled document in Adobe Digital Editions on a private "
    "Xvfb display, OCR every page, and cache the text with a provenance "
    "sidecar. Does NOT ingest into the corpus — use acsm.ingest_document."
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "document_key": {
            "type": "string",
            "description": "The key returned by acsm.fulfill_document.",
        },
        "page_delay_min": {"type": "number", "default": 1.5},
        "page_delay_max": {"type": "number", "default": 4.0},
        "max_pages": {
            "type": ["integer", "null"],
            "default": None,
            "description": "Stop after N pages (null = until the document ends).",
        },
        "max_consecutive_stale": {
            "type": "integer",
            "default": 4,
            "description": (
                "End the capture after this many consecutive turns where the frame "
                "did not change."
            ),
        },
        "max_consecutive_empty": {
            "type": "integer",
            "default": 20,
            "description": (
                "End the capture after this many consecutive pages with no OCR text. "
                "Image inserts and blank pages legitimately raise this."
            ),
        },
        "force_rescrape": {
            "type": "boolean",
            "default": False,
            "description": "Capture again even if cached text already exists.",
        },
    },
    "required": ["document_key"],
}


def _cached_result(key: str, sidecar: dict, text_path: Path, text: str) -> dict:
    return {
        "status": _common.STATUS_SUCCESS,
        "document_key": key,
        "title": sidecar.get("title"),
        "page_count": sidecar.get("page_count"),
        "ended_by": sidecar.get("ended_by"),
        "complete": sidecar.get("ended_by") == "observed_end",
        "restarts": sidecar.get("restarts"),
        "chars": len(text),
        "text_preview": text[:500],
        "suspect_pages": sidecar.get("suspect_pages", []),
        "ocr": sidecar.get("ocr", {}),
        "output_path": str(text_path),
        "reused_cache": True,
    }


@tool(id="acsm.scrape_document", description=DESCRIPTION, input_schema=INPUT_SCHEMA)
async def handler(
    document_key: str,
    page_delay_min: float = 1.5,
    page_delay_max: float = 4.0,
    max_pages: int | None = None,
    max_consecutive_stale: int = 4,
    max_consecutive_empty: int = 20,
    force_rescrape: bool = False,
    context=None,
    **_clients,
) -> dict:
    data_dir = context.data_dir
    prefix = _common.prefix_from_env()
    sidecar, text_path = _common.cache_state(data_dir, document_key)

    if text_path is not None and sidecar is not None and not force_rescrape:
        return _cached_result(
            document_key, sidecar, text_path, text_path.read_text(encoding="utf-8")
        )

    container = _common.resolve_container(data_dir, prefix, document_key, sidecar=sidecar)
    if container is None:
        return _common.error(
            "config",
            f"no fulfilled container found for {document_key}; "
            "call acsm.fulfill_document first",
            document_key=document_key,
        )

    try:
        outcome = await scrape_to_disk(
            key=document_key,
            container=container,
            data_dir=data_dir,
            settings=_common.settings_from_env(),
            title=(sidecar or {}).get("title", ""),
            prefix=prefix,
            page_delay_min=page_delay_min,
            page_delay_max=page_delay_max,
            max_pages=max_pages,
            max_consecutive_stale=max_consecutive_stale,
            max_consecutive_empty=max_consecutive_empty,
        )
    except Exception as exc:
        return _common.classify_runtime_failure(exc)

    if not outcome.text.strip():
        return _common.error(
            "empty_text",
            f"capture produced no text for {document_key}; the reader may not have "
            "rendered, or recognition failed",
            document_key=document_key,
            page_count=outcome.page_count,
            ended_by=outcome.ended_by,
        )

    return {
        "status": _common.STATUS_SUCCESS,
        "document_key": document_key,
        "title": outcome.title,
        "page_count": outcome.page_count,
        "ended_by": outcome.ended_by,
        "complete": outcome.complete,
        "restarts": outcome.restarts,
        "chars": len(outcome.text),
        "text_preview": outcome.text[:500],
        "suspect_pages": outcome.suspect_pages,
        "ocr": outcome.ocr,
        "output_path": str(outcome.output_path),
        "reused_cache": False,
    }
