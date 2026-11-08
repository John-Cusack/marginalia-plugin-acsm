"""acsm.fulfill_document — an .acsm file to a fulfilled, identified container."""

from __future__ import annotations

import shutil
from pathlib import Path

import structlog
from research_engine_sdk import tool

from .. import _paths
from ..capture import Display
from ..fulfill import FulfillError, fulfill
from . import _common

log = structlog.get_logger(__name__)

DESCRIPTION = (
    "Open an .acsm fulfillment file with Adobe Digital Editions under Wine and "
    "return the fulfilled container's stable document key. Requires an "
    "activated Wine prefix; activation is performed once, interactively, by "
    "the operator."
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "acsm_path": {
            "type": "string",
            "description": "Path to the .acsm file.",
        },
        "loan_expires_at": {
            "type": "string",
            "description": "Optional ISO 8601 expiry date for this loan, recorded in metadata.",
        },
        "force_refill": {
            "type": "boolean",
            "default": False,
            "description": "Re-open the .acsm even if a fulfilled container is already known.",
        },
    },
    "required": ["acsm_path"],
}


@tool(id="acsm.fulfill_document", description=DESCRIPTION, input_schema=INPUT_SCHEMA)
async def handler(
    acsm_path: str,
    loan_expires_at: str | None = None,
    force_refill: bool = False,
    context=None,
    ingestion=None,
    **_clients,
) -> dict:
    source = Path(acsm_path).expanduser()
    if not source.is_file():
        return _common.error("config", f"{source} is not a file")

    data_dir: Path = context.data_dir if context is not None else _common.DEFAULT_PREFIX.parent
    prefix = _common.prefix_from_env()
    inbox = _paths.inbox_dir(data_dir)
    inbox.mkdir(parents=True, exist_ok=True)

    # Stage a copy so the operator's file can move afterwards; the .acsm is a
    # token, not a document, and re-opening it later is a deliberate act.
    staged = inbox / source.name
    if not staged.exists() or staged.stat().st_size != source.stat().st_size:
        shutil.copy2(source, staged)

    display = Display()
    try:
        async with display:
            fulfilled = await fulfill(staged, prefix=prefix, display=display)
    except FulfillError as exc:
        action = getattr(exc, "action", None) or "research-engine-acsm-setup"
        return _common.action_required(action, str(exc), acsm_path=str(source))
    except Exception as exc:
        return _common.classify_runtime_failure(exc)

    documents, warning = await _common.ingested_documents(ingestion, fulfilled.key)
    result = {
        "status": _common.STATUS_SUCCESS,
        "document_key": fulfilled.key,
        "key_source": fulfilled.key_source,
        "container": str(fulfilled.container),
        "title": fulfilled.title,
        "staged_acsm": str(staged),
        "already_fulfilled": False,
        "prefix": str(prefix),
        "ingested": bool(documents),
    }
    if loan_expires_at:
        result["loan_expires_at"] = loan_expires_at
    if warning:
        result["warning"] = warning
    return result
