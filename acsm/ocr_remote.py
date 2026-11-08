"""Opt-in remote batches, with whole-batch local fallback on any outage.

Vendored from ``marginalia-plugin-kindle`` (see ``acsm/VENDORED_FROM``).
Only import paths changed; keep it that way (constitution §VI, FR-023).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import structlog

from . import ocr_local as ocr
from .ocr_wire import OcrProtocolError, decode_results, encode_batch, health_ready

if TYPE_CHECKING:
    from .ocr_config import OcrSettings
    from .ocr_wire import PageImage

log = structlog.get_logger(__name__)


async def remote_ocr_batch(
    pages: list[PageImage],
    settings: OcrSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """Strict remote API: return in input order, or raise; never partial output.

    Only transport errors and transient HTTP 502/503/504 are retried. Requests
    are stateless, so a lost response may repeat work but never duplicate text.
    """
    if not settings.base_url:
        raise OcrProtocolError("Remote OCR endpoint is not configured")
    payload = encode_batch(pages, settings)
    async with httpx.AsyncClient(
        base_url=settings.base_url.rstrip("/") + "/",
        timeout=httpx.Timeout(settings.timeout, connect=min(10.0, settings.timeout)),
        transport=transport,
        trust_env=False,
    ) as client:
        for attempt in range(settings.retries + 1):
            try:
                health = await client.get("health")
                health.raise_for_status()
                if not health_ready(health.json(), settings):
                    raise OcrProtocolError("OCR server is not warm on CUDA")
                response = await client.post(
                    "v1/ocr", content=payload, headers={"Content-Type": "application/zip"}
                )
                response.raise_for_status()
                return decode_results(response.json(), pages, settings)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {502, 503, 504} or attempt == settings.retries:
                    raise
            except httpx.TransportError:
                if attempt == settings.retries:
                    raise
            await asyncio.sleep(0.25 * (attempt + 1))
    raise AssertionError("OCR retry loop exhausted without a result")


class OcrBatchClient:
    """One scrape's circuit: an outage falls back and disables further attempts."""

    def __init__(
        self, settings: OcrSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.remote_enabled = bool(settings.base_url)

    async def recognize(self, pages: list[PageImage]) -> list[str]:
        if not pages:
            return []
        if self.remote_enabled:
            try:
                return await remote_ocr_batch(pages, self.settings, transport=self.transport)
            except Exception as exc:
                # Never log exception messages: HTTP bodies can contain book text.
                log.warning("remote_ocr_fallback", reason=type(exc).__name__, pages=len(pages))
                self.remote_enabled = False
        texts = []
        for page in pages:
            try:
                texts.append((await asyncio.to_thread(ocr.ocr_image_bytes, page.png)).strip())
            except Exception as exc:
                log.warning("local_ocr_failed", reason=type(exc).__name__, page=page.page_id)
                texts.append("")
        return texts
