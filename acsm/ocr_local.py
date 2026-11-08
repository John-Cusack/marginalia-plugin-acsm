"""Cached local EasyOCR; remote batches live in ``ocr_remote``.

Vendored from ``marginalia-plugin-kindle`` (see ``acsm/VENDORED_FROM``).
The laptop-only path remains the default. Screenshots and crops stay in memory.
"""

from __future__ import annotations

import io
import threading
from typing import Any

import structlog
from PIL import Image

log = structlog.get_logger(__name__)
_reader: Any = None
_reader_lock = threading.RLock()


def _get_reader(*, languages: list[str] | None = None, gpu: bool = True) -> Any:
    """Return a cached EasyOCR reader, constructing it on first call."""
    global _reader
    with _reader_lock:
        if _reader is not None:
            return _reader
        import easyocr

        langs = languages or ["en"]
        log.info("easyocr_init", languages=langs, gpu=gpu)
        _reader = easyocr.Reader(langs, gpu=gpu, verbose=False)
        return _reader


def reset_reader() -> None:
    """Drop the cached reader. Useful in tests."""
    global _reader
    with _reader_lock:
        _reader = None


def ocr_image_bytes(image_bytes: bytes, *, crop: tuple[int, int, int, int] | None = None) -> str:
    """OCR a screenshot. ``crop`` is ``(left, top, right, bottom)`` in pixels."""
    import numpy as np

    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.crop(crop) if crop is not None else source
        arr = np.array(image)
    # Threaded outage fallback preserves the old single-reader serialization.
    with _reader_lock:
        results = _get_reader().readtext(arr, detail=0, paragraph=True)
    return "\n".join(results)
