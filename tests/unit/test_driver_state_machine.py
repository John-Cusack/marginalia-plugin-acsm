"""The page loop's state machine, exercised with no display and no reader.

Every transition the spec names (FR-007 … FR-009, SC-004) has a test here:
steady progress, a lost turn that recovers, a stale budget exhausted, an empty
run, a stall that restarts the reader, a page cap, and a reader-signalled end.
"""

from __future__ import annotations

import pytest

from acsm.driver import (
    CANCELLED,
    EMPTY,
    MAX_PAGES,
    OBSERVED_END,
    STALE,
    drive_document,
)

BLANK = b"BLANK"


class FakeWindows:
    """Scripted pixels: each segment is the capture sequence between restarts."""

    def __init__(self, *scripts: list[bytes], texts: dict[bytes, str] | None = None) -> None:
        self.scripts = [list(script) for script in scripts]
        self.segment = 0
        self.position = 0
        self.texts = texts or {}
        self.sent_turns = 0
        self.sent_starts = 0
        self.restarts = 0
        self.accepted: list[bytes] = []
        self.last_page_tokens: set[bytes] = set()

    def _script(self) -> list[bytes]:
        return self.scripts[min(self.segment, len(self.scripts) - 1)]

    async def capture(self, index: int) -> bytes:
        script = self._script()
        token = script[min(self.position, len(script) - 1)]
        self.position += 1
        return token

    async def send_turn(self) -> None:
        self.sent_turns += 1

    async def send_start(self) -> None:
        self.sent_starts += 1

    async def on_restart(self) -> None:
        self.restarts += 1
        self.segment += 1
        self.position = 0

    def accept_page(self, png: bytes) -> str:
        self.accepted.append(png)
        return self.texts.get(png, f"text-{png.decode()}")

    async def is_last_page(self, png: bytes) -> bool:
        return png in self.last_page_tokens


