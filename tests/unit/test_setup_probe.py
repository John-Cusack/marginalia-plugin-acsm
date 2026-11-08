"""``research-engine-acsm-setup``: it reports, and it installs nothing."""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest

from acsm import setup
from acsm.ocr_config import OcrSettings


@pytest.fixture
def bare_host(monkeypatch):
    """A host with none of the external tools present."""

    def nothing(_name):
        return None

    monkeypatch.setattr(shutil, "which", nothing)


def probe_names(probes) -> set[str]:
    return {probe.name for probe in probes}


def test_every_required_probe_is_reported_missing_with_a_remediation(bare_host, tmp_path: Path):
    probes = setup.probe_environment(prefix=tmp_path / "prefix", settings=OcrSettings())
    names = probe_names(probes)

    assert {"wine", "Xvfb", "matchbox-window-manager", "xdotool", "capture tool"} <= names
    required_missing = [probe for probe in probes if probe.required and not probe.found]
    assert required_missing, "a bare host must report missing required components"
    for probe in required_missing:
        assert probe.remediation, f"{probe.name} needs a remediation hint"


def test_ocr_endpoint_is_optional_and_says_so(bare_host, tmp_path: Path):
    probes = {probe.name: probe for probe in setup.probe_environment(prefix=tmp_path, settings=OcrSettings())}
    endpoint = probes["OCR endpoint"]
    assert endpoint.required is False
    assert "KINDLE_OCR_BASE_URL" in endpoint.detail


def test_probe_reports_a_present_reader_and_library(tmp_path: Path):
    prefix = tmp_path / "prefix"
    reader = prefix / "drive_c/Program Files/Adobe/Digital Editions/DigitalEditions.exe"
    reader.parent.mkdir(parents=True)
    reader.write_text("", encoding="utf-8")
    (prefix / "drive_c/users/u/Documents/My Digital Editions").mkdir(parents=True)

    probes = {probe.name: probe for probe in setup.probe_environment(prefix=prefix, settings=OcrSettings())}

    assert probes["wine prefix"].found is True
    assert probes["Adobe Digital Editions"].found is True
    assert probes["reader library directory"].found is True


def test_main_exits_non_zero_and_installs_nothing(bare_host, tmp_path: Path, capsys):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    before = sorted(prefix.iterdir())

    code = setup.main(["--prefix", str(prefix)])
    output = capsys.readouterr().out

    assert code == 1
    assert "missing" in output.lower()
    assert "Nothing was installed" in output
    assert sorted(prefix.iterdir()) == before, "probe mode must not write anything"


def test_main_reports_success_shape_when_everything_is_present(monkeypatch, tmp_path: Path, capsys):
    prefix = tmp_path / "prefix"
    reader = prefix / "drive_c/Program Files/Adobe/Digital Editions/DigitalEditions.exe"
    reader.parent.mkdir(parents=True)
    reader.write_text("", encoding="utf-8")
    (prefix / "drive_c/users/u/Documents/My Digital Editions").mkdir(parents=True)

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(setup, "_probe_wine", lambda: setup.Probe("wine", True, "wine-9.0", ""))

    code = setup.main(["--prefix", str(prefix)])
    output = capsys.readouterr().out

    assert code == 0
    assert "All required components present" in output
    # The reader-facing placeholders are surfaced so the operator knows what to measure.
    assert "capture.CROP" in output
    assert "--capture-probe" in output


