"""Version 1: stored ZIP of numbered PNGs + manifest; JSON results by page ID.

Vendored from ``marginalia-plugin-kindle`` (see ``acsm/VENDORED_FROM``); the
protocol version must stay equal to upstream's, asserted by
``tests/unit/test_vendored_protocol.py``.

Archives are decoded in memory, never extracted. Manifest order is authoritative;
filenames alone must never be used to infer reading order.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .ocr_config import MAX_BYTES, MAX_PAGES

if TYPE_CHECKING:
    from .ocr_config import OcrSettings

PROTOCOL = 1


class OcrProtocolError(ValueError):
    """Invalid or incompatible batch; callers must not accept partial results."""


class OcrVersionError(OcrProtocolError):
    """Client and server disagree about the OCR protocol or model identity."""


@dataclass(frozen=True)
class PageImage:
    page_id: int
    png: bytes


def _check_ids(ids: list[int]) -> None:
    if not 1 <= len(ids) <= MAX_PAGES or any(type(i) is not int or i < 0 for i in ids):
        raise OcrProtocolError(f"Batch requires 1..{MAX_PAGES} nonnegative integer page IDs")
    if len(set(ids)) != len(ids):
        raise OcrProtocolError("Duplicate page IDs")


def encode_batch(pages: list[PageImage], settings: OcrSettings) -> bytes:
    ids = [p.page_id for p in pages]
    _check_ids(ids)
    if sum(len(p.png) for p in pages) > MAX_BYTES:
        raise OcrProtocolError("Batch exceeds 32 MiB")
    manifest = {
        "protocol": PROTOCOL,
        "model": settings.model,
        "model_version": settings.version,
        "pages": ids,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for page in pages:
            archive.writestr(f"{page.page_id:09d}.png", page.png)
    payload = buf.getvalue()
    if len(payload) > MAX_BYTES:
        raise OcrProtocolError("Batch exceeds 32 MiB including manifest")
    return payload


def decode_batch(payload: bytes, settings: OcrSettings) -> list[PageImage]:
    if len(payload) > MAX_BYTES:
        raise OcrProtocolError("Batch exceeds 32 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if (
                len(members) > MAX_PAGES + 1
                or len({m.filename for m in members}) != len(members)
                or any(m.compress_type != zipfile.ZIP_STORED for m in members)
                or sum(m.file_size for m in members) > MAX_BYTES
            ):
                raise OcrProtocolError("Invalid archive members or size")
            if archive.getinfo("manifest.json").file_size > 4096:
                raise OcrProtocolError("Manifest exceeds 4 KiB")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict):
                raise OcrProtocolError("Manifest must be an object")
            if (
                type(manifest.get("protocol")) is not int
                or manifest["protocol"] != PROTOCOL
                or manifest.get("model") != settings.model
                or manifest.get("model_version") != settings.version
            ):
                raise OcrVersionError("OCR protocol/model version mismatch")
            ids = manifest.get("pages")
            if not isinstance(ids, list):
                raise OcrProtocolError("Manifest pages must be a list")
            _check_ids(ids)
            expected = {"manifest.json", *(f"{i:09d}.png" for i in ids)}
            if {m.filename for m in members} != expected:
                raise OcrProtocolError("Archive does not match manifest")
            return [PageImage(i, archive.read(f"{i:09d}.png")) for i in ids]
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeError, RuntimeError, RecursionError) as exc:
        raise OcrProtocolError("Malformed OCR archive") from exc


def health_ready(value: Any, settings: OcrSettings) -> bool:
    if not isinstance(value, dict):
        raise OcrProtocolError("Invalid health response")
    if (
        type(value.get("protocol")) is not int
        or value.get("protocol") != PROTOCOL
        or value.get("model") != settings.model
        or value.get("model_version") != settings.version
    ):
        raise OcrVersionError("OCR protocol/model version mismatch")
    return value.get("warm") is True and str(value.get("device", "")).startswith("cuda")


def decode_results(value: Any, pages: list[PageImage], settings: OcrSettings) -> list[str]:
    if not isinstance(value, dict) or (
        type(value.get("protocol")) is not int
        or value.get("protocol") != PROTOCOL
        or value.get("model") != settings.model
        or value.get("model_version") != settings.version
    ):
        raise OcrVersionError("OCR result protocol/model version mismatch")
    results = value.get("results")
    if not isinstance(results, list):
        raise OcrProtocolError("Missing OCR results")
    by_id = {}
    for result in results:
        if (
            not isinstance(result, dict)
            or type(result.get("page_id")) is not int
            or not isinstance(result.get("text"), str)
            or result["page_id"] in by_id
        ):
            raise OcrProtocolError("Invalid or duplicate OCR result")
        by_id[result["page_id"]] = result["text"]
    if set(by_id) != {p.page_id for p in pages}:
        raise OcrProtocolError("Missing or unexpected OCR page IDs")
    return [by_id[p.page_id] for p in pages]
