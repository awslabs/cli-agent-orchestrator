"""Idle output that changes no cell must not hold the stale-PROCESSING gate shut (#1367).

``agy`` repeats one 39-byte block of terminal-mode sequences while it sits at its ready
prompt. ``test/providers/fixtures/agy_idle_mode_block_timed.json`` is that block with the
arrival times measured on a live pane: 60 arrivals in 125 s, 1.99 to 2.21 s apart. The block
changes no screen cell, but each arrival reset the 3 s quiet gate and dropped the pending
capture candidate, so ``get_status()`` never reached the ``capture-pane`` fallback and a
composite that still read PROCESSING was never corrected.

The tests drive the real ``StatusMonitor``, the real pyte compositor and the real agy provider
on a fake clock. Only ``capture-pane`` is faked, and it answers with the ready screen of the
same recorded turn.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Callable, List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
from cli_agent_orchestrator.services import status_monitor as sm_module
from cli_agent_orchestrator.services.status_monitor import (
    STALE_PROCESSING_BUFFER_QUIET_S,
    StatusMonitor,
)

FIXTURES = Path(__file__).resolve().parents[1] / "providers" / "fixtures"
TURN = (FIXTURES / "agy_turn_end_stale_esc_to_cancel.txt").read_bytes().decode("utf-8")
TIMED = json.loads((FIXTURES / "agy_idle_mode_block_timed.json").read_text(encoding="utf-8"))
MODE_BLOCK = bytes.fromhex(TIMED["chunk_hex"]).decode("ascii")

# The turn recorded in the fixture, cut where agy prints "ready for input": the screen at that
# byte is the turn still working (a spinner row and the "esc to cancel" footer).
WORKING = TURN[: TURN.index("\x1b]9;Antigravity CLI is ready for input")]

# The bound the issue asks for: how long after the last real output the fallback must have
# healed a stale composite. One quiet gate (3 s), then two reads one capture interval (3 s)
# apart; the poller below runs every second, so 10 s is the gate, the confirm and slack.
HEAL_WITHIN_S = 10.0
POLL_EVERY_S = 1.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def _rendered_ready_pane() -> str:
    """The pane after the recorded turn ends, as ``capture-pane`` would print it."""
    with _patched_monitor_deps(_tmux_backend()):
        monitor = StatusMonitor()
        provider = AntigravityCliProvider("t1", "s", "w")
        provider._turns = 1
        with patch.object(sm_module, "provider_manager") as pm:
            pm.get_provider.return_value = provider
            for i in range(0, len(TURN), 4096):
                monitor._process_chunk("t1", TURN[i : i + 4096])
        lines, _ = monitor._screen_lines("t1")
    rows = [ln.rstrip() for ln in (lines or [])]
    return "\n".join(rows).rstrip()


def _tmux_backend() -> MagicMock:
    """A tmux-like backend: no event inbox, no native agent state."""
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.get_native_status.return_value = None
    return backend


def _patched_monitor_deps(backend: MagicMock):
    """The collaborators a unit test cannot have: server settings, status queue, backend."""
    stack = types.SimpleNamespace()
    patches = [
        patch.object(sm_module, "get_server_settings", return_value={"state_buffer_max": 32768}),
        patch.object(sm_module, "_queue_status_transition"),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
    ]

    class _Ctx:
        def __enter__(self):
            for p in patches:
                p.start()
            return stack

        def __exit__(self, *exc):
            for p in reversed(patches):
                p.stop()

    return _Ctx()


def _working_monitor(backend: MagicMock, clock: _Clock):
    """A monitor whose composite is the working turn and whose status is PROCESSING."""
    provider = AntigravityCliProvider("t1", "s", "w")
    provider._turns = 1
    monitor = StatusMonitor()
    with patch.object(sm_module, "provider_manager") as pm:
        pm.get_provider.return_value = provider
        monitor.notify_input_sent("t1")
        for i in range(0, len(WORKING), 4096):
            monitor._process_chunk("t1", WORKING[i : i + 4096])
    assert monitor._detect_screen("t1", provider) == TerminalStatus.PROCESSING, "premise"
    monitor._last_status["t1"] = TerminalStatus.PROCESSING
    return monitor, provider


def _run_idle(
    chunk_at: Callable[[int], str], horizon_s: float
) -> Tuple[List[Tuple[float, TerminalStatus]], StatusMonitor, MagicMock]:
    """Replay the measured idle arrivals after the working turn, polling get_status().

    ``chunk_at(i)`` is the i-th idle chunk. Returns the (seconds since the last real output,
    status) pair of every poll, the monitor, and the backend.
    """
    clock = _Clock()
    backend = _tmux_backend()
    backend.get_history.return_value = _rendered_ready_pane()
    arrivals = [t for t in TIMED["arrival_s"] if t <= horizon_s]
    polls: List[Tuple[float, TerminalStatus]] = []
    fake_time = types.SimpleNamespace(monotonic=clock.monotonic)
    with _patched_monitor_deps(backend), patch.object(sm_module, "time", fake_time):
        monitor, provider = _working_monitor(backend, clock)
        start = clock.now
        events = sorted(
            [(t, "chunk", i) for i, t in enumerate(arrivals)]
            + [(k * POLL_EVERY_S, "poll", k) for k in range(1, int(horizon_s / POLL_EVERY_S) + 1)]
        )
        with patch.object(sm_module, "provider_manager") as pm:
            pm.get_provider.return_value = provider
            for t, kind, i in events:
                clock.now = start + t
                if kind == "chunk":
                    monitor._process_chunk("t1", chunk_at(i))
                else:
                    status = monitor.get_status("t1")
                    polls.append((t, status))
    return polls, monitor, backend


def _first_ready(polls: List[Tuple[float, TerminalStatus]]):
    ready = (TerminalStatus.IDLE, TerminalStatus.COMPLETED)
    return next((t for t, s in polls if s in ready), None)


def test_the_recording_measures_the_interval_the_issue_could_only_assume():
    """The measured block and interval: the facts the fix rests on."""
    arrivals = TIMED["arrival_s"]
    gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
    assert len(arrivals) == TIMED["chunk_count"] == 60
    assert TIMED["chunk_bytes"] == len(MODE_BLOCK) == 39
    assert min(gaps) > 1.9 and max(gaps) < 2.3
    # Every gap is shorter than the quiet gate, which is why the gate never opened.
    assert max(gaps) < STALE_PROCESSING_BUFFER_QUIET_S
    assert "\x1b[?2004h" in MODE_BLOCK and all(
        ch == "\x1b" or ch.isprintable() for ch in MODE_BLOCK
    )


def test_a_stale_composite_is_healed_while_agy_repeats_its_idle_mode_block():
    polls, monitor, backend = _run_idle(lambda i: MODE_BLOCK, horizon_s=120.0)

    healed_at = _first_ready(polls)
    assert healed_at is not None, (
        "get_status() stayed PROCESSING for the whole recording: the mode block reset the quiet "
        f"gate on each of its {len(TIMED['arrival_s'])} arrivals "
        f"({backend.get_history.call_count} capture-pane reads)"
    )
    assert healed_at <= HEAL_WITHIN_S, healed_at
    assert monitor._last_status["t1"] in (TerminalStatus.IDLE, TerminalStatus.COMPLETED)


def test_real_output_at_the_same_cadence_still_holds_the_gate_shut():
    """Control: a chunk that changes a cell every 2.1 s is activity, so no capture happens."""

    def spinner(i: int) -> str:
        return f"\x1b[1;1H{i:04d}"  # rewrites the first row's text each time

    polls, monitor, backend = _run_idle(spinner, horizon_s=60.0)

    assert _first_ready(polls) is None
    assert backend.get_history.call_count == 0
    assert all(status == TerminalStatus.PROCESSING for _, status in polls)


def test_a_mode_block_is_buffered_but_is_not_activity():
    clock = _Clock()
    backend = _tmux_backend()
    backend.get_history.return_value = ""
    fake_time = types.SimpleNamespace(monotonic=clock.monotonic)
    with _patched_monitor_deps(backend), patch.object(sm_module, "time", fake_time):
        monitor, provider = _working_monitor(backend, clock)
        changed_at = monitor._buffer_changed_at["t1"]
        generation = monitor._capture_generation["t1"]
        output_generation = monitor._output_generation["t1"]
        monitor._pending_stale_capture["t1"] = (TerminalStatus.IDLE, clock.now, generation)

        clock.now += 2.1
        with patch.object(sm_module, "provider_manager") as pm:
            pm.get_provider.return_value = provider
            monitor._process_chunk("t1", MODE_BLOCK)

        assert monitor._buffer_changed_at["t1"] == changed_at
        assert monitor._capture_generation["t1"] == generation
        assert "t1" in monitor._pending_stale_capture
        assert monitor._output_generation["t1"] == output_generation + 1
        assert monitor._buffers["t1"].endswith(MODE_BLOCK)

        clock.now += 2.1
        with patch.object(sm_module, "provider_manager") as pm:
            pm.get_provider.return_value = provider
            monitor._process_chunk("t1", "\x1b[1;1Hnew words")

        assert monitor._buffer_changed_at["t1"] == clock.now
        assert monitor._capture_generation["t1"] == generation + 1
        assert "t1" not in monitor._pending_stale_capture


def test_the_first_chunk_of_a_terminal_still_starts_the_gate_clock():
    """A terminal whose only output so far is mode output must not have no clock at all."""
    clock = _Clock()
    fake_time = types.SimpleNamespace(monotonic=clock.monotonic)
    provider = AntigravityCliProvider("t1", "s", "w")
    with _patched_monitor_deps(_tmux_backend()), patch.object(sm_module, "time", fake_time):
        monitor = StatusMonitor()
        with patch.object(sm_module, "provider_manager") as pm:
            pm.get_provider.return_value = provider
            monitor._process_chunk("t1", MODE_BLOCK)
    assert monitor._buffer_changed_at["t1"] == clock.now


ONE_CELL = "\x1b[2J\x1b[Hx"  # one visible "x" at the top left
# Every row of the 200-row compositor holds "x", cursor on the last row.
ALL_ROWS_X = "\x1b[2J\x1b[H" + "x\r\n" * (sm_module.PYTE_SCREEN_ROWS - 1) + "x"


def _claude():
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

    return ClaudeCodeProvider("t1", "s", "w")


def _agy():
    return AntigravityCliProvider("t1", "s", "w")


@pytest.mark.parametrize(
    "make_provider, setup, chunks, changes",
    [
        (_agy, ONE_CELL, [MODE_BLOCK], [False]),
        (_agy, ONE_CELL, ["\x1b[3;7H"], [False]),  # cursor move only
        (_agy, ONE_CELL, ["\x1b[38;5;153m\x1b[0m"], [False]),  # colour set and reset, no text
        (_agy, ONE_CELL, ["\x1b[H\x1b[31mx\x1b[0m"], [False]),  # colour on existing text
        (_agy, ONE_CELL, ["\x1b[Hx"], [False]),  # the same character again
        (_agy, ONE_CELL, ["\x1b[Hy"], [True]),  # a different character
        (_agy, ONE_CELL, ["\x1b[2J"], [True]),  # erases the one visible cell
        (_agy, ONE_CELL, ["\x1b[2J\x1b[Hx"], [False]),  # clear and redraw in one chunk
        (_agy, ONE_CELL, ["\x1b[2J", "\x1b[Hx"], [True, True]),  # clear, then redraw
        (_agy, ALL_ROWS_X, ["\r\nx"], [False]),  # scrolls one row; every row still reads "x"
        (_agy, ONE_CELL, ["\x1b[200;1H\r\n"], [True]),  # scroll moves "x" up a row
        (_claude, ONE_CELL, [MODE_BLOCK], [False]),  # same rule for another screen provider
        (_claude, ONE_CELL, ["\x1b[Hy"], [True]),
    ],
    ids=[
        "mode-block",
        "cursor-move",
        "colour-only",
        "colour-on-existing-text",
        "same-char",
        "new-char",
        "erase",
        "clear-and-redraw-one-chunk",
        "clear-then-redraw-two-chunks",
        "scroll-identical-rows",
        "scroll-moves-text",
        "claude-mode-block",
        "claude-new-char",
    ],
)
def test_which_chunks_count_as_changing_the_screen(make_provider, setup, chunks, changes):
    provider = make_provider()
    assert provider.supports_screen_detection is True
    monitor = StatusMonitor()
    seen = []
    with _patched_monitor_deps(_tmux_backend()):
        with patch.object(sm_module, "provider_manager") as pm:
            pm.get_provider.return_value = provider
            monitor._process_chunk("t1", setup)
            for chunk in chunks:
                before = monitor._capture_generation["t1"]
                monitor._process_chunk("t1", chunk)
                seen.append(monitor._capture_generation["t1"] == before + 1)
    assert seen == changes


def test_a_provider_without_a_screen_counts_every_chunk():
    """Raw detection has no composite to compare, so the old rule stands."""
    provider = MagicMock()
    provider.supports_screen_detection = False
    provider.supports_direct_status_probe = False
    provider.get_status.return_value = TerminalStatus.PROCESSING
    monitor = StatusMonitor()
    with _patched_monitor_deps(_tmux_backend()):
        with patch.object(sm_module, "provider_manager") as pm:
            pm.get_provider.return_value = provider
            monitor._process_chunk("t1", "spinner")
            before = monitor._capture_generation["t1"]
            monitor._process_chunk("t1", MODE_BLOCK)
    assert monitor._capture_generation["t1"] == before + 1
