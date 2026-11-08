"""``research-engine-acsm-setup`` — report the environment, install nothing.

The pack spawns Wine, which runs Windows software with the operator's
privileges; the least it can do is tell the operator, precisely, what is missing
and what would fix it (constitution §III). Nothing here installs, downloads, or
rewrites a prefix — the only writes are the ones the operator explicitly asks
for with ``--backup-prefix`` / ``--restore-prefix``.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from . import capture, fulfill
from .ocr_config import OcrSettings

log = structlog.get_logger(__name__)

SETUP_COMMAND = "research-engine-acsm-setup"
PROBE_TURNS = 6


@dataclass(frozen=True)
class Probe:
    name: str
    found: bool
    detail: str
    remediation: str
    required: bool = True


def _which(name: str) -> str | None:
    return shutil.which(name)


def _probe_binary(name: str, remediation: str, *, required: bool = True) -> Probe:
    found = _which(name)
    return Probe(name, bool(found), found or "not found", remediation, required)


def _probe_wine() -> Probe:
    wine = _which("wine")
    if not wine:
        return Probe("wine", False, "not found", "install Wine from your distribution")
    try:
        version = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [wine, "--version"], capture_output=True, text=True, timeout=20, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - host specific
        version = f"present, but --version failed: {type(exc).__name__}"
    return Probe("wine", True, version or "present", "")


def _probe_prefix(prefix: Path) -> list[Probe]:
    exists = prefix.is_dir()
    reader = fulfill.reader_executable(prefix)
    library = fulfill.library_dir(prefix)
    return [
        Probe(
            "wine prefix",
            exists,
            str(prefix) if exists else f"{prefix} does not exist",
            "install Adobe Digital Editions into the prefix you configure with ACSM_WINEPREFIX",
        ),
        Probe(
            "Adobe Digital Editions",
            reader.is_file(),
            str(reader) if reader.is_file() else f"{reader} not found",
            "install Adobe Digital Editions 4.5.x inside the prefix",
        ),
        Probe(
            "reader library directory",
            library is not None,
            str(library) if library else "not found under drive_c/users/*",
            "open Adobe Digital Editions once so it creates its library directory",
            required=False,
        ),
        Probe(
            "Adobe activation",
            False,
            "cannot be checked without launching the reader",
            f"{SETUP_COMMAND} --activate",
            required=False,
        ),
    ]


def _probe_capture_tools() -> list[Probe]:
    importer = _which("import")
    xwd, convert = _which("xwd"), _which("convert")
    if importer:
        detail = importer
    else:
        found = ", ".join(f"{name} → {path}" for name, path in (("xwd", xwd), ("convert", convert)))
        missing = [name for name, path in (("xwd", xwd), ("convert", convert)) if path is None]
        detail = found if not missing else f"{found} (missing: {', '.join(missing)})"
    return [
        Probe(
            "capture tool",
            bool(importer or (xwd and convert)),
            detail,
            "install ImageMagick (import), or x11-apps + ImageMagick (xwd + convert)",
        ),
        Probe(
            "tesseract",
            bool(_which("tesseract")),
            _which("tesseract") or "not found",
            "optional: only needed if you also use the local pytesseract path",
            required=False,
        ),
    ]


def _probe_ocr(settings: OcrSettings) -> Probe:
    if not settings.base_url:
        return Probe(
            "OCR endpoint",
            False,
            "KINDLE_OCR_BASE_URL is not set (local OCR only)",
            "set KINDLE_OCR_BASE_URL to the CUDA OCR service, e.g. http://gpu-host:9883",
            required=False,
        )
    try:
        with httpx.Client(timeout=min(5.0, settings.timeout), trust_env=False) as client:
            response = client.get(settings.base_url.rstrip("/") + "/health")
            response.raise_for_status()
            body = response.json()
    except Exception as exc:
        return Probe(
            "OCR endpoint",
            False,
            f"{settings.base_url}: {type(exc).__name__}",
            "start the OCR service (see its systemd unit) or unset KINDLE_OCR_BASE_URL",
            required=False,
        )
    warm = bool(body.get("warm")) and str(body.get("device", "")).startswith("cuda")
    return Probe(
        "OCR endpoint",
        warm,
        f"{settings.base_url}: model={body.get('model')} warm={body.get('warm')} "
        f"device={body.get('device')}",
        "wait for the OCR service to finish warming its CUDA reader",
        required=False,
    )


def probe_environment(prefix: Path | None = None, settings: OcrSettings | None = None) -> list[Probe]:
    """Every external dependency, with the command that fixes each gap."""
    prefix = prefix or _common_prefix()
    settings = settings or OcrSettings.from_env()
    probes = [
        _probe_wine(),
        _probe_binary("Xvfb", "install Xvfb (xserver-xorg-video-dummy / xorg-x11-server-Xvfb)"),
        _probe_binary(
            capture.WINDOW_MANAGER,
            "install a minimal window manager (e.g. matchbox-window-manager or openbox)",
        ),
        _probe_binary("xdotool", "install xdotool (synthetic key delivery)"),
        *_probe_capture_tools(),
        *_probe_prefix(prefix),
        _probe_ocr(settings),
    ]
    return probes


def _common_prefix() -> Path:
    from .tools._common import prefix_from_env

    return prefix_from_env()


def _format(probe: Probe) -> str:
    mark = "OK  " if probe.found else ("MISS" if probe.required else "opt ")
    line = f"  [{mark}] {probe.name}: {probe.detail}"
    if not probe.found and probe.remediation:
        line += f"\n         → {probe.remediation}"
    return line


def report(probes: list[Probe]) -> int:
    print("ACSM pack environment\n")
    for probe in probes:
        print(_format(probe))
    missing = [probe for probe in probes if probe.required and not probe.found]
    print()
    if missing:
        print(f"{len(missing)} required component(s) missing: "
              + ", ".join(probe.name for probe in missing))
        print("Nothing was installed: this command only reports.")
        return 1
    print("All required components present.")
    print(f"Next: {SETUP_COMMAND} --activate (once), then acsm.fulfill_document.")
    return 0


async def _capture_probe(container: Path, turns: int) -> int:
    """Measure the crop and the key mechanism against a real document.

    This is how the reader-facing constants in ``capture.py`` are re-measured
    after a reader or Wine upgrade: run it, read the digests, and update the
    constants only if the digests prove the turn advanced a page.
    """
    display = capture.Display()
    async with display:
        window_id = await fulfill.open_in_reader(
            _common_prefix(), container, display=display, settle=4.0
        )
        print(f"window={window_id} screen={capture.SCREEN}")
        await capture.send_start(display.display, window_id)
        previous = None
        for turn in range(turns):
            frame = capture.crop_png(await capture.grab_png(display.display, window_id))
            digest = capture.frame_digest(frame)
            blank = capture.is_blank(frame)
            changed = "changed" if digest != previous else "SAME"
            print(f"  turn {turn}: digest={digest[:16]} {changed} blank={blank}")
            previous = digest
            await capture.send_turn(display.display, window_id)
            await asyncio.sleep(2.0)
    print(
        "\nIf no turn reports 'changed', keys are not reaching the reader: check "
        "capture.KEY_MODE and capture.WINDOW_TITLE, then re-run."
    )
    return 0


def _backup_prefix(prefix: Path, destination: Path) -> int:
    if not prefix.is_dir():
        print(f"prefix {prefix} does not exist; nothing to back up")
        return 1
    if destination.exists():
        print(f"refusing to overwrite {destination}")
        return 1
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(prefix, arcname=prefix.name)
    print(f"backed up {prefix} → {destination}")
    print("Adobe activations are limited: restore this archive instead of re-activating.")
    return 0


def _restore_prefix(archive_path: Path, prefix: Path) -> int:
    if not archive_path.is_file():
        print(f"{archive_path} is not a file")
        return 1
    if prefix.exists() and any(prefix.iterdir()):
        print(f"refusing to restore into a non-empty {prefix}; move it aside first")
        return 1
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=prefix.parent) as staging:
        with tarfile.open(archive_path, "r:gz") as archive:
            try:
                archive.extractall(staging, filter="data")
            except TypeError:  # pragma: no cover - Python < 3.12 without the filter backport
                archive.extractall(staging)  # noqa: S202 - operator-chosen archive
        entries = list(Path(staging).iterdir())
        # The archive holds one root directory (the prefix as it was named when
        # it was written); the operator asked for this path, so move the
        # contents there rather than resurrecting the old name.
        source = entries[0] if len(entries) == 1 and entries[0].is_dir() else Path(staging)
        shutil.move(str(source), str(prefix))
    print(f"restored {archive_path} → {prefix}")
    return 0


async def _activate(prefix: Path) -> int:
    """Open the reader so the operator can complete Adobe activation."""
    reader = fulfill.reader_executable(prefix)
    if not reader.is_file():
        print(f"Adobe Digital Editions not found at {reader}; install it first")
        return 1
    print("Opening Adobe Digital Editions. Complete activation yourself;")
    print("this command never asks for, stores, or transmits your credentials.")
    display = capture.Display()
    async with display:
        await fulfill.launch_reader(display.display, reader, reader, prefix=prefix)
        print("Close the reader window when activation is complete (waiting 10 minutes).")
        await asyncio.sleep(600)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=SETUP_COMMAND,
        description="Report (and never install) the ACSM pack's external requirements.",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="open Adobe Digital Editions so you can activate it yourself",
    )
    parser.add_argument(
        "--capture-probe",
        metavar="CONTAINER",
        help="measure crop/turn behaviour against a fulfilled container",
    )
    parser.add_argument("--turns", type=int, default=PROBE_TURNS)
    parser.add_argument("--backup-prefix", metavar="ARCHIVE", help="write a tar.gz of the prefix")
    parser.add_argument("--restore-prefix", metavar="ARCHIVE", help="restore a prefix archive")
    parser.add_argument(
        "--prefix", default=None, help="override ACSM_WINEPREFIX for this run"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prefix = Path(args.prefix).expanduser() if args.prefix else _common_prefix()

    if args.backup_prefix:
        return _backup_prefix(prefix, Path(args.backup_prefix).expanduser())
    if args.restore_prefix:
        return _restore_prefix(Path(args.restore_prefix).expanduser(), prefix)
    if args.activate:
        return asyncio.run(_activate(prefix))
    if args.capture_probe:
        return asyncio.run(_capture_probe(Path(args.capture_probe).expanduser(), args.turns))

    probes = probe_environment(prefix=prefix, settings=OcrSettings.from_env())
    code = report(probes)
    if code == 0:
        print(
            "\nReader-facing constants still needing Phase 0 verification (A1–A3):\n"
            f"  capture.READER_TITLE = {capture.READER_TITLE!r}\n"
            f"  capture.SCREEN       = {capture.SCREEN!r}\n"
            f"  capture.CROP         = {capture.CROP!r}\n"
            f"  capture.KEY_MODE     = {capture.KEY_MODE!r}\n"
            f"Verify them with: {SETUP_COMMAND} --capture-probe <fulfilled container>"
        )
    return code


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())
