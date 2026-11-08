"""Fulfilling a loan and identifying the container.

The pack never decrypts anything (constitution §II): Adobe Digital Editions is
handed the ``.acsm`` and writes the encrypted container into its own library
inside the Wine prefix. All this module does is launch the reader, notice the
new file, and derive a stable key from it.

Identity (research D1) comes from the container's plaintext rights metadata when
one is present and usable, otherwise from the container's content hash — and
which one was used is always reported, because a content hash changes when the
same loan is re-downloaded.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import structlog

from . import _paths
from .capture import Display, _child_env, run_command

log = structlog.get_logger(__name__)

# PLACEHOLDER paths inside the prefix until Phase 0 (A1) measures them.
READER_RELATIVE = "drive_c/Program Files/Adobe/Digital Editions/DigitalEditions.exe"
LIBRARY_CANDIDATES = (
    "drive_c/users/{user}/Documents/My Digital Editions",
    "drive_c/users/{user}/My Documents/My Digital Editions",
)
CONTAINER_SUFFIXES = (".pdf", ".epub")

# Windows that mean "a human must act", matched case-insensitively against the
# reader's window titles. Phase 0 (A5) confirms the wording.
BLOCKER_HINTS = {
    "activate": ("activation", "activate", "sign in", "sign-in", "authorize"),
    "loan": ("expired", "loan", "return", "not available"),
}

RIGHTS_MEMBER = "META-INF/rights.xml"
_RIGHTS_SCAN_BYTES = 256 * 1024


class FulfillError(RuntimeError):
    """The reader could not fulfil this loan.

    ``action`` names the operator action that would unblock it, when there is
    one — the handler turns that into ``operator_action_required`` rather than a
    generic error (FR-018).
    """

    def __init__(self, message: str, *, action: str | None = None) -> None:
        super().__init__(message)
        self.action = action


@dataclass(frozen=True)
class Fulfilled:
    key: str
    key_source: str
    container: Path
    title: str


def reader_executable(prefix: Path) -> Path:
    return Path(prefix) / READER_RELATIVE


def library_dir(prefix: Path) -> Path | None:
    """Locate the reader's library directory inside the prefix."""
    users = Path(prefix) / "drive_c" / "users"
    if users.is_dir():
        for user in sorted(users.iterdir()):
            for template in LIBRARY_CANDIDATES:
                candidate = Path(prefix) / template.format(user=user.name)
                if candidate.is_dir():
                    return candidate
    return None


def containers_in(directory: Path | None) -> set[Path]:
    if directory is None or not directory.is_dir():
        return set()
    return {
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in CONTAINER_SUFFIXES
    }


def rights_text(container: Path) -> str:
    """Return the container's plaintext rights metadata, or "".

    For an EPUB this is a ZIP member. For a PDF there is no member, so the head
    of the file is scanned for a rights block. Neither reads encrypted content:
    rights metadata is the part of the container that describes the licence.
    """
    try:
        if zipfile.is_zipfile(container):
            with zipfile.ZipFile(container) as archive:
                if RIGHTS_MEMBER in archive.namelist():
                    return archive.read(RIGHTS_MEMBER).decode("utf-8", "replace")
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        log.warning("rights_member_unreadable", container=str(container), reason=type(exc).__name__)
    try:
        with container.open("rb") as handle:
            head = handle.read(_RIGHTS_SCAN_BYTES)
    except OSError as exc:
        log.warning("container_unreadable", container=str(container), reason=type(exc).__name__)
        return ""
    text = head.decode("latin-1", "replace")
    start = text.find("<rights")
    return text[start:] if start >= 0 else ""


def extract_rights_id(container: Path) -> str | None:
    """The container's declared identifier, if it is usable as a document key."""
    text = rights_text(container)
    if not text:
        return None
    for tag in ("dc:identifier", "identifier", "adept:resource"):
        start = 0
        while True:
            open_tag = text.find(f"<{tag}", start)
            if open_tag < 0:
                break
            close_tag = text.find(">", open_tag)
            end_tag = text.find(f"</{tag}>", close_tag)
            if close_tag < 0 or end_tag < 0:
                break
            value = text[close_tag + 1 : end_tag].strip()
            if value and _paths.is_valid_key(value):
                return value
            start = end_tag + 1
    return None


def content_hash(container: Path) -> str:
    digest = hashlib.sha256()
    with container.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def document_key(container: Path, *, rights_id: str | None = None) -> tuple[str, str]:
    """Return ``(key, key_source)`` — never an empty key (VR-001)."""
    candidate = rights_id if rights_id is not None else extract_rights_id(container)
    if candidate and _paths.is_valid_key(candidate):
        return candidate, "rights_metadata"
    return content_hash(container), "content_hash"