def frame(marker: str) -> bytes:
    """A real PNG with visible content, so the probe's own digest logic runs."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (400, 300), (255, 255, 255))
    ImageDraw.Draw(image).text((20, 20), marker, fill=(0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeDisplay:
    display = ":99"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


def wire_probe(monkeypatch, tmp_path: Path, frames: list[bytes]):
    """Wire the probe to scripted frames, leaving crop/digest/blank logic real."""
    container = tmp_path / "book.pdf"
    container.write_bytes(b"%PDF")
    sent: list[str] = []

    async def fake_open(prefix, path, display, settle):
        return "12345"

    async def fake_grab(display, window_id, **kwargs):
        return frames[min(len(sent), len(frames) - 1)]

    async def fake_send(display, window_id):
        sent.append("turn")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(setup.capture, "Display", FakeDisplay)
    monkeypatch.setattr(setup.fulfill, "open_in_reader", fake_open)
    monkeypatch.setattr(setup.capture, "grab_png", fake_grab)
    monkeypatch.setattr(setup.capture, "send_turn", fake_send)
    monkeypatch.setattr(setup.capture, "send_start", fake_send)
    monkeypatch.setattr(setup.asyncio, "sleep", no_sleep)
    return container


def test_capture_probe_reports_digests_per_turn(monkeypatch, tmp_path: Path, capsys):
    container = wire_probe(monkeypatch, tmp_path, [frame("page one"), frame("page two"), frame("page two")])

    code = setup.main(["--capture-probe", str(container), "--turns", "3"])
    output = capsys.readouterr().out

    assert code == 0
    assert "turn 0" in output and "turn 2" in output
    assert "changed" in output, "a new frame must read as a successful turn"
    assert "SAME" in output, "a repeated frame must be visible in the probe output"


def test_capture_probe_names_the_key_modes_to_check(monkeypatch, tmp_path: Path, capsys):
    container = wire_probe(monkeypatch, tmp_path, [frame("only page")])

    setup.main(["--capture-probe", str(container), "--turns", "1"])
    output = capsys.readouterr().out
    assert "KEY_MODE" in output and "WINDOW_TITLE" in output


def test_backup_and_restore_round_trip(monkeypatch, tmp_path: Path, capsys):
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    (prefix / "drive_c/activation.marker").write_text("activated", encoding="utf-8")
    archive = tmp_path / "prefix.tar.gz"

    assert setup.main(["--prefix", str(prefix), "--backup-prefix", str(archive)]) == 0
    assert archive.exists()
    assert "activations are limited" in capsys.readouterr().out

    # Refuse to clobber an existing archive.
    assert setup.main(["--prefix", str(prefix), "--backup-prefix", str(archive)]) == 1

    restored = tmp_path / "restored"
    assert setup.main(["--prefix", str(restored), "--restore-prefix", str(archive)]) == 0
    assert (restored / "drive_c/activation.marker").read_text(encoding="utf-8") == "activated"


def test_restore_refuses_a_non_empty_prefix(tmp_path: Path, capsys):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"")
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "existing").write_text("x", encoding="utf-8")

    assert setup.main(["--prefix", str(prefix), "--restore-prefix", str(archive)]) == 1
    assert "refusing" in capsys.readouterr().out


def test_activate_reports_a_missing_reader(tmp_path: Path, capsys):
    code = setup.main(["--prefix", str(tmp_path / "prefix"), "--activate"])
    assert code == 1
    assert "install it first" in capsys.readouterr().out


def test_probe_formatting_marks_optional_and_required(monkeypatch):
    probes = [
        setup.Probe("wine", True, "/usr/bin/wine", ""),
        setup.Probe("xdotool", False, "not found", "install xdotool"),
        setup.Probe("tesseract", False, "not found", "optional extra", required=False),
    ]
    lines = [setup._format(probe) for probe in probes]
    assert "[OK  ]" in lines[0]
    assert "→ install xdotool" in lines[1]
    assert "[opt ]" in lines[2] and "→" in lines[2]


def test_report_writes_only_to_stdout(capsys):
    code = setup.report([setup.Probe("wine", True, "wine-9.0", "")])
    assert code == 0
    assert "wine-9.0" in capsys.readouterr().out


def test_setup_module_never_calls_an_installer():
    source = Path(setup.__file__).read_text(encoding="utf-8")
    for forbidden in ("winetricks", "apt-get", "dnf install", "pip install", "winget"):
        assert forbidden not in source
