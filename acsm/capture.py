"""The display, the reader window, and the pixels.

Everything reader-specific in this pack lives here as a named constant, because
every one of them is pinned by a Phase 0 measurement rather than by knowledge
(research.md A1–A5, constitution §VI). Until those verdicts are recorded, treat
the values below as placeholders and re-measure with::

    research-engine-acsm-setup --capture-probe

Invariants (research D8, constitution §II):

* every external call uses a fixed argv, never a shell, and passes an explicit
  environment — the operator's ``DISPLAY`` must never leak into a capture;
* a frame is returned as bytes and never written to disk, logged, or sent
  anywhere except the configured OCR endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shutil

import structlog
from PIL import Image, ImageStat

log = structlog.get_logger(__name__)

# --- pinned by Phase 0 (A1): reader + display geometry ---------------------
DISPLAY = ":9"
SCREEN = "1280x900x24"
WINDOW_MANAGER = "matchbox-window-manager"
READER_TITLE = "Adobe Digital Editions"  # PLACEHOLDER until A1 is measured

# --- pinned by Phase 0 (A3): what of the frame is actually the page --------
CROP = (0, 44, 1280, 860)  # PLACEHOLDER until A3 is measured
BLANK_STDDEV = 3.0  # a frame quieter than this has not rendered yet

# --- pinned by Phase 0 (A2): how keys reach the window --------------------
# "window" uses XSendEvent (xdotool key --window), which needs no input focus;
# "focus" activates the window first and relies on XTEST reaching the focus.
KEY_MODE = "window"

_TURN_KEYS = ["Right", "Next"]
_START_KEYS = ["ctrl+Home"]

REQUIRED_TOOLS = ("Xvfb", "xdotool", "import")

# Only what the child needs: no inherited environment, no operator display.
_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "XDG_RUNTIME_DIR")


def available_tools() -> dict[str, str | None]:
    """Which external binaries this host provides, for setup and diagnostics."""
    names = (*REQUIRED_TOOLS, WINDOW_MANAGER, "wine", "convert", "scrot")
    return {name: shutil.which(name) for name in names}


def _child_env(display: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    env["DISPLAY"] = display
    if extra:
        env.update(extra)
    return env


async def run_command(
    argv: list[str],
    *,
    env: dict[str, str],
    capture_stdout: bool = False,
    input_bytes: bytes | None = None,
    timeout: float = 60.0,
) -> bytes:
    """Run a fixed argv with an explicit environment. Never a shell."""
    log.debug("running", argv=argv[0], args=len(argv))
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(input_bytes), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise TimeoutError(f"{argv[0]} did not finish within {timeout:g}s") from None
    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"{argv[0]} failed ({process.returncode}): {detail[-1] if detail else ''}")
    return stdout or b""


class Display:
    """A private Xvfb display with a minimal window manager.

    Started per capture and torn down on exit (research D4): two readers sharing
    one display fight for input focus, which is exactly how keypresses get lost.
    """

    def __init__(self, display: str = DISPLAY, screen: str = SCREEN) -> None:
        self.display = display
        self.screen = screen
        self._processes: list[asyncio.subprocess.Process] = []

    @property
    def socket_path(self) -> str:
        return f"/tmp/.X11-unix/X{self.display.lstrip(':')}"

    async def _spawn(self, argv: list[str]) -> asyncio.subprocess.Process:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_child_env(self.display),
        )
        self._processes.append(process)
        return process

    async def __aenter__(self) -> Display:
        missing = [name for name in ("Xvfb", WINDOW_MANAGER) if not shutil.which(name)]
        if missing:
            raise RuntimeError(
                "missing external tool(s): "
                + ", ".join(missing)
                + " — run research-engine-acsm-setup"
            )
        await self._spawn(["Xvfb", self.display, "-screen", "0", self.screen])
        for _ in range(100):  # ~10s
            if os.path.exists(self.socket_path):
                break
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError(f"Xvfb did not create {self.socket_path}")
        await self._spawn([WINDOW_MANAGER])
        log.info("display_started", display=self.display, screen=self.screen)
        return self

    async def __aexit__(self, *exc: object) -> None:
        for process in reversed(self._processes):
            if process.returncode is None:
                process.terminate()
        for process in reversed(self._processes):
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:  # pragma: no cover - defensive
                process.kill()
        self._processes.clear()
        log.info("display_stopped", display=self.display)


async def find_window(display: str, title: str = READER_TITLE) -> str:
    """Return the reader's window id. Ambiguity is an error, not a guess."""
    out = await run_command(
        ["xdotool", "search", "--onlyvisible", "--name", title],
        env=_child_env(display),
        capture_stdout=True,
        timeout=20.0,
    )
    ids = [line.strip() for line in out.decode("utf-8", "replace").split() if line.strip()]
    if not ids:
        raise RuntimeError(
            f"no visible window matching {title!r} — is the reader running with the document open?"
        )
    if len(ids) > 1:
        log.warning("multiple_reader_windows", count=len(ids), using=ids[-1])
    return ids[-1]


