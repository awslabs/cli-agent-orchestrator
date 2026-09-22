"""launch_remote_terminal persists what it launched, and tears down what it can't.

Two findings on #802 (guojing1217):
- The engine the caller pinned is forwarded in LAUNCH and honored by the bridge,
  but was never passed to db_create_terminal, so a remote assign/handoff that
  pinned an engine recorded engine=None centrally — where reuse validation and
  the KAS input gate read it.
- If persisting the row raises after the provider has already launched in the
  runtime, there is no row and no binding: nothing can route to or tear down the
  agent, a leaked pod holding a live model session. A best-effort TEARDOWN on the
  same connection bounds it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from cli_agent_orchestrator.runtime_channel import api as rc_api
from cli_agent_orchestrator.runtime_channel.api import (
    CreateRemoteTerminalBody,
    launch_remote_terminal,
)
from cli_agent_orchestrator.runtime_channel.protocol import CommandOutcome, CommandType

RUNTIME = "worker-1"
TID = "abcd1234"


def _launch_result(**terminal_extra):
    terminal = {
        "id": TID,
        "session_name": "cao-abcd1234",
        "name": "developer-abcd",
        "provider": "kiro_cli",
        "agent_profile": "developer",
        "status": "initializing",
    }
    terminal.update(terminal_extra)
    return SimpleNamespace(outcome=CommandOutcome.OK, payload={"terminal": terminal}, op_id="op-1")


@pytest.fixture
def wired(monkeypatch):
    """A connected runtime and a recording registry, with send_command stubbed."""
    conn = MagicMock()
    conn.send_command = AsyncMock(return_value=_launch_result())

    registry = MagicMock()
    registry.get_runtime.return_value = conn
    monkeypatch.setattr(rc_api, "runtime_registry", registry)
    return SimpleNamespace(conn=conn, registry=registry)


@pytest.mark.asyncio
async def test_the_pinned_engine_is_persisted(wired, monkeypatch):
    wired.conn.send_command = AsyncMock(return_value=_launch_result())
    created = {}

    def fake_db_create(*args, **kwargs):
        created.update(kwargs)

    monkeypatch.setattr(rc_api, "db_create_terminal", fake_db_create)

    body = CreateRemoteTerminalBody(agent_profile="developer", engine="kas")
    result = await launch_remote_terminal(RUNTIME, body, owner_id="cao:local#local")

    assert created["engine"] == "kas"
    assert result.engine == "kas"


@pytest.mark.asyncio
async def test_the_runtimes_engine_echo_wins_over_the_request(wired, monkeypatch):
    wired.conn.send_command = AsyncMock(return_value=_launch_result(engine="v2"))
    created = {}
    monkeypatch.setattr(rc_api, "db_create_terminal", lambda *a, **k: created.update(k))

    body = CreateRemoteTerminalBody(agent_profile="developer", engine="kas")
    await launch_remote_terminal(RUNTIME, body, owner_id=None)

    assert created["engine"] == "v2"


@pytest.mark.asyncio
async def test_a_persist_failure_tears_the_leaked_terminal_back_down(wired, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(rc_api, "db_create_terminal", boom)

    body = CreateRemoteTerminalBody(agent_profile="developer")
    with pytest.raises(HTTPException) as exc:
        await launch_remote_terminal(RUNTIME, body, owner_id=None)

    assert exc.value.status_code == 500
    # A compensating TEARDOWN went to the same connection for the leaked id.
    teardown = [
        c
        for c in wired.conn.send_command.await_args_list
        if c.args and c.args[0] == CommandType.TEARDOWN
    ]
    assert teardown, "the launched-but-unpersisted terminal must be torn down"
    assert teardown[0].kwargs.get("terminal_id") == TID
    # It was never bound, since binding follows a successful persist.
    wired.registry.bind_terminal.assert_not_called()


@pytest.mark.asyncio
async def test_a_teardown_that_itself_fails_does_not_mask_the_launch_error(wired, monkeypatch):
    monkeypatch.setattr(
        rc_api, "db_create_terminal", MagicMock(side_effect=RuntimeError("db down"))
    )
    # First call is the LAUNCH (ok); the compensating TEARDOWN then also fails.
    wired.conn.send_command = AsyncMock(
        side_effect=[_launch_result(), RuntimeError("teardown unreachable")]
    )

    body = CreateRemoteTerminalBody(agent_profile="developer")
    with pytest.raises(HTTPException) as exc:
        await launch_remote_terminal(RUNTIME, body, owner_id=None)
    assert exc.value.status_code == 500
