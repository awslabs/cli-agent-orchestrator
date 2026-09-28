"""Settle definitively failed remote launches; keep unknown outcomes unsettled.

haefeif re#15 + Augusto on PR #802: the dispatch journal only settled on the
launch happy path, so every definitively failed launch left a permanent
``dispatched`` row that the janitor (which prunes only settled rows) could never
reclaim. The arms whose outcome is KNOWN must settle:

  * 503 ``RuntimeNotDispatchedError`` — provably nothing on the wire;
  * 502 non-OK result — matched and acked, cannot be reconciled later;
  * 500 persist failure AFTER a confirmed teardown — known clean.

The arms whose outcome is UNKNOWN must NOT settle (the runtime may redeliver):

  * 504 base ``RuntimeUnavailableError`` and 504 ``TimeoutError``;
  * 500 persist failure with an UNCONFIRMED teardown (the agent may be live).

haefeif re#12: the 500 detail must state whether the compensating teardown was
confirmed, and when it was not, name the terminal id / runtime and say the agent
may still be running — not the unconditional "tore it down".
"""

import asyncio
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import cli_agent_orchestrator.clients.database as db
import cli_agent_orchestrator.runtime_channel.api as api
from cli_agent_orchestrator.runtime_channel.protocol import CommandOutcome, CommandType
from cli_agent_orchestrator.runtime_channel.registry import RuntimeNotDispatchedError

RUNTIME = "worker-1"
TID = "abcd1234"


@pytest.fixture()
def temp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False})
    db.Base.metadata.create_all(engine)
    TempSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db, "SessionLocal", TempSession)
    return TempSession


def _launch_ok():
    return SimpleNamespace(
        outcome=CommandOutcome.OK,
        payload={
            "terminal": {
                "id": TID,
                "session_name": "cao-abcd1234",
                "name": "developer-abcd",
                "provider": "kiro_cli",
                "status": "idle",
            }
        },
    )


def _body():
    return api.CreateRemoteTerminalBody(agent_profile="developer")


def _only_row_state(temp_db):
    with temp_db() as s:
        rows = s.query(db.DispatchJournalModel).all()
        assert len(rows) == 1
        return rows[0].state


def _wire(monkeypatch, conn):
    registry = MagicMock()
    registry.get_runtime.return_value = conn
    monkeypatch.setattr(api, "runtime_registry", registry)
    return registry


@pytest.mark.asyncio
async def test_503_not_dispatched_settles_the_journal(temp_db, monkeypatch):
    conn = MagicMock()
    conn.send_command = AsyncMock(side_effect=RuntimeNotDispatchedError("closed"))
    conn.ack = AsyncMock()
    _wire(monkeypatch, conn)

    with pytest.raises(api.HTTPException) as ei:
        await api.launch_remote_terminal(RUNTIME, _body(), owner_id=None)

    assert ei.value.status_code == 503
    assert _only_row_state(temp_db) == "settled"


@pytest.mark.asyncio
async def test_504_timeout_leaves_the_journal_unsettled(temp_db, monkeypatch):
    conn = MagicMock()
    conn.send_command = AsyncMock(side_effect=TimeoutError())
    conn.ack = AsyncMock()
    _wire(monkeypatch, conn)

    with pytest.raises(api.HTTPException) as ei:
        await api.launch_remote_terminal(RUNTIME, _body(), owner_id=None)

    assert ei.value.status_code == 504
    assert _only_row_state(temp_db) == "dispatched"


@pytest.mark.asyncio
async def test_502_non_ok_result_settles_and_acks(temp_db, monkeypatch):
    conn = MagicMock()
    conn.send_command = AsyncMock(
        return_value=SimpleNamespace(outcome=CommandOutcome.FAILED, payload={"error": "boom"})
    )
    conn.ack = AsyncMock()
    _wire(monkeypatch, conn)

    with pytest.raises(api.HTTPException) as ei:
        await api.launch_remote_terminal(RUNTIME, _body(), owner_id=None)

    assert ei.value.status_code == 502
    assert _only_row_state(temp_db) == "settled"
    conn.ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_500_after_a_confirmed_teardown_settles(temp_db, monkeypatch):
    conn = MagicMock()
    conn.send_command = AsyncMock(
        side_effect=[
            _launch_ok(),
            SimpleNamespace(outcome=CommandOutcome.OK, payload={"deleted": True}),
        ]
    )
    conn.ack = AsyncMock()
    _wire(monkeypatch, conn)
    monkeypatch.setattr(api, "db_create_terminal", MagicMock(side_effect=RuntimeError("db down")))

    with pytest.raises(api.HTTPException) as ei:
        await api.launch_remote_terminal(RUNTIME, _body(), owner_id=None)

    assert ei.value.status_code == 500
    assert "tore it down" in ei.value.detail
    assert _only_row_state(temp_db) == "settled"


@pytest.mark.asyncio
async def test_500_after_an_unconfirmed_teardown_stays_unsettled_and_is_honest(
    temp_db, monkeypatch
):
    conn = MagicMock()
    conn.send_command = AsyncMock(
        side_effect=[
            _launch_ok(),
            SimpleNamespace(outcome=CommandOutcome.FAILED, payload={}),
        ]
    )
    conn.ack = AsyncMock()
    _wire(monkeypatch, conn)
    monkeypatch.setattr(api, "db_create_terminal", MagicMock(side_effect=RuntimeError("db down")))

    with pytest.raises(api.HTTPException) as ei:
        await api.launch_remote_terminal(RUNTIME, _body(), owner_id=None)

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert "tore it down" not in detail
    assert TID in detail
    assert RUNTIME in detail
    assert "may still be running" in detail
    assert _only_row_state(temp_db) == "dispatched"