async def grab_png(
    display: str,
    window_id: str,
    *,
    import_tool: str | None = None,
    xwd_tool: str | None = None,
    convert_tool: str | None = None,
) -> bytes:
    """Capture the reader window (never the root) as PNG bytes.

    Prefers ImageMagick ``import``; falls back to ``xwd`` piped into
    ``convert``. Both paths are fixed-argv, and the pipe is stdin bytes, not a
    shell pipeline.
    """
    importer = import_tool if import_tool is not None else shutil.which("import")
    if importer:
        return await run_command(
            [importer, "-window", window_id, "png:-"],
            env=_child_env(display),
            capture_stdout=True,
            timeout=20.0,
        )
    xwd = xwd_tool if xwd_tool is not None else shutil.which("xwd")
    convert = convert_tool if convert_tool is not None else shutil.which("convert")
    if xwd and convert:
        raw = await run_command(
            [xwd, "-id", window_id, "-silent"],
            env=_child_env(display),
            capture_stdout=True,
            timeout=20.0,
        )
        return await run_command(
            [convert, "xwd:-", "png:-"],
            env=_child_env(display),
            capture_stdout=True,
            input_bytes=raw,
            timeout=20.0,
        )
    raise RuntimeError(
        "no capture tool found (ImageMagick 'import', or 'xwd' + 'convert') "
        "— run research-engine-acsm-setup"
    )


def crop_png(png: bytes, box: tuple[int, int, int, int] = CROP) -> bytes:
    """Crop chrome out of a captured frame. Returns PNG bytes."""
    with Image.open(io.BytesIO(png)) as source:
        image = source.convert("RGB")
        left, top, right, bottom = box
        width, height = image.size
        clamped = (
            max(0, min(left, width)),
            max(0, min(top, height)),
            max(0, min(right, width)),
            max(0, min(bottom, height)),
        )
        if clamped[2] <= clamped[0] or clamped[3] <= clamped[1]:
            raise ValueError(f"crop {box} is empty for a {width}x{height} frame")
        cropped = image.crop(clamped)
        buffer = io.BytesIO()
        cropped.save(buffer, format="PNG")
    return buffer.getvalue()


def frame_digest(png: bytes) -> str:
    """Stable digest of the visible pixels.

    Hashes decoded pixels rather than the PNG bytes so an encoder difference
    cannot masquerade as a page turn.
    """
    with Image.open(io.BytesIO(png)) as source:
        pixels = source.convert("L").tobytes()
    return hashlib.sha256(pixels).hexdigest()


def is_blank(png: bytes, *, threshold: float = BLANK_STDDEV) -> bool:
    """True when the frame is so uniform it cannot be a rendered page."""
    with Image.open(io.BytesIO(png)) as source:
        grey = source.convert("L")
        deviation = ImageStat.Stat(grey).stddev[0]
    return deviation < threshold


async def _key(display: str, window_id: str, keys: list[str]) -> None:
    if KEY_MODE == "focus":
        await run_command(
            ["xdotool", "windowactivate", "--sync", window_id],
            env=_child_env(display),
            timeout=20.0,
        )
        await run_command(
            ["xdotool", "key", "--clearmodifiers", *keys],
            env=_child_env(display),
            timeout=20.0,
        )
        return
    await run_command(
        ["xdotool", "key", "--clearmodifiers", "--window", window_id, *keys],
        env=_child_env(display),
        timeout=20.0,
    )


async def send_turn(display: str, window_id: str) -> None:
    """Advance one page."""
    await _key(display, window_id, _TURN_KEYS)


async def send_start(display: str, window_id: str) -> None:
    """Ask the reader for the beginning of the document.

    The reader reopens at its last-read position (like every reader), so this
    is never optional; whether the affordance works is Phase 0 verdict A2.
    """
    await _key(display, window_id, _START_KEYS)