async def launch_reader(
    display: str,
    reader: Path,
    target: Path,
    *,
    prefix: Path,
    timeout: float | None = None,
) -> None:
    """Start the reader on ``target`` and do not wait for it to exit.

    A separate function so tests can replace the one place a process is spawned.
    """
    log.info("launching_reader", target=target.name, display=display)
    env = _child_env(display, {"WINEPREFIX": str(prefix)})
    await asyncio.create_subprocess_exec(
        "wine",
        str(reader),
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )


async def detect_blocker(display: str) -> str | None:
    """Look for a reader window that means the operator must do something."""
    try:
        listing = await run_command(
            ["xdotool", "search", "--name", "Adobe"], env=_child_env(display), capture_stdout=True
        )
    except Exception as exc:  # pragma: no cover - diagnostic only
        log.debug("blocker_probe_failed", reason=type(exc).__name__)
        return None
    for window_id in listing.decode().split():
        try:
            name = await run_command(
                ["xdotool", "getwindowname", window_id],
                env=_child_env(display),
                capture_stdout=True,
            )
        except Exception:
            continue
        title = name.decode("utf-8", "replace").lower()
        for kind, hints in BLOCKER_HINTS.items():
            if any(hint in title for hint in hints):
                return kind
    return None


async def wait_for_container(
    folder: Path | None,
    *,
    before: set[Path],
    timeout: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Path:
    """Wait for a container that was not there before this fulfillment."""
    if folder is None:
        raise FulfillError(
            "the reader's library directory was not found inside the Wine prefix; "
            "run research-engine-acsm-setup to verify the prefix"
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new = containers_in(folder) - before
        if new:
            return max(new, key=lambda path: path.stat().st_mtime)
        await sleep(1.0)
    raise TimeoutError(f"no new container appeared in {folder} within {timeout:g}s")


async def fulfill(
    acsm_path: Path,
    *,
    prefix: Path,
    display: Display,
    timeout: float = 300.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Fulfilled:
    """Open ``acsm_path`` with the reader and return the fulfilled container.

    Raises :class:`FulfillError` with an operator-action message when the prefix
    is not usable, is not activated, or the loan has expired — never a bare
    subprocess error (FR-018).
    """
    acsm_path = Path(acsm_path)
    if not acsm_path.is_file():
        raise FulfillError(f"{acsm_path} is not a file")

    reader = reader_executable(prefix)
    if not reader.is_file():
        raise FulfillError(
            f"Adobe Digital Editions was not found at {reader}; install it in the "
            f"prefix ({prefix})",
            action="research-engine-acsm-setup",
        )

    folder = library_dir(prefix)
    before = containers_in(folder)

    await launch_reader(display.display, reader, acsm_path, prefix=prefix)
    try:
        container = await wait_for_container(folder, before=before, timeout=timeout, sleep=sleep)
    except TimeoutError as exc:
        blocker = await detect_blocker(display.display)
        if blocker == "activate":
            raise FulfillError(
                "the reader is asking for activation",
                action="research-engine-acsm-setup --activate",
            ) from exc
        if blocker == "loan":
            raise FulfillError(
                "the reader reports the loan is unavailable or expired; re-borrow or "
                "re-download the .acsm",
                action="re-borrow or re-download the .acsm",
            ) from exc
        raise FulfillError(
            f"{exc}; check the reader window for a dialog",
            action="research-engine-acsm-setup",
        ) from exc

    key, key_source = document_key(container)
    if key_source == "content_hash":
        log.info("key_from_content_hash", container=container.name)
    return Fulfilled(key=key, key_source=key_source, container=container, title=container.stem)


async def open_in_reader(
    prefix: Path, container: Path, *, display: Display, settle: float = 4.0
) -> str:
    """Open a fulfilled container and return the reader window id.

    The window id is looked up after a settle delay; ``find_window`` from
    ``capture`` is injected by the caller's default path so this stays testable.
    """
    from .capture import find_window

    reader = reader_executable(prefix)
    if not reader.is_file():
        raise FulfillError(f"Adobe Digital Editions was not found at {reader}")
    await launch_reader(display.display, reader, container, prefix=prefix)
    await asyncio.sleep(settle)
    return await find_window(display.display)


async def find_container(prefix: Path, key: str) -> Path | None:
    """Recover a container path without a sidecar, by matching the key."""
    for container in containers_in(library_dir(prefix)):
        try:
            candidate, _ = document_key(container)
        except OSError:  # pragma: no cover - unreadable file
            continue
        if candidate == key:
            return container
    return None
