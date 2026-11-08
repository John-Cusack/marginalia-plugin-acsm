"""The page loop.

This module is a pure state machine over injected I/O (constitution §IV): it
opens no display, spawns no process, and imports nothing from ``capture``. The
only signal available is the pixels, so a page is "new" when its frame digest
changes from the previous frame's, and the two ways a capture can stop without
reaching the end — a turn that never registered, and pages that produced no
text — are counted separately (research D3).

Blank frames are their own case: an unrendered window is neither a page nor a
stale turn, so it does not consume the stale budget; it only feeds the stall
watchdog, which is what restarts a frozen reader.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from inspect import isawaitable

import structlog

log = structlog.get_logger(__name__)

# End-of-run reasons (data-model.md: EndOfRun).
OBSERVED_END = "observed_end"
MAX_PAGES = "max_pages"
STALE = "stale"
EMPTY = "empty"
CANCELLED = "cancelled"


@dataclass(frozen=True)
class PageCapture:
    """One captured page. ``text`` is filled by the caller (OCR) or a fake."""

    index: int
    png: bytes
    digest: str
    text: str = ""


@dataclass
class DriveResult:
    pages: list[PageCapture] = field(default_factory=list)
    ended_by: str = STALE
    restarts: int = 0
    stale_runs: int = 0
    empty_runs: int = 0

    @property
    def complete(self) -> bool:
        """Only a reader-signalled end counts as a complete capture (SC-003)."""
        return self.ended_by == OBSERVED_END


async def drive_document(
    *,
    capture: Callable[[int], Awaitable[bytes]],
    send_turn: Callable[[], Awaitable[None]],
    send_start: Callable[[], Awaitable[None]],
    digest_of: Callable[[bytes], str],
    is_blank: Callable[[bytes], bool],
    on_restart: Callable[[], Awaitable[None]] | None = None,
    accept_page: Callable[[bytes], str] | None = None,
    is_last_page: Callable[[bytes], Awaitable[bool]] | None = None,
    page_delay_min: float = 1.5,
    page_delay_max: float = 4.0,
    settle_timeout: float = 30.0,
    stall_timeout: float = 120.0,
    max_pages: int | None = None,
    max_consecutive_stale: int = 4,
    max_consecutive_empty: int = 20,
    max_restarts: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    rng: random.Random | None = None,
) -> DriveResult:
    """Capture pages until the document ends or a stopping threshold trips.

    ``capture(index)`` returns the cropped PNG of the current page.
    ``accept_page(png)`` returns the page's text (OCR, or a fake in tests); it
    may be a coroutine, in which case it is awaited before the page's counters
    move — the loop must see real recognition output, not a placeholder.
    ``is_last_page(png)`` is the optional reader affordance; when it is absent,
    the end of the document is only ever reported as ``stale`` or ``empty``,
    never as ``observed_end``.
    """
    if page_delay_min <= 0 or page_delay_max < page_delay_min:
        raise ValueError("page delays must be positive and ordered")
    if max_consecutive_stale < 1 or max_consecutive_empty < 1:
        raise ValueError("stopping thresholds must be at least 1")

    jitter = rng or random.Random()
    result = DriveResult()
    prev_digest: str | None = None
    last_progress_at = clock()

    await send_start()

    def delay() -> float:
        return jitter.uniform(page_delay_min, page_delay_max)

    try:
        while True:
            if max_pages is not None and len(result.pages) >= max_pages:
                result.ended_by = MAX_PAGES
                break

            png = await capture(len(result.pages))
            digest = digest_of(png)
            blank = is_blank(png)
            advanced = prev_digest is None or digest != prev_digest

            if advanced and not blank:
                last_progress_at = clock()
                result.stale_runs = 0
                text = accept_page(png) if accept_page is not None else ""
                if isawaitable(text):
                    # OCR is I/O; the loop still gets the page's own text before
                    # its counters move, so an empty-page run is measured on real
                    # recognition output rather than on a placeholder.
                    text = await text
                result.pages.append(PageCapture(len(result.pages), png, digest, text))
                prev_digest = digest

                if text.strip():
                    result.empty_runs = 0
                else:
                    result.empty_runs += 1
                    if result.empty_runs >= max_consecutive_empty:
                        log.info("capture_stopped_no_text", pages=len(result.pages))
                        result.ended_by = EMPTY
                        break

                if is_last_page is not None and await is_last_page(png):
                    log.info("capture_reached_last_page", pages=len(result.pages))
                    result.ended_by = OBSERVED_END
                    break

                await sleep(delay())
                await send_turn()
                continue

            # No new page content. A repeated digest is a turn that did not
            # register; a blank frame is an unrendered window and is neither.
            if advanced and blank:
                result.stale_runs = 0
            else:
                result.stale_runs += 1

            since_progress = clock() - last_progress_at
            stalled_for = settle_timeout if not result.pages else stall_timeout
            if since_progress > stalled_for:
                if on_restart is not None and result.restarts < max_restarts:
                    result.restarts += 1
                    log.warning("capture_restarting_reader", restarts=result.restarts)
                    await on_restart()
                    await send_start()
                    last_progress_at = clock()
                    prev_digest = None
                    result.stale_runs = 0
                    continue
                log.warning("capture_stalled", pages=len(result.pages))
                result.ended_by = STALE
                break

            if result.stale_runs >= max_consecutive_stale:
                log.info("capture_stopped_stale", pages=len(result.pages))
                result.ended_by = STALE
                break

            await sleep(delay())
            if not blank:
                await send_turn()
    except TimeoutError:
        # A caller-imposed timeout: the capture is partial, and says so.
        log.warning("capture_cancelled", pages=len(result.pages))
        result.ended_by = CANCELLED

    return result
