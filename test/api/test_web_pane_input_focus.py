"""Browser terminals on pane-mode agents deliver input to their own pane only.

In pane spawn mode several agents are panes of one tmux window, and tmux keeps
one active pane per window, not per client: keys go to whichever pane is active
when tmux reads them. ``/terminals/{id}/ws`` therefore takes the attach argv and
the pane from ONE backend lookup, refuses the attach when that lookup fails,
and serialises input per window: a terminal selects its pane, writes, and holds
the window until tmux has read every byte, on every input frame. Input whose
pane cannot be selected is dropped, never delivered to another agent's pane.
"""

import asyncio
import gc
import json
import os
import threading
import time
import tty
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect

from cli_agent_orchestrator.api import main as main_module
from cli_agent_orchestrator.backends.base import TerminalBackendError
from cli_agent_orchestrator.clients.database import create_terminal as db_create
from cli_agent_orchestrator.clients.tmux import TmuxLookupError

_TID, _TID_B = "5a1e0c42", "5a1e0c43"
_SCOPE, _PANE, _PANE_B = "@3", "%7", "%8"
_ARGV = ["tmux", "attach-session", "-t", "caoweb-x"]

pytestmark = pytest.mark.usefixtures("isolated_memory_db")


@pytest.fixture(autouse=True)
def _rows(isolated_memory_db):
    db_create(_TID, "cao-session", "worker-abcd", "claude_code", "developer")
    db_create(_TID_B, "cao-session", "worker-efgh", "claude_code", "developer")


@contextmanager
def _pty_attach(plan):
    """Real PTYs for the endpoint, the attach client faked, every step recorded.

    ``plan`` is what ``prepare_web_attach_input`` answers: an ``(argv, focus)``
    pair, an exception to raise, or a callable of ``window_name``. ``events``
    interleaves ``("focus", pane)``, ``("write", fd, bytes)`` and
    ``("drain", fd)`` in the order they happened, so a test can assert the pane
    was selected before its bytes and held until they were read.
    """
    state = SimpleNamespace(events=[], stamps=[], masters={}, peers=[], first_write=asyncio.Event())
    real_openpty, real_write = os.openpty, os.write

    def _openpty():
        master, slave = real_openpty()
        peer = os.dup(slave)
        tty.setraw(peer)
        state.peers.append(peer)
        state.masters[master] = os.ttyname(slave)
        return master, slave

    def _write(fd, data):
        if fd in state.masters:
            state.events.append(("write", fd, bytes(data)))
            state.stamps.append(("write", fd, time.monotonic()))
            state.first_write.set()
        return real_write(fd, data)

    async def _drain(slave_path):
        fd = next(m for m, path in state.masters.items() if path == slave_path)
        state.events.append(("drain", fd))
        return True

    def _prepare(session_name, window_name):
        if isinstance(plan, BaseException):
            raise plan
        return plan(window_name) if callable(plan) else plan

    backend = MagicMock()
    backend.prepare_web_attach_input.side_effect = _prepare

    def _focus(pane):
        state.events.append(("focus", pane))
        state.stamps.append(("focus", pane, time.monotonic()))

    backend.focus_web_input.side_effect = _focus
    backend.web_input_escape_hold.return_value = 0.0
    state.backend = backend
    proc = MagicMock()
    proc.poll.return_value = None
    with (
        patch.object(main_module, "get_backend", return_value=backend),
        patch.object(main_module.pty, "openpty", side_effect=_openpty),
        patch.object(main_module.subprocess, "Popen", return_value=proc) as popen,
        patch.object(main_module.os, "write", side_effect=_write),
        patch.object(main_module.os, "kill"),
        patch.object(main_module, "_wait_for_pty_drain", side_effect=_drain),
    ):
        state.popen = popen
        try:
            yield state
        finally:
            for peer in state.peers:
                os.close(peer)


