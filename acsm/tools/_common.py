"""Shared helpers for the pack's tool handlers.

Everything here exists to keep the five handlers uniform in three respects: how
the prefix and OCR settings are resolved, how a container is located from a
document key, and how failures are classified into the response envelope's
``status`` values (``contracts/mcp-tools.md``).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import structlog

from .. import _paths
from ..ocr_config import OcrSettings

log = structlog.get_logger(__name__)

# The Wine prefix is operator state, never plugin data (design doc: "the Wine
# prefix is operator state"). One setting, so setup and fulfillment cannot
# disagree about where it is.
DEFAULT_PREFIX = Path.home() / ".local/share/acsm/wineprefix"

STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_ACTION = "operator_action_required"
STATUS_ALREADY = "already_ingested"


def prefix_from_env(environ: dict[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    configured = env.get("ACSM_WINEPREFIX")
    return Path(configured).expanduser() if configured else DEFAULT_PREFIX


def settings_from_env() -> OcrSettings:
    """OCR settings, falling back to defaults when the environment is invalid."""
    try:
        return OcrSettings.from_env()
    except ValueError as exc:
        log.warning("ocr_config_fallback", reason=type(exc).__name__)
        return OcrSettings()


def error(error_type: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": STATUS_ERROR, "error_type": error_type, "message": message, **extra}


def action_required(action: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": STATUS_ACTION, "action": action, "detail": message, **extra}


def missing_tool() -> str | None:
    """Name the first missing external tool, for dependency failures."""
    for name in ("wine", "Xvfb", "xdotool"):
        if shutil.which(name) is None:
            return name
    if shutil.which("import") is None and shutil.which("xwd") is None:
        return "import (ImageMagick)"
    return None


def classify_runtime_failure(exc: BaseException) -> dict[str, Any]:
    """Map an exception from the reader path onto the response envelope."""
    name = type(exc).__name__
    message = str(exc)
    missing = missing_tool()
    if missing:
        return action_required(
            "research-engine-acsm-setup",
            f"{message} (missing external tool: {missing})",
        )
    if isinstance(exc, (TimeoutError, OSError, RuntimeError, ValueError)):
        return error("reader", message)
    return error(name, message)


def resolve_container(
    data_dir: Path, prefix: Path, key: str, *, sidecar: dict[str, Any] | None = None
) -> Path | None:
    """Find the fulfilled container for a key: the sidecar first, then a search.

    A missing sidecar or a moved container is not an error — it means "look
    again", never "captured".
    """
    from ..fulfill import find_container

    recorded = (sidecar or {}).get("container")
    if recorded:
        candidate = Path(recorded)
        if candidate.is_file():
            return candidate
    return find_container(prefix, key)


def cache_state(data_dir: Path, key: str) -> tuple[dict[str, Any] | None, Path | None]:
    """The sidecar and the cached text path, when they exist."""
    sidecar = _paths.read_sidecar(data_dir, key)
    text_path = _paths.text_path_for(data_dir, key)
    return sidecar, text_path if text_path.exists() else None


async def ingested_documents(ingestion: Any, key: str) -> tuple[list[dict[str, Any]], str | None]:
    """Corpus documents for a key, plus a warning when the lookup failed."""
    if ingestion is None:
        return [], None
    try:
        return await ingestion.find_existing(source_pattern=key), None
    except Exception as exc:  # pragma: no cover - adapter-specific
        log.warning("corpus_lookup_failed", key=key, reason=type(exc).__name__)
        return [], f"Corpus lookup failed: {exc}"


def document_state(
    *, has_sidecar: bool, has_text: bool, reviewed: bool, ingested: bool
) -> str:
    """data-model.md's state table, in one place."""
    if not has_sidecar and not has_text:
        return "unfulfilled" if not ingested else "ingested"
    if ingested:
        return "ingested"
    return "captured_reviewed" if reviewed else "captured_unreviewed"