class Clock:
    """Deterministic clock the injected sleep advances."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def drive(windows: FakeWindows, clock: Clock, **overrides):
    kwargs = {
        "capture": windows.capture,
        "send_turn": windows.send_turn,
        "send_start": windows.send_start,
        "digest_of": bytes.decode,
        "is_blank": lambda png: png == BLANK,
        "on_restart": windows.on_restart,
        "accept_page": windows.accept_page,
        "sleep": clock.sleep,
        "clock": clock,
    }
    kwargs.update(overrides)
    return drive_document(**kwargs)


async def test_steady_progress_then_stale_end():
    windows = FakeWindows([b"p1", b"p2", b"p3"])
    clock = Clock()
    result = await drive(windows, clock, max_consecutive_stale=2)

    assert [page.text for page in result.pages] == ["text-p1", "text-p2", "text-p3"]
    assert result.ended_by == STALE
    assert result.complete is False
    assert windows.restarts == 0
    assert windows.sent_starts == 1


async def test_reader_signalled_end_is_the_only_complete_result():
    windows = FakeWindows([b"p1", b"p2", b"p3", b"p4"])
    windows.last_page_tokens = {b"p3"}
    clock = Clock()
    result = await drive(windows, clock, is_last_page=windows.is_last_page)

    assert result.ended_by == OBSERVED_END
    assert result.complete is True
    assert len(result.pages) == 3
    assert windows.accepted == [b"p1", b"p2", b"p3"]


async def test_lost_turn_recovers_within_the_stale_budget():
    # p1 twice: the second capture has the same digest, so the turn is re-sent.
    windows = FakeWindows([b"p1", b"p1", b"p2"])
    clock = Clock()
    result = await drive(windows, clock, max_consecutive_stale=3)

    assert [page.text for page in result.pages] == ["text-p1", "text-p2"]
    assert windows.accepted == [b"p1", b"p2"]  # the repeated frame is not a page
    assert result.stale_runs >= 1  # it did happen, and was counted


async def test_stale_budget_exhausted_ends_the_capture():
    windows = FakeWindows([b"p1"])
    clock = Clock()
    result = await drive(windows, clock, max_consecutive_stale=2)

    assert len(result.pages) == 1
    assert result.ended_by == STALE
    assert result.stale_runs >= 2
    assert windows.restarts == 0


async def test_empty_run_ends_the_capture():
    windows = FakeWindows([b"p1", b"p2", b"p3"], texts={b"p1": "", b"p2": "", b"p3": ""})
    clock = Clock()
    result = await drive(windows, clock, max_consecutive_empty=3)

    assert result.ended_by == EMPTY
    assert len(result.pages) == 3


async def test_empty_counter_resets_on_the_next_page_with_text():
    windows = FakeWindows(
        [b"p1", b"p2", b"p3", b"p4"],
        texts={b"p1": "", b"p2": "", b"p3": "text", b"p4": "text"},
    )
    clock = Clock()
    # Two empty pages would trip a threshold of 2; with 3 they must not, and the
    # run must continue past them (the p3 page resets the counter).
    result = await drive(windows, clock, max_consecutive_empty=3, max_consecutive_stale=2)

    assert result.ended_by == STALE
    assert len(result.pages) == 4
    assert windows.accepted == [b"p1", b"p2", b"p3", b"p4"]


async def test_stall_restarts_the_reader_and_resumes():
    windows = FakeWindows([b"p1"], [b"p2", b"p3"])
    clock = Clock()
    result = await drive(
        windows,
        clock,
        settle_timeout=5.0,
        stall_timeout=10.0,
        max_consecutive_stale=50,
        max_restarts=1,
    )

    # The first segment never advances past p1, so the stall watchdog fires;
    # the second segment is captured from after the restart.
    assert result.restarts == 1
    assert [page.text for page in result.pages] == ["text-p1", "text-p2", "text-p3"]
    assert windows.sent_starts >= 2  # the reader is re-homed after a restart
    assert result.ended_by == STALE  # and the capture gives up on the next stall


async def test_restarts_are_bounded_and_then_the_capture_gives_up():
    windows = FakeWindows([b"p1"])  # every segment repeats the same page
    clock = Clock()
    result = await drive(
        windows, clock, settle_timeout=5.0, stall_timeout=10.0, max_consecutive_stale=50
    )

    assert result.restarts == 3  # max_restarts default
    assert result.ended_by == STALE


async def test_max_pages_stops_the_capture():
    windows = FakeWindows([b"p1", b"p2", b"p3", b"p4", b"p5"])
    clock = Clock()
    result = await drive(windows, clock, max_pages=3)

    assert result.ended_by == MAX_PAGES
    assert len(result.pages) == 3
    assert result.complete is False


async def test_first_frames_stay_blank_until_the_settle_timeout_then_end_stale():
    windows = FakeWindows([BLANK])
    clock = Clock()
    result = await drive(windows, clock, settle_timeout=5.0, on_restart=None)

    assert result.pages == []
    assert result.ended_by == STALE
    assert windows.accepted == []


async def test_blank_frames_do_not_consume_the_stale_budget():
    # A blank frame between pages is an unrendered window, not a lost turn.
    windows = FakeWindows([BLANK, b"p1", b"p2"])
    clock = Clock()
    result = await drive(windows, clock, max_consecutive_stale=1, settle_timeout=60.0)

    assert [page.text for page in result.pages] == ["text-p1", "text-p2"]


async def test_delays_stay_inside_the_configured_window():
    windows = FakeWindows([b"p1", b"p2", b"p3"])
    clock = Clock()
    await drive(windows, clock, page_delay_min=2.0, page_delay_max=3.0)

    assert clock.slept, "the loop must settle between turns"
    assert all(2.0 <= delay <= 3.0 for delay in clock.slept)


async def test_a_caller_timeout_is_reported_as_partial_not_as_an_error():
    windows = FakeWindows([b"p1"])

    class Sleeping(Clock):
        async def sleep(self, seconds: float) -> None:
            raise TimeoutError("operator-imposed timeout")

    clock = Sleeping()
    result = await drive(windows, clock)

    assert result.ended_by == CANCELLED
    assert result.complete is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"page_delay_min": 0.0},
        {"page_delay_min": 3.0, "page_delay_max": 1.0},
        {"max_consecutive_stale": 0},
        {"max_consecutive_empty": 0},
    ],
)
async def test_invalid_configuration_fails_before_touching_the_reader(overrides):
    windows = FakeWindows([b"p1"])
    clock = Clock()
    with pytest.raises(ValueError):
        await drive(windows, clock, **overrides)
    assert windows.sent_starts == 0
