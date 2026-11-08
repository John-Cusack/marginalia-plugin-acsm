"""acsm.list_documents — everything the pack knows, joined with the corpus."""

from __future__ import annotations

import json
from pathlib import Path

import structlog
from research_engine_sdk import tool

from .. import _paths
from . import _common

log = structlog.get_logger(__name__)

DESCRIPTION = (
    "List documents known to the pack, joining the local cache with matching "
    "corpus documents."
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "include_ingested": {
            "type": "boolean",
            "default": True,
            "description": "If true, look up matching corpus documents.",
        },
    },
}


@tool(id="acsm.list_documents", description=DESCRIPTION, input_schema=INPUT_SCHEMA)
async def handler(include_ingested: bool = True, context=None, ingestion=None, **_clients) -> dict:
    data_dir: Path = context.data_dir
    folder = _paths.extracted_dir(data_dir)
    rows: list[dict] = []

    sidecars = sorted(folder.glob("*.json")) if folder.is_dir() else []
    for sidecar_path in sidecars:
        key = sidecar_path.stem
        try:
            raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A cache the pack cannot read is exactly what the operator needs
            # to know about, so it is listed rather than skipped.
            rows.append({"document_key": key, "unreadable": True})
            continue
        if not isinstance(raw, dict) or raw.get("schema") != _paths.SIDECAR_SCHEMA:
            rows.append({"document_key": key, "unreadable": True})
            continue
        text_path = _paths.text_path_for(data_dir, key)
        ended_by = raw.get("ended_by")
        row = {
            "document_key": key,
            "title": raw.get("title"),
            "page_count": raw.get("page_count"),
            "ended_by": ended_by,
            "complete": ended_by == "observed_end",
            "partial": ended_by in {"stale", "empty", "cancelled"},
            "reviewed": bool(raw.get("reviewed")),
            "characters": text_path.stat().st_size if text_path.exists() else 0,
            "scraped_at": raw.get("scraped_at"),
            "output_path": str(text_path) if text_path.exists() else None,
            "ingested": False,
        }
        if include_ingested and ingestion is not None:
            documents, _ = await _common.ingested_documents(ingestion, key)
            row["ingested"] = bool(documents)
            if documents:
                row["document_id"] = documents[0].get("document_id")
        rows.append(row)

    rows.sort(key=lambda row: str(row.get("scraped_at") or ""), reverse=True)
    return {"status": _common.STATUS_SUCCESS, "count": len(rows), "documents": rows}
