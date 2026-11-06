"""Browser terminals on pane-mode agents select their own pane before input.

In pane spawn mode several agents are panes of one tmux window, and tmux keeps
one active pane per window, not per client. ``/terminals/{id}/ws`` therefore
selects the terminal's pane when it connects, and again before forwarding
input whenever another connection selected a pane of that window since (or its
own selection is stale). If the pane cannot be selected, the input is dropped
rather than delivered to whichever agent's pane happens to be active.
"""

import asyncio
import json
import os
import time
import tty
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect

from cli_agent_orchestrator.api import main as main_module
from cli_agent_orchestrator.clients.database import create_terminal as db_create
from cli_agent_orchestrator.clients.tmux import TmuxLookupError

_TID = "5a1e0c42"
_SCOPE, _PANE = "@3", "%7"

pytestmark = pytest.mark.usefixtures("isolated_memory_db")


@pytest.fixture(autouse=True)
def _row_and_clean_registry(isolated_memory_db):
    db_create(_TID, "cao-session", "worker-abcd", "claude_code", "developer")
    main_module._WEB_INPUT_FOCUS.clear()
    yield
    main_module._WEB_INPUT_FOCUS.clear()


@contextmanager
def _pty_attach(focus):
    """A real PTY for the endpoint, the attach client faked, writes recorded.

    ``events`` interleaves ``("focus", target)`` and ``("write", bytes)`` so a
    test can assert the pane was selected before the bytes reached the PTY.
    """
    state = SimpleNamespace(master=None, peer=None, events=[])
    real_openpty, real_write = os.openpty, os.write

    def _openpty():
        state.master, slave = real_openpty()
        state.peer = os.dup(slave)
        tty.setraw(state.peer)
        return state.master, slave

    def _write(fd, data):
        if state.master is not None and fd == state.master:
            state.events.append(("write", bytes(data)))
        return real_write(fd, data)

    backend = MagicMock()
    backend.prepare_web_attach.return_value = ["tmux", "attach-session", "-t", "caoweb-x"]
    backend.web_input_focus.return_value = focus
    backend.focus_web_input.side_effect = lambda target: state.events.append(("focus", target))
    state.backend = backend
    proc = MagicMock()
    proc.poll.return_value = None
    with (
        patch.object(main_module, "get_backend", return_value=backend),
        patch.object(main_module.pty, "openpty", side_effect=_openpty),
        patch.object(main_module.subprocess, "Popen", return_value=proc),
        patch.object(main_module.os, "write", side_effect=_write),
        patch.object(main_module.os, "kill"),
    ):
        try:
            yield state
        finally:
            if state.peer is not None:
                os.close(state.peer)


async def _attach(steps):
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
        await main_module.terminal_ws(ws, _TID)
    return ws


def _input(data):
    return json.dumps({"type": "input", "data": data})


class TestPaneTerminalsSelectTheirOwnPane:
    @pytest.mark.asyncio
    async def test_the_pane_is_selected_on_connect_before_any_input(self):
        with _pty_attach((_SCOPE, _PANE)) as state:
            await _attach([_input("A-1"), _input("A-2")])

        assert state.events[0] == ("focus", _PANE)
        assert [e for e in state.events if e[0] == "write"] == [
            ("write", b"A-1"),
            ("write", b"A-2"),
        ]
        # Still the holder for both keys: one selection, not one per key.
        assert [e for e in state.events if e[0] == "focus"] == [("focus", _PANE)]

    @pytest.mark.asyncio
    async def test_input_reselects_after_another_terminal_took_the_window(self):
        def another_terminal_selects():
            main_module._WEB_INPUT_FOCUS[_SCOPE] = (object(), time.monotonic())

        with _pty_attach((_SCOPE, _PANE)) as state:
            await _attach([_input("A-1"), another_terminal_selects, _input("A-2")])

        assert state.events == [
            ("focus", _PANE),
            ("write", b"A-1"),
            ("focus", _PANE),
            ("write", b"A-2"),
        ]

    @pytest.mark.asyncio
    async def test_a_stale_selection_is_reasserted(self):
        def age_own_selection():
            token, _ = main_module._WEB_INPUT_FOCUS[_SCOPE]
            main_module._WEB_INPUT_FOCUS[_SCOPE] = (token, time.monotonic() - 60)

        with _pty_attach((_SCOPE, _PANE)) as state:
            await _attach([_input("A-1"), age_own_selection, _input("A-2")])

        assert [e[0] for e in state.events] == ["focus", "write", "focus", "write"]

    @pytest.mark.asyncio
    async def test_input_is_dropped_when_the_pane_cannot_be_selected(self, caplog):
        with _pty_attach((_SCOPE, _PANE)) as state:
            state.backend.focus_web_input.side_effect = TmuxLookupError("can't find pane: %7")
            await _attach([_input("WRONG-AGENT-CANARY"), _input("AGAIN")])

        assert [e for e in state.events if e[0] == "write"] == []
        drops = [r for r in caplog.records if "could not focus its pane" in r.getMessage()]
        assert len(drops) == 1

    @pytest.mark.asyncio
    async def test_the_window_scope_is_released_when_the_terminal_disconnects(self):
        with _pty_attach((_SCOPE, _PANE)):
            await _attach([_input("A-1")])

        assert _SCOPE not in main_module._WEB_INPUT_FOCUS

    @pytest.mark.asyncio
    async def test_a_disconnect_does_not_release_another_terminals_scope(self):
        other = (object(), time.monotonic())

        def another_terminal_selects():
            main_module._WEB_INPUT_FOCUS[_SCOPE] = other

        with _pty_attach((_SCOPE, _PANE)):
            await _attach([_input("A-1"), another_terminal_selects])

        assert main_module._WEB_INPUT_FOCUS[_SCOPE] is other


class TestWindowTerminalsNeedNoFocus:
    @pytest.mark.asyncio
    async def test_no_selection_and_input_flows(self):
        with _pty_attach(None) as state:
            await _attach([_input("W-1")])

        state.backend.focus_web_input.assert_not_called()
        assert state.events == [("write", b"W-1")]

    @pytest.mark.asyncio
    async def test_a_malformed_focus_answer_is_ignored(self):
        with _pty_attach(MagicMock()) as state:
            await _attach([_input("W-1")])

        state.backend.focus_web_input.assert_not_called()
        assert state.events == [("write", b"W-1")]
