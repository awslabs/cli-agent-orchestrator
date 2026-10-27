"""The Kiro identity input gate at the API (#1074).

``POST /terminals/{id}/input``, ``/key`` and ``/exit`` answer 409, never 500, for
a Kiro terminal whose row does not record a passed identity check, whether its
init is still running here or has ended. ``/terminals/{id}/ws`` forwards output
but drops input for such a terminal, re-checking each input frame, and refuses
the attach before accept only when CAO already reclaimed that runtime.
"""

import asyncio
import fcntl
import json
import logging
import os
import struct
import termios
import time
import tty
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect

from cli_agent_orchestrator.api import main as main_module
from cli_agent_orchestrator.clients.database import create_terminal as db_create
from cli_agent_orchestrator.clients.database import (
    mark_terminal_identity_verified,
    update_terminal_deferred_init_failure,
    update_terminal_deferred_init_runtime_reclaimed,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service

_TID = "1e293d67"
_FAILURE = {"phase": "deferred_init", "kind": "provider_init_error", "untrusted_runtime": True}

pytestmark = pytest.mark.usefixtures("isolated_memory_db")


def _row(*, identity_verified, provider="kiro_cli", failure=None, reclaimed=False):
    db_create(
        _TID,
        "cao-session",
        "developer-abcd",
        provider,
        "developer",
        deferred_init_external_owner=True,
        identity_verified=identity_verified,
    )
    if failure is not None:
        assert update_terminal_deferred_init_failure(_TID, failure)
    if reclaimed:
        assert update_terminal_deferred_init_runtime_reclaimed(_TID, True)
    return _TID


@pytest.fixture
def pending():
    """Mark the row's init as running in this process, as create_terminal does."""
    terminal_service._mark_identity_check_in_flight(_TID)
    try:
        yield
    finally:
        terminal_service._clear_identity_check_in_flight(_TID)


@pytest.fixture
def pane(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", tmp_path / "logs")
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.get_pane_current_command.return_value = "kiro-cli"
    provider = MagicMock()
    provider.paste_enter_count = 1
    provider.paste_submit_delay = 0
    provider.assume_processing_on_dispatch = False
    provider.shell_baseline = None
    provider.exit_cli.return_value = "/exit"
    with (
        patch.object(terminal_service, "get_backend", return_value=backend),
        patch.object(main_module, "get_backend", return_value=backend),
        patch.object(terminal_service.provider_manager, "get_provider", return_value=provider),
        patch.object(
            terminal_service.status_monitor, "get_status", return_value=TerminalStatus.IDLE
        ),
        patch.object(terminal_service, "_dialog_blocks_orchestrated_input", return_value=False),
        patch.object(terminal_service, "inject_memory_context", side_effect=lambda m, *a, **k: m),
    ):
        yield SimpleNamespace(backend=backend, provider=provider)


def _nothing_typed(backend):
    backend.send_keys.assert_not_called()
    backend.send_special_key.assert_not_called()


class TestTheWriteRoutesRefuseAnUnverifiedKiroTerminal:
    @pytest.mark.parametrize("exit_command", ["/exit", "C-d"])
    def test_a_terminal_still_initializing_is_409_on_every_route(
        self, client, pane, pending, exit_command
    ):
        _row(identity_verified=False)
        pane.provider.exit_cli.return_value = exit_command

        responses = [
            client.post(f"/terminals/{_TID}/input", params={"message": "SEND-CANARY"}),
            client.post(f"/terminals/{_TID}/key", params={"key": "Enter"}),
            client.post(f"/terminals/{_TID}/exit"),
        ]

        for response in responses:
            assert response.status_code == 409
            assert "has not passed its Kiro identity check yet" in response.json()["detail"]
        _nothing_typed(pane.backend)

    @pytest.mark.parametrize("exit_command", ["/exit", "C-d"])
    def test_a_terminal_whose_init_ended_unverified_is_409_on_every_route(
        self, client, pane, exit_command
    ):
        _row(identity_verified=False)
        pane.provider.exit_cli.return_value = exit_command

        responses = [
            client.post(f"/terminals/{_TID}/input", params={"message": "SEND-CANARY"}),
            client.post(f"/terminals/{_TID}/key", params={"key": "x"}),
            client.post(f"/terminals/{_TID}/exit"),
        ]

        for response in responses:
            assert response.status_code == 409
            detail = response.json()["detail"]
            assert "(identity_unverified)" in detail and "Refusing to deliver input" in detail
            assert "Traceback" not in detail and "/" not in detail.split("(")[0]
        _nothing_typed(pane.backend)

    def test_a_verified_terminal_takes_input_and_keys(self, client, pane):
        _row(identity_verified=True)

        assert client.post(f"/terminals/{_TID}/input", params={"message": "SEND-CANARY"}).json()[
            "success"
        ]
        assert client.post(f"/terminals/{_TID}/key", params={"key": "Enter"}).status_code == 200

        assert pane.backend.send_keys.call_args.args[2] == "SEND-CANARY"
        pane.backend.send_special_key.assert_called_once_with(
            "cao-session", "developer-abcd", "Enter"
        )


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.01)


def _frame(kind, **fields):
    return json.dumps({"type": kind, **fields})


@contextmanager
def _pty_attach():
    """A real PTY pair for the endpoint, with the attach client faked.

    ``peer`` is a raw-mode duplicate of the slave end, standing in for the
    attached backend client: what it writes is the pane's output, and every
    ``os.write`` to the master end is recorded in ``writes``.
    """
    state = SimpleNamespace(master=None, peer=None, writes=[], opened=0)
    real_openpty, real_write = os.openpty, os.write

    def _openpty():
        state.master, slave = real_openpty()
        state.peer = os.dup(slave)
        tty.setraw(state.peer)
        state.opened += 1
        return state.master, slave

    def _write(fd, data):
        if state.master is not None and fd == state.master:
            state.writes.append(bytes(data))
        return real_write(fd, data)

    proc = MagicMock()
    proc.poll.return_value = None
    backend = MagicMock()
    backend.prepare_web_attach.return_value = ["tmux", "attach-session", "-t", "cao-session"]
    state.backend = backend
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


def _ws_double():
    """A WebSocket client the IP and Origin checks admit (loopback, no Origin)."""
    ws = MagicMock()
    ws.client = MagicMock(host="127.0.0.1")
    ws.headers = {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_bytes = AsyncMock()
    return ws


async def _attach(steps, ws=None):
    """Run the endpoint with a WebSocket double that plays ``steps``.

    A string step is the next frame the client sends; a callable step runs
    (and is awaited if it returns a coroutine) before the next frame is read.
    """
    ws = ws or _ws_double()
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


def _output_reaches(state, ws):
    """A step: the attached client prints, and the endpoint forwards it."""

    async def step():
        os.write(state.peer, b"BOOT-OUTPUT")
        await _until(lambda: ws.send_bytes.await_count > 0)

    return step


class TestTheWebAttachIsOutputOnlyUntilVerified:
    @pytest.mark.asyncio
    async def test_a_reclaimed_untrusted_runtime_refuses_the_attach(self):
        _row(identity_verified=False, failure=_FAILURE, reclaimed=True)

        with _pty_attach() as state:
            ws = await _attach([])

        ws.accept.assert_not_called()
        ws.close.assert_awaited_once_with(code=4409, reason="Terminal runtime was reclaimed")
        state.backend.prepare_web_attach.assert_not_called()
        assert state.opened == 0

    @pytest.mark.asyncio
    async def test_an_unconfirmed_reclaim_gets_output_and_resizes_but_no_input(self):
        _row(identity_verified=False, failure=_FAILURE, reclaimed=False)
        sizes = []

        with _pty_attach() as state:

            def _measure():
                raw = fcntl.ioctl(state.peer, termios.TIOCGWINSZ, b"\0" * 8)
                sizes.append(struct.unpack("HHHH", raw)[:2])

            ws = _ws_double()
            await _attach(
                [
                    _output_reaches(state, ws),
                    _frame("input", data="WS-CANARY\r"),
                    _frame("resize", rows=40, cols=100),
                    _measure,
                ],
                ws,
            )

        ws.accept.assert_awaited_once()
        state.backend.prepare_web_attach.assert_called_once_with("cao-session", "developer-abcd")
        sent = b"".join(c.args[0] for c in ws.send_bytes.await_args_list)
        assert b"BOOT-OUTPUT" in sent
        assert state.writes == []
        assert sizes == [(40, 100)]

    @pytest.mark.asyncio
    async def test_a_pending_terminal_starts_taking_input_once_verified(self, pending, caplog):
        _row(identity_verified=False)

        with caplog.at_level(logging.WARNING, logger=main_module.logger.name):
            with _pty_attach() as state:
                ws = await _attach(
                    [
                        _frame("input", data="EARLY-1"),
                        _frame("input", data="EARLY-2"),
                        lambda: mark_terminal_identity_verified(_TID),
                        _frame("input", data="LATE-CANARY"),
                        _frame("input", data="AFTER"),
                    ],
                )

        ws.accept.assert_awaited_once()
        assert state.writes == [b"LATE-CANARY", b"AFTER"]
        drops = [r for r in caplog.records if "Dropping WebSocket input" in r.getMessage()]
        assert len(drops) == 1 and "identity_pending" in drops[0].getMessage()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "row",
        [
            {"identity_verified": True},
            {"identity_verified": None},
            {"identity_verified": None, "provider": "claude_code", "failure": _FAILURE},
        ],
        ids=["kiro-verified", "kiro-legacy", "other-provider-failed"],
    )
    async def test_a_trusted_terminal_is_unchanged(self, row):
        _row(**row)
        # Under one PTY input buffer: nothing reads the slave end here.
        data = "é→ " + "x" * 600

        with (
            _pty_attach() as state,
            patch.object(
                main_module, "_ws_input_refusal", wraps=main_module._ws_input_refusal
            ) as check,
        ):
            await _attach([_frame("input", data=data)])

        assert b"".join(state.writes) == data.encode()
        assert check.call_count == 1  # at attach only: no per-frame cost

    @pytest.mark.asyncio
    async def test_a_check_that_raises_drops_the_frame(self, pending):
        _row(identity_verified=False)
        real = main_module._ws_input_refusal
        calls = []

        def _check(terminal_id):
            calls.append(terminal_id)
            if len(calls) > 1:
                raise RuntimeError("database is locked")
            return real(terminal_id)

        with _pty_attach() as state, patch.object(main_module, "_ws_input_refusal", _check):
            mark = lambda: mark_terminal_identity_verified(_TID)  # noqa: E731
            await _attach([mark, _frame("input", data="WS-CANARY")])

        assert len(calls) == 2
        assert state.writes == []