async def _attach(steps, terminal_id=_TID):
    ws = MagicMock()
    ws.client = MagicMock(host="127.0.0.1")
    ws.headers = {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_bytes = AsyncMock()
    steps = list(steps)

    async def receive_text():
        while steps:
            step = steps.pop(0)
            if isinstance(step, str):
                return step
            result = step()
            if asyncio.iscoroutine(result):
                await result
        raise WebSocketDisconnect()

    ws.receive_text = receive_text
    with patch.object(main_module, "WS_ALLOWED_CLIENTS", ["127.0.0.1"]):
        await main_module.terminal_ws(ws, terminal_id)
    return ws


def _input(data):
    return json.dumps({"type": "input", "data": data})


def _kinds(events):
    return [e[0] for e in events]


class TestPaneTerminalsSelectTheirOwnPane:
    @pytest.mark.asyncio
    async def test_every_frame_selects_writes_and_drains_in_that_order(self):
        """No cache: an operator in a real terminal may move the pane at any time."""
        with _pty_attach((_ARGV, (_SCOPE, _PANE))) as state:
            await _attach([_input("A-1"), _input("A-2")])

        assert _kinds(state.events) == [
            "focus",  # on connect
            "focus",
            "write",
            "drain",
            "focus",
            "write",
            "drain",
        ]
        assert [e[2] for e in state.events if e[0] == "write"] == [b"A-1", b"A-2"]
        assert {e[1] for e in state.events if e[0] == "focus"} == {_PANE}

    @pytest.mark.asyncio
    async def test_input_is_dropped_when_the_pane_cannot_be_selected(self, caplog):
        with _pty_attach((_ARGV, (_SCOPE, _PANE))) as state:
            state.backend.focus_web_input.side_effect = TmuxLookupError("can't find pane: %7")
            await _attach([_input("WRONG-AGENT-CANARY"), _input("AGAIN")])

        assert [e for e in state.events if e[0] == "write"] == []
        drops = [r for r in caplog.records if "could not focus its pane" in r.getMessage()]
        assert len(drops) == 1

    @pytest.mark.asyncio
    async def test_the_window_lock_is_released_when_the_terminal_disconnects(self):
        with _pty_attach((_ARGV, (_SCOPE, _PANE))):
            await _attach([_input("A-1")])

        gc.collect()
        assert _SCOPE not in main_module._WEB_INPUT_WINDOWS


class TestTwoTerminalsOnOneWindow:
    @pytest.mark.asyncio
    async def test_a_key_typed_during_a_paste_waits_for_the_whole_paste(self):
        """fanhongy's P2: B's selection must not land between A's chunks."""
        panes = {"worker-abcd": _PANE, "worker-efgh": _PANE_B}
        paste = "x" * 3000  # three 1 KiB chunks, with awaits in between

        with _pty_attach(lambda window: (_ARGV, (_SCOPE, panes[window]))) as state:

            async def b_types_once_a_is_mid_paste():
                await state.first_write.wait()

            await asyncio.gather(
                _attach([_input(paste)], _TID),
                _attach([b_types_once_a_is_mid_paste, _input("b")], _TID_B),
            )

        writes = [e for e in state.events if e[0] == "write"]
        a_fd = writes[0][1]
        a_writes = [i for i, e in enumerate(state.events) if e[0] == "write" and e[1] == a_fd]
        b_write = next(i for i, e in enumerate(state.events) if e[0] == "write" and e[1] != a_fd)
        b_focus_before_its_write = max(
            i for i, e in enumerate(state.events[:b_write]) if e[0] == "focus" and e[1] == _PANE_B
        )
        a_drain = state.events.index(("drain", a_fd))

        assert len(a_writes) == 3
        assert b"".join(state.events[i][2] for i in a_writes) == paste.encode()
        # A's whole paste, then A's drain, then B's selection and key.
        assert a_writes[-1] < a_drain < b_focus_before_its_write < b_write

    def test_terminals_on_different_windows_get_different_locks(self):
        window_3 = main_module._web_input_window("@3")
        window_4 = main_module._web_input_window("@4")

        assert window_3.lock is not window_4.lock
        assert main_module._web_input_window("@3") is window_3


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        await asyncio.sleep(0.005)


def _stamp(state, kind, key, nth=0):
    return [s[2] for s in state.stamps if s[0] == kind and s[1] == key][nth]


class TestALoneEscapeHoldsTheWindow:
    """haofeif's P2: tmux keeps a trailing Escape as a partial key until its
    escape-time expires, then delivers it to whichever pane is active then."""

    HOLD = 0.3
    PANES = {"worker-abcd": _PANE, "worker-efgh": _PANE_B}

    def _plan(self, window):
        return _ARGV, (_SCOPE, self.PANES[window])

    @pytest.mark.asyncio
    async def test_another_terminal_waits_out_the_hold_before_selecting(self):
        with _pty_attach(self._plan) as state:
            state.backend.web_input_escape_hold.return_value = self.HOLD
            a_connected = asyncio.Event()

            async def b_types_right_after_a_escapes():
                await state.first_write.wait()

            await asyncio.gather(
                _attach(
                    [a_connected.set, _input("\x1b"), lambda: asyncio.sleep(self.HOLD * 2)], _TID
                ),
                _attach([a_connected.wait, b_types_right_after_a_escapes, _input("b")], _TID_B),
            )

        a_fd = next(e[1] for e in state.events if e[0] == "write" and e[2] == b"\x1b")
        escape_at = _stamp(state, "write", a_fd)
        b_select_at = max(s[2] for s in state.stamps if s[0] == "focus" and s[1] == _PANE_B)
        assert b_select_at - escape_at >= self.HOLD

    @pytest.mark.asyncio
    async def test_a_terminal_that_connects_during_the_hold_selects_after_it(self):
        with _pty_attach(self._plan) as state:
            state.backend.web_input_escape_hold.return_value = self.HOLD

            async def b_connects_right_after_a_escapes():
                await state.first_write.wait()
                await _attach([], _TID_B)

            await asyncio.gather(
                # A stays connected; its view (and tmux's partial key) outlive the frame.
                _attach([_input("\x1b"), lambda: asyncio.sleep(self.HOLD * 2)], _TID),
                b_connects_right_after_a_escapes(),
            )

        a_fd = next(e[1] for e in state.events if e[0] == "write")
        assert _stamp(state, "focus", _PANE_B) - _stamp(state, "write", a_fd) >= self.HOLD

    @pytest.mark.asyncio
    async def test_the_sender_does_not_wait_for_its_own_escape(self):
        with _pty_attach(self._plan) as state:
            state.backend.web_input_escape_hold.return_value = 5.0
            started = time.monotonic()
            await _attach([_input("\x1b"), _input("x")])

        assert [e[2] for e in state.events if e[0] == "write"] == [b"\x1b", b"x"]
        assert time.monotonic() - started < 2.0

    @pytest.mark.asyncio
    async def test_the_senders_next_frame_releases_the_hold(self):
        with _pty_attach(self._plan) as state:
            state.backend.web_input_escape_hold.return_value = 5.0

            def a_wrote(n):
                return lambda: len([e for e in state.events if e[0] == "write"]) >= n

            async def b_types_after_a_moved_on():
                await _until(a_wrote(2))

            started = time.monotonic()
            await asyncio.gather(
                _attach([_input("\x1b"), _input("x")], _TID),
                _attach([b_types_after_a_moved_on, _input("b")], _TID_B),
            )

        assert [e[2] for e in state.events if e[0] == "write"] == [b"\x1b", b"x", b"b"]
        assert time.monotonic() - started < 2.0

    @pytest.mark.parametrize(
        "answer", [MagicMock(), -1, True, "0.1"], ids=["mock", "negative", "bool", "str"]
    )
    @pytest.mark.asyncio
    async def test_an_unusable_answer_falls_back_to_a_safe_hold(self, answer):
        seen = []
        real_after_write = main_module._WebInputWindow.after_write

        def spy(self, owner, raw, escape_hold_s):
            seen.append(escape_hold_s)
            return real_after_write(self, owner, raw, escape_hold_s)

        with _pty_attach(self._plan) as state:
            state.backend.web_input_escape_hold.return_value = answer
            with patch.object(main_module._WebInputWindow, "after_write", spy):
                await _attach([_input("\x1b")])

        assert seen == [main_module._WEB_INPUT_ESCAPE_HOLD_FALLBACK_S]

    @pytest.mark.asyncio
    async def test_window_terminals_never_ask_for_the_hold(self):
        with _pty_attach((_ARGV, None)) as state:
            await _attach([_input("\x1b")])

        state.backend.web_input_escape_hold.assert_not_called()


class TestWebInputWindow:
    @pytest.mark.asyncio
    async def test_only_a_trailing_escape_leaves_a_hold(self):
        window, a = main_module._WebInputWindow(), object()

        window.after_write(a, b"\x1b[A", 5.0)  # a whole arrow-key sequence
        assert window.hold_owner is None
        window.after_write(a, b"abc\x1b", 5.0)
        assert window.hold_owner is a

    @pytest.mark.asyncio
    async def test_only_the_owner_skips_the_hold(self):
        window, a, b = main_module._WebInputWindow(), object(), object()
        window.after_write(a, b"\x1b", 0.2)

        started = time.monotonic()
        await window.wait_for_hold(a)
        assert time.monotonic() - started < 0.1
        await window.wait_for_hold(b)
        assert time.monotonic() - started >= 0.2

    @pytest.mark.asyncio
    async def test_another_terminals_frame_does_not_clear_the_owners_hold(self):
        window, a, b = main_module._WebInputWindow(), object(), object()
        window.after_write(a, b"\x1b", 5.0)

        window.after_write(b, b"b", 5.0)
        assert window.hold_owner is a
        window.after_write(a, b"x", 5.0)
        assert window.hold_owner is None

    @pytest.mark.asyncio
    async def test_a_waiter_wakes_as_soon_as_the_owner_releases_the_hold(self):
        """Not at the end of the hold: the owner's next frame ends it early."""
        window, a, b = main_module._WebInputWindow(), object(), object()
        window.after_write(a, b"\x1b", 5.0)

        started = time.monotonic()
        waiter = asyncio.create_task(window.wait_for_hold(b))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        window.after_write(a, b"x", 5.0)
        await asyncio.wait_for(waiter, timeout=1.0)
        assert time.monotonic() - started < 1.0

    @pytest.mark.asyncio
    async def test_a_new_hold_after_a_release_holds_again(self):
        window, a, b = main_module._WebInputWindow(), object(), object()
        window.after_write(a, b"\x1b", 5.0)
        window.after_write(a, b"x", 5.0)  # released
        window.after_write(a, b"\x1b", 0.2)  # a fresh hold, not the set event

        started = time.monotonic()
        await window.wait_for_hold(b)
        assert time.monotonic() - started >= 0.2


class TestTheAttachFailsClosed:
    @pytest.mark.asyncio
    async def test_a_lookup_failure_refuses_the_attach(self):
        with _pty_attach(TerminalBackendError("listing failed to parse")) as state:
            ws = await _attach([_input("WRONG-AGENT-CANARY")])

        ws.close.assert_awaited_once_with(code=4004, reason="Failed to attach terminal")
        state.popen.assert_not_called()
        assert state.events == []

    @pytest.mark.parametrize(
        "focus",
        [MagicMock(), ("@3",), ("@3", None), ("", "%7"), ["@3", "%7"]],
        ids=["mock", "short", "none-pane", "empty-scope", "list"],
    )
    @pytest.mark.asyncio
    async def test_a_malformed_focus_refuses_the_attach(self, focus):
        with _pty_attach((_ARGV, focus)) as state:
            ws = await _attach([_input("WRONG-AGENT-CANARY")])

        ws.close.assert_awaited_once_with(code=4004, reason="Failed to attach terminal")
        state.popen.assert_not_called()
        assert state.events == []


class TestWindowTerminalsNeedNoFocus:
    @pytest.mark.asyncio
    async def test_no_selection_no_drain_and_input_flows(self):
        with _pty_attach((_ARGV, None)) as state:
            await _attach([_input("W-1")])

        state.backend.focus_web_input.assert_not_called()
        assert [(e[0], e[2]) for e in state.events] == [("write", b"W-1")]


class TestThePtyDrainWait:
    """``_wait_for_pty_drain`` against a real PTY."""

    @pytest.mark.asyncio
    async def test_returns_once_the_reader_has_read_everything(self):
        master, slave = os.openpty()
        tty.setraw(slave)
        try:
            path = os.ttyname(slave)
            os.write(master, b"queued-input")
            time.sleep(0.05)  # let the line discipline take the bytes
            assert main_module._pty_unread_bytes(path) == len(b"queued-input")

            def read_later():
                time.sleep(0.05)
                os.read(slave, 64)

            reader = threading.Thread(target=read_later)
            started = time.monotonic()
            reader.start()
            assert await main_module._wait_for_pty_drain(path) is True
            assert time.monotonic() - started >= 0.05
            reader.join()
        finally:
            os.close(master)
            os.close(slave)

    @pytest.mark.asyncio
    async def test_gives_up_when_nothing_reads(self):
        master, slave = os.openpty()
        tty.setraw(slave)
        try:
            os.write(master, b"never-read")
            with patch.object(main_module, "_WEB_INPUT_DRAIN_TIMEOUT_S", 0.1):
                assert await main_module._wait_for_pty_drain(os.ttyname(slave)) is False
        finally:
            os.close(master)
            os.close(slave)

    def test_a_vanished_pty_has_nothing_unread(self):
        assert main_module._pty_unread_bytes("/dev/pts/does-not-exist") == 0
