"""Orchestration: drive the reader, OCR the pages, publish the cache.

This is the only module that wires the three sides together — ``capture`` knows
about X, ``driver`` knows about the loop, the vendored client knows about OCR —
and it is where the honest reporting originates: page count, how the run ended,
reader restarts, and per-page text quality.

OCR is issued per page as the page is captured, so the loop's empty-page counter
is measured on real recognition output. The transport's batching is therefore
unused (research D10): a page turn costs 1.5–4.0 s of settle time, against
50–200 ms of recognition, so per-page requests cost little and buy exact
accounting.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from . import _paths
from . import capture as capture_mod
from .driver import DriveResult, drive_document
from .ocr_config import OcrSettings
from .ocr_remote import OcrBatchClient
from .ocr_wire import PageImage

log = structlog.get_logger(__name__)

# A crude, honest quality signal: real page text is overwhelmingly printable
# characters. Symbol soup (a failed recognition) is not. This detects garbage,
# not chrome contamination — that is the crop probe's job (research D11).
_PLAUSIBLE = re.compile(r"[A-Za-z0-9 \t.,;:'\"()\[\]{}<>&/\\%$#*+=-]")
QUALITY_FLOOR = 0.9


def text_quality(text: str) -> float:
    """Share of plausible characters, 1.0 for an empty string."""
    if not text:
        return 1.0
    plausible = len(_PLAUSIBLE.findall(text))
    return plausible / len(text)


@dataclass
class ScrapeOutcome:
    key: str = ""
    title: str = ""
    text: str = ""
    page_count: int = 0
    ended_by: str = ""
    restarts: int = 0
    ocr: dict[str, Any] = field(default_factory=dict)
    suspect_pages: list[int] = field(default_factory=list)
    container: str | None = None
    output_path: Path | None = None
    reused_cache: bool = False

    @property
    def complete(self) -> bool:
        from .driver import OBSERVED_END

        return self.ended_by == OBSERVED_END


def _join_pages(pages: list[str]) -> str:
    """Join page texts the way the sibling pack does: blank line between pages."""
    return "\n\n".join(page.strip() for page in pages if page.strip())


async def scrape_to_disk(
    *,
    key: str,
    container: Path,
    data_dir: Path,
    settings: OcrSettings,
    title: str = "",
    prefix: Path | None = None,
    client: OcrBatchClient | None = None,
    driver_fn: Callable[..., Awaitable[DriveResult]] = drive_document,
    open_document: Callable[[], Awaitable[str]] | None = None,
    page_delay_min: float = 1.5,
    page_delay_max: float = 4.0,
    max_pages: int | None = None,
    max_consecutive_stale: int = 4,
    max_consecutive_empty: int = 20,
    quality_floor: float = QUALITY_FLOOR,
    driver_overrides: dict[str, Any] | None = None,
) -> ScrapeOutcome:
    """Capture a fulfilled document into the cache.

    ``open_document`` is the seam that starts the reader and returns its window
    id; tests inject a fake. Without it (and without a prefix) the reader path
    raises, which is the honest failure when the environment is incomplete.

    ``driver_overrides`` forwards extra keyword arguments to the loop — used by
    tests to collapse the settle delays, and by callers who want the loop's
    clock and jitter injectable without touching this module.
    """
    client = client or OcrBatchClient(settings)
    outcome = ScrapeOutcome(key=key, title=title or container.stem, container=str(container))

    if open_document is None and prefix is None:
        # Fail before touching the display: a missing prefix is a configuration
        # mistake, not a broken X server.
        raise ValueError("scrape_to_disk needs a prefix or an injected open_document")

    display = capture_mod.Display()
    async with display:
        if open_document is None:
            from .fulfill import open_in_reader

            async def open_document() -> str:  # type: ignore[misc]
                return await open_in_reader(prefix, container, display=display)

        window_id = await open_document()
        position = {"index": 0}

        async def capture_page(index: int) -> bytes:
            position["index"] = index
            frame = await capture_mod.grab_png(display.display, window_id)
            return capture_mod.crop_png(frame)

        async def accept_page(png: bytes) -> str:
            # One page per request: the loop's empty-page counter is measured on
            # this text, so it cannot be deferred to a later batch (research D10).
            texts = await client.recognize([PageImage(position["index"], png)])
            return texts[0] if texts else ""

        async def on_restart() -> None:
            nonlocal window_id
            window_id = await open_document()

        result = await driver_fn(
            capture=capture_page,
            send_turn=lambda: capture_mod.send_turn(display.display, window_id),
            send_start=lambda: capture_mod.send_start(display.display, window_id),
            digest_of=capture_mod.frame_digest,
            is_blank=capture_mod.is_blank,
            on_restart=on_restart,
            accept_page=accept_page,
            page_delay_min=page_delay_min,
            page_delay_max=page_delay_max,
            max_pages=max_pages,
            max_consecutive_stale=max_consecutive_stale,
            max_consecutive_empty=max_consecutive_empty,
            **(driver_overrides or {}),
        )

    texts = [page.text for page in result.pages]
    outcome.page_count = len(texts)
    outcome.ended_by = result.ended_by
    outcome.restarts = result.restarts
    outcome.text = _join_pages(texts)
    qualities = [text_quality(text) for text in texts]
    real = [q for q, text in zip(qualities, texts, strict=True) if text.strip()]
    outcome.suspect_pages = [i for i, (q, t) in enumerate(zip(qualities, texts, strict=True))
                             if t.strip() and q < quality_floor]
    outcome.ocr = {
        "protocol": 1,
        "model": settings.model,
        "model_version": settings.version,
        "remote": client.remote_enabled,
        "quality_metric": "printable_char_ratio",
        "quality_min": min(real) if real else None,
        "quality_mean": round(sum(real) / len(real), 4) if real else None,
        "quality_floor": quality_floor,
    }

    if not outcome.text.strip():
        log.warning("capture_produced_no_text", key=key, pages=outcome.page_count,
                    ended_by=outcome.ended_by)
        return outcome

    record = _paths.build_sidecar(
        key=key,
        key_source=(_paths.read_sidecar(data_dir, key) or {}).get("key_source", "unknown"),
        title=outcome.title,
        page_count=max(outcome.page_count, 1),
        ended_by=outcome.ended_by,
        restarts=outcome.restarts,
        ocr=outcome.ocr,
        suspect_pages=outcome.suspect_pages,
        container=str(container),
    )
    outcome.output_path = _paths.write_cache(data_dir, key, outcome.text, record)
    return outcome
