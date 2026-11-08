"""Guard for the vendored OCR copy (FR-023, constitution §VI).

The client is duplicated from the sibling pack on purpose (research D2), and
duplication is only safe if a drift is loud. This asserts the wire constants,
and skips with a reason when the sibling pack is not installed rather than
silently passing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acsm import ocr_config, ocr_wire

kindle_wire = pytest.importorskip(
    "kindle.ocr_wire", reason="sibling pack marginalia-plugin-kindle is not installed"
)
kindle_config = pytest.importorskip("kindle.ocr_config")


def test_protocol_version_matches_upstream():
    assert ocr_wire.PROTOCOL == kindle_wire.PROTOCOL


def test_transport_limits_match_upstream():
    assert ocr_config.MAX_PAGES == kindle_config.MAX_PAGES
    assert ocr_config.MAX_BYTES == kindle_config.MAX_BYTES


def test_model_version_string_matches_upstream():
    for model in ("english_g2", "latin_g2"):
        assert ocr_config.model_version(model) == kindle_config.model_version(model)


def test_vendored_revision_is_recorded():
    marker = Path(ocr_config.__file__).with_name("VENDORED_FROM")
    assert marker.exists(), "the vendored copy must record its upstream revision"
    assert len(marker.read_text(encoding="utf-8").strip()) >= 7
