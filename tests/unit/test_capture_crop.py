"""Pixels, digests and blank detection — no display required.

The reader-specific constants are placeholders until Phase 0 lands, so these
tests pin the *behaviour* around them (crop bounds, digest stability, blank
thresholds, fixed-argv subprocesses) rather than the values themselves.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from acsm import capture


def png(width: int = 200, height: int = 120, *, colour: tuple[int, int, int] = (255, 255, 255),
        text: str | None = None, box: tuple[int, int, int, int] | None = None) -> bytes:
    image = Image.new("RGB", (width, height), colour)
    draw = ImageDraw.Draw(image)
    if text:
        draw.text((10, 10), text, fill=(0, 0, 0))
    if box:
        draw.rectangle(box, fill=(0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def test_crop_png_returns_the_requested_region():
    assert size_of(capture.crop_png(png(), (0, 20, 100, 100))) == (100, 80)


def test_crop_png_clamps_to_the_frame_instead_of_failing_on_overflow():
    assert size_of(capture.crop_png(png(), (0, 0, 9999, 9999))) == (200, 120)


def test_crop_png_rejects_an_empty_box():
    with pytest.raises(ValueError):
        capture.crop_png(png(), (50, 50, 50, 90))


def test_frame_digest_is_stable_and_content_sensitive():
    base = png()
    assert capture.frame_digest(base) == capture.frame_digest(base)
    assert capture.frame_digest(base) != capture.frame_digest(png(text="different"))


def test_frame_digest_ignores_encoder_differences():
    # Same pixels, re-encoded: the digest must not move, or a re-encode would
    # look like a page turn.
    original = png(text="stable")
    with Image.open(io.BytesIO(original)) as image:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
    assert capture.frame_digest(original) == capture.frame_digest(buffer.getvalue())


def test_is_blank_separates_unrendered_from_rendered():
    assert capture.is_blank(png()) is True
    assert capture.is_blank(png(text="a page of text")) is False


def test_is_blank_threshold_decides_for_a_small_mark():
    # A small mark on a large frame does not make the frame "rendered"; whether
    # it counts as blank is the threshold's job, so assert both sides of it
    # rather than guessing where the line sits.
    marked = png(box=(0, 0, 6, 6))
    assert capture.is_blank(marked, threshold=20.0) is True
    assert capture.is_blank(marked, threshold=1.0) is False


def test_child_env_replaces_display_and_does_not_inherit_the_operator_display(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WINEPREFIX", "/operator/prefix")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = capture._child_env(":9")

    assert env["DISPLAY"] == ":9"
    assert env["PATH"] == "/usr/bin"
    assert "WINEPREFIX" not in env  # only fulfill.py passes the prefix, deliberately


def test_child_env_accepts_explicit_extras():
    env = capture._child_env(":9", {"WINEPREFIX": "/capture/prefix"})
    assert env["WINEPREFIX"] == "/capture/prefix"


class Recorded:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str], bytes | None]] = []

    async def __call__(self, argv, *, env, capture_stdout=False, input_bytes=None, timeout=60.0):
        self.calls.append((list(argv), dict(env), input_bytes))
        return b"PNG-BYTES"


async def test_send_turn_and_send_start_use_fixed_argv_and_no_shell(monkeypatch):
    recorded = Recorded()
    monkeypatch.setattr(capture, "run_command", recorded)

    await capture.send_turn(":9", "12345")
    await capture.send_start(":9", "12345")

    argv, env, _ = recorded.calls[0]
    assert argv[0] == "xdotool"
    assert "12345" in argv
    assert "Right" in argv
    assert env["DISPLAY"] == ":9"
    assert len(recorded.calls) == 2
    assert "Home" in " ".join(recorded.calls[1][0])


async def test_grab_png_prefers_import_and_pipes_nothing(monkeypatch):
    recorded = Recorded()
    monkeypatch.setattr(capture, "run_command", recorded)

    data = await capture.grab_png(":9", "12345", import_tool="/usr/bin/import")

    assert data == b"PNG-BYTES"
    argv, env, input_bytes = recorded.calls[0]
    assert argv == ["/usr/bin/import", "-window", "12345", "png:-"]
    assert input_bytes is None
    assert env["DISPLAY"] == ":9"


async def test_grab_png_falls_back_to_xwd_piped_through_convert(monkeypatch):
    recorded = Recorded()
    monkeypatch.setattr(capture, "run_command", recorded)

    await capture.grab_png(
        ":9", "12345", import_tool="", xwd_tool="/usr/bin/xwd", convert_tool="/usr/bin/convert"
    )

    assert [call[0][0] for call in recorded.calls] == ["/usr/bin/xwd", "/usr/bin/convert"]
    assert recorded.calls[1][2] == b"PNG-BYTES"  # the xwd output is piped in


async def test_grab_png_names_the_missing_tool_when_none_exists(monkeypatch):
    monkeypatch.setattr(capture, "run_command", Recorded())
    with pytest.raises(RuntimeError, match="no capture tool"):
        await capture.grab_png(":9", "12345", import_tool="", xwd_tool="", convert_tool="")


async def test_find_window_errors_when_nothing_matches(monkeypatch):
    async def empty(*args, **kwargs):
        return b""

    monkeypatch.setattr(capture, "run_command", empty)
    with pytest.raises(RuntimeError, match="no visible window"):
        await capture.find_window(":9")


async def test_find_window_uses_the_last_match_when_several_exist(monkeypatch):
    async def two(*args, **kwargs):
        return b"111\n222\n"

    monkeypatch.setattr(capture, "run_command", two)
    assert await capture.find_window(":9") == "222"


def test_available_tools_reports_every_probed_binary():
    tools = capture.available_tools()
    for name in (*capture.REQUIRED_TOOLS, capture.WINDOW_MANAGER, "wine"):
        assert name in tools
