"""X11 plumbing against a real display — but not against the reader.

What this proves: a private display starts and stops, a window is found by
title, a frame is captured from *that window*, and the digest/blank/crop
helpers behave on real captures rather than on fixtures.

What it cannot prove: that a page turn advances a page. That needs the reader,
which CI cannot run, so it stays an operator gate (see the README). The test
skips with a printed reason when the host lacks the tooling, rather than
pretending the plumbing works.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from acsm import capture

REQUIRED = ("Xvfb", "xdotool", "xmessage", capture.WINDOW_MANAGER)


def _missing() -> list[str]:
    gaps = [name for name in REQUIRED if shutil.which(name) is None]
    if shutil.which("import") is None and not (
        shutil.which("xwd") and shutil.which("convert")
    ):
        gaps.append("import (or xwd + convert)")
    return gaps


@pytest.fixture(scope="module")
def display_env():
    gaps = _missing()
    if gaps:
        pytest.skip("X11 integration tooling not installed: " + ", ".join(gaps))
    return True


async def test_capture_round_trips_through_a_real_display(display_env):
    display = capture.Display()
    async with display:
        window = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            ["xmessage", "-title", "acsm-stub-reader", "-buttons", "", "stub page"],
            env=capture._child_env(display.display),
        )
        try:
            await asyncio.sleep(1.5)
            window_id = await capture.find_window(display.display, "acsm-stub-reader")

            raw = await capture.grab_png(display.display, window_id)
            assert raw.startswith(b"\x89PNG"), "a capture must be a PNG, not a path to one"

            cropped = capture.crop_png(raw, (0, 0, 200, 120))
            assert capture.frame_digest(cropped) == capture.frame_digest(
                capture.crop_png(raw, (0, 0, 200, 120))
            ), "the same pixels must digest identically"
            assert isinstance(capture.is_blank(cropped), bool)

            # A key must reach the pipeline without raising; whether the window
            # acts on it is the reader's business, not this test's.
            await capture.send_turn(display.display, window_id)
            await capture.send_start(display.display, window_id)
        finally:
            window.terminate()
            window.wait(timeout=10)


async def test_a_private_display_is_torn_down_after_use(display_env):
    display = capture.Display()
    async with display:
        socket = display.socket_path
        assert subprocess.run(  # noqa: S603
            ["test", "-e", socket], check=False
        ).returncode == 0
    assert not display._processes, "no child process may outlive the display"


async def test_grabbing_a_window_that_does_not_exist_fails_loudly(display_env):
    display = capture.Display()
    async with display:
        with pytest.raises(RuntimeError, match="no visible window"):
            await capture.find_window(display.display, "definitely-not-here")
