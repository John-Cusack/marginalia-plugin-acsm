"""Filesystem paths and the cache writer.

All mutable state lives under the injected ``PluginContext.data_dir``
(``~/.research-engine/plugin-data/acsm``); nothing here hard-codes a home
directory. The Wine prefix is deliberately *not* here: it holds licensed
content and an activation, so it is operator state, passed in as a path.

The cache write is atomic (research D5): a partially written text file would be
indistinguishable from a complete one to the idempotency lookup and would be
ingested as canonical text.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Sidecar schema version. A sidecar claiming a newer schema is reported as
# unreadable rather than partially interpreted (VR-006).
SIDECAR_SCHEMA = 1

# A key must be usable as a filename component and stable enough to be an
# identity (VR-001). Rights-metadata identifiers are URNs, so the character
# set is deliberately permissive but bounded; anything else falls back to a
# content hash at the call site in fulfill.py.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def inbox_dir(data_dir: Path) -> Path:
    """Staged copies of operator-supplied .acsm files."""
    return Path(data_dir) / "inbox"


def extracted_dir(data_dir: Path) -> Path:
    """Captured text and capture sidecars."""
    return Path(data_dir) / "extracted"


def text_path_for(data_dir: Path, key: str) -> Path:
    return extracted_dir(data_dir) / f"{key}.txt"


def sidecar_path_for(data_dir: Path, key: str) -> Path:
    return extracted_dir(data_dir) / f"{key}.json"


def is_valid_key(key: str) -> bool:
    """VR-001: could this string be used as a document key?"""
    return bool(key) and bool(_KEY_RE.match(key))


def read_sidecar(data_dir: Path, key: str) -> dict[str, Any] | None:
    """Load a capture sidecar.

    Returns ``None`` for a missing, unreadable, malformed, or newer-schema
    file. Callers treat ``None`` as "not captured yet", never as an error: a
    capture is expensive and must be restartable.
    """
    path = sidecar_path_for(data_dir, key)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("sidecar_unreadable", key=key, reason=type(exc).__name__)
        return None
    if not isinstance(raw, dict):
        log.warning("sidecar_not_an_object", key=key)
        return None
    schema = raw.get("schema")
    if schema != SIDECAR_SCHEMA:
        log.warning("sidecar_schema_unsupported", key=key, schema=schema)
        return None
    return raw


def build_sidecar(
    *,
    key: str,
    key_source: str,
    title: str,
    page_count: int,
    ended_by: str,
    restarts: int,
    ocr: dict[str, Any],
    suspect_pages: list[int],
    container: str | None = None,
    reviewed: bool = False,
    reader_version: str | None = None,
    wine_version: str | None = None,
    tools: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a sidecar record (data-model.md, VR-003, VR-004)."""
    if page_count <= 0:
        raise ValueError("a sidecar exists only for captures that produced pages")
    pages = sorted({int(p) for p in suspect_pages})
    if any(p < 0 or p >= page_count for p in pages):
        raise ValueError("suspect_pages must be page indices within the capture")
    return {
        "schema": SIDECAR_SCHEMA,
        "key": key,
        "key_source": key_source,
        "title": title or "Untitled",
        "container": container,
        "page_count": page_count,
        "ended_by": ended_by,
        "restarts": restarts,
        "suspect_pages": pages,
        "ocr": ocr,
        "reviewed": bool(reviewed),
        "scraped_at": datetime.now(UTC).isoformat(),
        "reader_version": reader_version,
        "wine_version": wine_version,
        "tools": tools or {},
    }


def write_cache(data_dir: Path, key: str, text: str, record: dict[str, Any]) -> Path:
    """Write captured text and its sidecar atomically.

    Both files are staged as temporaries and renamed into place, so an
    interrupted capture leaves either the previous pair or the new pair — never
    a truncated text file (VR-002, SC-008).
    """
    if not text.strip():
        raise ValueError("refusing to cache an empty capture")
    directory = extracted_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)

    text_path = text_path_for(data_dir, key)
    sidecar_path = sidecar_path_for(data_dir, key)
    text_tmp = text_path.with_suffix(".txt.tmp")
    sidecar_tmp = sidecar_path.with_suffix(".json.tmp")

    try:
        text_tmp.write_text(text, encoding="utf-8")
        sidecar_tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(text_tmp, text_path)
        os.replace(sidecar_tmp, sidecar_path)
    finally:
        for leftover in (text_tmp, sidecar_tmp):
            if leftover.exists():
                with suppress(OSError):  # pragma: no cover - best effort cleanup
                    leftover.unlink()
    log.info("cache_written", key=key, chars=len(text), path=str(text_path))
    return text_path
