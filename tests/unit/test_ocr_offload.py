"""Tests for the vendored OCR client — adapted from the upstream suite.

Upstream (`marginalia-plugin-kindle: tests/unit/test_ocr_offload.py`) covers its
own server and scraper as well. This pack ships only the client side plus the
wire protocol, so the server-driving and scraper-driving tests are replaced by
tests of the two things this pack actually calls:

* ``OcrBatchClient.recognize`` — order, whole-batch fallback, circuit breaking,
  transient retry, malformed-protocol handling;
* ``ocr_local.ocr_image_bytes`` — crop and line joining, since we ship it.

The unused upstream fixtures are gone on purpose; anything that would need the
sibling pack's server or reader is out of scope here (it is tested there).
"""

from __future__ import annotations

import io
from dataclasses import replace

import httpx
import pytest
from PIL import Image, ImageDraw

from acsm import ocr_local
from acsm.ocr_config import MAX_PAGES, OcrSettings
from acsm.ocr_remote import OcrBatchClient, remote_ocr_batch
from acsm.ocr_wire import OcrProtocolError, PageImage, decode_batch, encode_batch


def png(number: int, *, chrome: bool = False) -> bytes:
    image = Image.new("RGB", (240, 200), (number + 10, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.text((20, 90), f"Invented orchard page {number}", fill="black")
    if chrome:
        draw.rectangle((0, 0, 239, 59), fill="black")
        draw.rectangle((0, 140, 239, 199), fill="black")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class PixelReader:
    """Stand-in for an EasyOCR reader: the pixel value encodes the page number."""

    device = "cuda:0"

    def readtext(self, pixels, **kwargs):
        return [f"Orchard page {int(pixels[0, 0, 0]) - 10}"]


def ocr_service(settings: OcrSettings) -> httpx.MockTransport:
    """A minimal remote OCR service: /health plus a text-less /v1/ocr.

    The real service recognises images; the client under test only needs a
    protocol-valid response whose page IDs match the request, so this decodes
    the posted batch and answers with the encoded page number.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "protocol": 1,
                    "model": settings.model,
                    "model_version": settings.version,
                    "warm": True,
                    "device": "cuda:0",
                },
            )
        pages = decode_batch(request.content, settings)
        return httpx.Response(
            200,
            json={
                "protocol": 1,
                "model": settings.model,
                "model_version": settings.version,
                "results": [
                    {"page_id": page.page_id, "text": f"Orchard page {page.page_id}"}
                    for page in pages
                ],
            },
        )

    return httpx.MockTransport(handle)


# --------------------------------------------------------------------------- wire


def test_batch_round_trips_in_manifest_order():
    settings = OcrSettings()
    pages = [PageImage(i, png(i)) for i in [9, 2, 7]]
    assert [p.page_id for p in decode_batch(encode_batch(pages, settings), settings)] == [9, 2, 7]


def test_batch_rejects_duplicate_ids_and_negative_ids():
    settings = OcrSettings()
    with pytest.raises(OcrProtocolError):
        encode_batch([PageImage(1, png(1)), PageImage(1, png(2))], settings)
    with pytest.raises(OcrProtocolError):
        encode_batch([PageImage(-1, png(1))], settings)


def test_batch_rejects_more_pages_than_the_transport_allows():
    settings = OcrSettings()
    too_many = [PageImage(i, png(1)) for i in range(MAX_PAGES + 1)]
    with pytest.raises(OcrProtocolError):
        encode_batch(too_many, settings)


# ------------------------------------------------------------------- remote client


async def test_remote_batch_returns_in_input_order():
    settings = OcrSettings(base_url="http://ocr.test")
    pages = [PageImage(i, png(i)) for i in [9, 2, 7]]
    got = await remote_ocr_batch(pages, settings, transport=ocr_service(settings))
    assert got == ["Orchard page 9", "Orchard page 2", "Orchard page 7"]


@pytest.mark.parametrize("failure", ["connection", "503", "missing-page", "wrong-version"])
async def test_remote_failure_falls_back_whole_batch_and_opens_circuit(
    monkeypatch, failure
):
    monkeypatch.setattr(ocr_local, "_get_reader", lambda *a, **k: PixelReader())
    settings = OcrSettings(base_url="http://ocr.test", retries=0)
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "protocol": 1,
                    "model": settings.model,
                    "model_version": settings.version,
                    "warm": True,
                    "device": "cuda:0",
                },
            )
        if failure == "connection":
            raise httpx.ConnectError("synthetic outage", request=request)
        if failure == "503":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "protocol": 1,
                "model": settings.model,
                "model_version": "other-version" if failure == "wrong-version" else settings.version,
                "results": [{"page_id": 1, "text": "partial result must be discarded"}],
            },
        )

    client = OcrBatchClient(settings, transport=httpx.MockTransport(handle))
    pages = [PageImage(i, png(i)) for i in [1, 3]]
    assert await client.recognize(pages) == ["Orchard page 1", "Orchard page 3"]
    assert client.remote_enabled is False

    calls_before = list(calls)
    assert await client.recognize([PageImage(4, png(4))]) == ["Orchard page 4"]
    assert calls == calls_before  # the circuit opens: no further remote attempts


async def test_transient_503_on_health_recovers_without_local_ocr(monkeypatch):
    settings = OcrSettings(base_url="http://ocr.test", retries=1)
    service = ocr_service(settings)
    attempts = 0

    class FirstUnavailable(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal attempts
            if request.url.path == "/health":
                attempts += 1
                if attempts == 1:
                    return httpx.Response(503)
            return await service.handle_async_request(request)

    def unavailable(*args, **kwargs):
        raise AssertionError("A successful retry must not touch local OCR")

    monkeypatch.setattr(ocr_local, "ocr_image_bytes", unavailable)
    client = OcrBatchClient(settings, transport=FirstUnavailable())
    assert await client.recognize([PageImage(6, png(6))]) == ["Orchard page 6"]
    assert attempts == 2
    assert client.remote_enabled is True


@pytest.mark.parametrize("path", ["/health", "/v1/ocr"])
@pytest.mark.parametrize("invalid_protocol", [True, 1.0])
async def test_malformed_protocol_response_falls_back(monkeypatch, path, invalid_protocol):
    settings = OcrSettings(base_url="http://ocr.test")
    service = ocr_service(settings)
    monkeypatch.setattr(ocr_local, "_get_reader", lambda *a, **k: PixelReader())

    class MalformedProtocol(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await service.handle_async_request(request)
            if request.url.path == path:
                await response.aread()
                body = response.json()
                body["protocol"] = invalid_protocol
                return httpx.Response(200, json=body)
            return response

    client = OcrBatchClient(settings, transport=MalformedProtocol())
    assert await client.recognize([PageImage(2, png(2))]) == ["Orchard page 2"]
    assert client.remote_enabled is False


async def test_recognize_of_empty_batch_makes_no_request():
    settings = OcrSettings(base_url="http://ocr.test")

    def handle(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("an empty batch must not reach the network")

    client = OcrBatchClient(settings, transport=httpx.MockTransport(handle))
    assert await client.recognize([]) == []


async def test_no_endpoint_configured_uses_local_only(monkeypatch):
    monkeypatch.setattr(ocr_local, "_get_reader", lambda *a, **k: PixelReader())
    client = OcrBatchClient(OcrSettings())
    assert client.remote_enabled is False
    assert await client.recognize([PageImage(5, png(5))]) == ["Orchard page 5"]


# --------------------------------------------------------------------- local OCR


def test_local_ocr_crops_and_joins_lines(monkeypatch):
    class TwoLines:
        def readtext(self, pixels, **kwargs):
            assert kwargs["detail"] == 0 and kwargs["paragraph"] is True
            return ["first line", "second line"]

    monkeypatch.setattr(ocr_local, "_get_reader", lambda *a, **k: TwoLines())
    # crop is (left, top, right, bottom); the result must come from the crop
    assert ocr_local.ocr_image_bytes(png(3), crop=(0, 60, 240, 140)) == "first line\nsecond line"


# ----------------------------------------------------------------------- settings


@pytest.mark.parametrize(
    "setting",
    [
        {"base_url": "ssh://host"},
        {"ssh_target": "-oProxyCommand=bad"},
        {"batch_size": 17},
        {"timeout": float("nan")},
        {"model": "unknown"},
        {"retries": 5},
    ],
)
def test_invalid_settings_fail_before_network(setting):
    with pytest.raises(ValueError):
        replace(OcrSettings(), **setting)
