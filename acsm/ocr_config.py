"""Opt-in OCR offload configuration; no Amazon credentials are involved.

Vendored from ``marginalia-plugin-kindle`` (see ``acsm/VENDORED_FROM``).
Keeps the upstream ``KINDLE_OCR_*`` variable names on purpose: they configure
the shared OCR service, and two sets of names for one service is drift.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

EASYOCR_VERSION = "1.7.2"
UNIT = "marginalia-kindle-ocr.service"
MAX_PAGES = 16
MAX_BYTES = 32 * 1024 * 1024


def model_version(model: str) -> str:
    """EasyOCR validates the built-in detector/recognizer weights by MD5."""
    return f"easyocr-{EASYOCR_VERSION}:craft:{model}"


@dataclass(frozen=True)
class OcrSettings:
    base_url: str | None = None
    ssh_target: str | None = None
    model: str = "english_g2"
    batch_size: int = 8
    timeout: float = 120.0
    retries: int = 1
    manage_gpu: str = "auto"
    start_timeout: float = 180.0

    def __post_init__(self) -> None:
        if self.base_url:
            url = urlsplit(self.base_url)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("KINDLE_OCR_BASE_URL must be an HTTP(S) endpoint")
            if url.username or url.password or url.query or url.fragment:
                raise ValueError("OCR endpoint must not contain credentials, query, or fragment")
        if self.ssh_target and (
            self.ssh_target.startswith("-")
            or any(c.isspace() for c in self.ssh_target)
            or any(c in self.ssh_target for c in ";|&`$\\\"'")
        ):
            raise ValueError("KINDLE_OCR_SSH_TARGET must be an SSH host alias or user@host")
        if self.model not in {"english_g2", "latin_g2"}:
            raise ValueError("KINDLE_OCR_MODEL must be english_g2 or latin_g2 (English pages)")
        if not 1 <= self.batch_size <= MAX_PAGES:
            raise ValueError(f"KINDLE_OCR_BATCH_SIZE must be 1..{MAX_PAGES}")
        if self.retries not in {0, 1, 2}:
            raise ValueError("KINDLE_OCR_RETRIES must be 0..2")
        if self.manage_gpu not in {"auto", "never"}:
            raise ValueError("KINDLE_OCR_MANAGE_GPU must be auto or never")
        if any(not math.isfinite(x) or x <= 0 for x in (self.timeout, self.start_timeout)):
            raise ValueError("OCR timeouts must be finite and positive")

    @property
    def version(self) -> str:
        return model_version(self.model)

    @classmethod
    def from_env(cls) -> OcrSettings:
        return cls(
            base_url=os.getenv("KINDLE_OCR_BASE_URL") or None,
            ssh_target=os.getenv("KINDLE_OCR_SSH_TARGET") or None,
            model=os.getenv("KINDLE_OCR_MODEL", "english_g2"),
            batch_size=int(os.getenv("KINDLE_OCR_BATCH_SIZE", "8")),
            timeout=float(os.getenv("KINDLE_OCR_TIMEOUT", "120")),
            retries=int(os.getenv("KINDLE_OCR_RETRIES", "1")),
            manage_gpu=os.getenv("KINDLE_OCR_MANAGE_GPU", "auto"),
            start_timeout=float(os.getenv("KINDLE_OCR_START_TIMEOUT", "180")),
        )
