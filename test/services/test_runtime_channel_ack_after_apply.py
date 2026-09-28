"""Ack LAUNCH / RUN_SCRIPT results only after their outcome is durably applied.

The frame reader acked a matched result the instant
``conn.resolve`` woke the waiter — before ``launch_remote_terminal`` wrote the
central row and bound routing, and before ``_drive_process_remote`` settled the
workflow run. The ack is what lets the runtime drop its only retained copy of
the result, so acking early orphans a live agent (no row to route to) or leaves
a redelivered RUN_SCRIPT's run RUNNING forever when the journal write fails.

The two state-creating op types now defer their ack to the coroutine that
applies the outcome: the reader does not ack them, and the coroutine acks only
after "persist, bind, settle" (LAUNCH) or "finalize, settle" (RUN_SCRIPT).
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
from cli_agent_orchestrator.runtime_channel.protocol import (
    AckFrame,
    CommandOutcome,
    CommandResultFrame,
    CommandType,
    decode_frame,
)
from cli_agent_orchestrator.runtime_channel.registry import (
    RuntimeConnection,
    runtime_registry,
)
from cli_agent_orchestrator.services import script_runner


@pytest.fixture()
def temp_db(monkeypatch):
    """Point clients.database.SessionLocal at a fresh temp SQLite file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False})
    db.Base.metadata.create_all(engine)
    TempSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db, "SessionLocal", TempSession)
    return TempSession


# --------------------------------------------------------------------------- #
# RuntimeConnection: the deferral flag and the ack sender
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_only_senders_that_opt_in_defer_their_ack():
    """Deferral is explicit: a sender that applies durable state asks for it.

    A RUN_SCRIPT sent without ``defer_ack`` (a flow pre-script, which applies
    nothing durable) is acked by the reader; otherwise its retained result would
    sit unacked in the runtime until the next reconnect.
    """
    sent = []

    async def send_text(text):
        sent.append(text)

    conn = RuntimeConnection("worker-1", send_text)

    async def _pending(command_type, op_id, defer_ack):
        task = asyncio.ensure_future(
            conn.send_command(command_type, {}, op_id=op_id, timeout=5, defer_ack=defer_ack)
        )
        await asyncio.sleep(0)  # let send_command register the op
        return task

    launch_task = await _pending(CommandType.LAUNCH, "op-L", True)
    input_task = await _pending(CommandType.INPUT, "op-I", False)
    script_task = await _pending(CommandType.RUN_SCRIPT, "op-S", True)
    prescript_task = await _pending(CommandType.RUN_SCRIPT, "op-P", False)

    assert conn.ack_is_deferred("op-L") is True
    assert conn.ack_is_deferred("op-S") is True
    assert conn.ack_is_deferred("op-I") is False
    assert conn.ack_is_deferred("op-P") is False

    for op_id, task in (
        ("op-L", launch_task),
        ("op-I", input_task),
        ("op-S", script_task),
        ("op-P", prescript_task),
    ):
        conn.resolve(CommandResultFrame(op_id=op_id, outcome=CommandOutcome.OK, payload={}))
        await task
    assert not conn.ack_is_deferred("op-L"), "the opt-in is cleared when the op completes"


@pytest.mark.asyncio
async def test_ack_sends_an_ackframe():
    sent = []

    async def send_text(text):
        sent.append(text)

    conn = RuntimeConnection("worker-1", send_text)
    await conn.ack("op-x")
    frames = [decode_frame(t) for t in sent]
    assert any(isinstance(f, AckFrame) and f.op_id == "op-x" for f in frames)


@pytest.mark.asyncio
async def test_ack_swallows_a_send_failure():
    async def send_text(text):
        raise ConnectionError("socket gone")

    conn = RuntimeConnection("worker-1", send_text)
    # Must not raise: a lost ack is recovered by redelivery.
    await conn.ack("op-x")


# --------------------------------------------------------------------------- #
# launch_remote_terminal: ack follows the durable write
# --------------------------------------------------------------------------- #
RUNTIME = "worker-1"
TID = "abcd1234"


def _launch_result():
    terminal = {
        "id": TID,
        "session_name": "cao-abcd1234",
        "name": "developer-abcd",
        "provider": "kiro_cli",
        "agent_profile": "developer",
        "status": "idle",
    }
    return SimpleNamespace(outcome=CommandOutcome.OK, payload={"terminal": terminal})


def _body(**kw):
    base = dict(agent_profile="developer")
    base.update(kw)
    return api.CreateRemoteTerminalBody(**base)


@pytest.mark.asyncio
async def test_launch_ok_acks_only_after_the_central_row_exists(temp_db, monkeypatch):
    observed = {}

    async def ack(op_id):
        observed["row_at_ack"] = db.get_terminal_metadata(TID)

    conn = MagicMock()
    conn.send_command = AsyncMock(return_value=_launch_result())
    conn.ack = AsyncMock(side_effect=ack)
    registry = MagicMock()
    registry.get_runtime.return_value = conn
    monkeypatch.setattr(api, "runtime_registry", registry)

    await api.launch_remote_terminal(RUNTIME, _body(), owner_id="cao:local#local")

    conn.ack.assert_awaited_once()
    assert observed["row_at_ack"] is not None, "the row must exist at the moment of ack"


@pytest.mark.asyncio
async def test_a_matched_launch_that_fails_to_persist_and_is_unconfirmed_never_acks(
    temp_db, monkeypatch
):
    conn = MagicMock()
    # LAUNCH ok, then a TEARDOWN that does NOT confirm cleanup.
    conn.send_command = AsyncMock(
        side_effect=[
            _launch_result(),
            SimpleNamespace(outcome=CommandOutcome.FAILED, payload={}),
        ]
    )
    conn.ack = AsyncMock()
    registry = MagicMock()
    registry.get_runtime.return_value = conn
    monkeypatch.setattr(api, "runtime_registry", registry)
    monkeypatch.setattr(
        api, "db_create_terminal", MagicMock(side_effect=RuntimeError("volume full"))
    )

    with pytest.raises(api.HTTPException) as ei:
        await api.launch_remote_terminal(RUNTIME, _body(), owner_id="cao:local#local")

    assert ei.value.status_code == 500
    conn.ack.assert_not_awaited()


# --------------------------------------------------------------------------- #
# _drive_process_remote: ack after finalize + settle
# --------------------------------------------------------------------------- #
def _script_record():
    return SimpleNamespace(run_id="run-1", step_id="step-1", remote_script=None)


@pytest.mark.asyncio
async def test_run_script_acks_only_after_finalize_and_the_journal_is_settled(
    temp_db, monkeypatch, tmp_path
):
    script = tmp_path / "wf.py"
    script.write_text("print('hi')\n")

    events = []

    async def fake_finalize(*a, **k):
        events.append("finalize")
        return "RESULT"

    monkeypatch.setattr(script_runner, "_interpret_and_finalize", fake_finalize)

    async def ack(op_id):
        rec = db.get_dispatch_record(op_id)
        events.append(("ack", rec["state"] if rec else None))

    conn = MagicMock()
    conn.send_command = AsyncMock(return_value=SimpleNamespace(payload={}))
    conn.ack = AsyncMock(side_effect=ack)

    monkeypatch.setattr(runtime_registry, "get_runtime", lambda rid: conn)

    out = await script_runner._drive_process_remote(_script_record(), RUNTIME, str(script), {})

    assert out == "RESULT"
    conn.ack.assert_awaited_once()
    assert events[0] == "finalize"
    assert events[1] == ("ack", "settled"), "ack must follow finalize AND settle"


@pytest.mark.asyncio
async def test_run_script_finalize_raising_neither_settles_nor_acks(temp_db, monkeypatch, tmp_path):
    script = tmp_path / "wf.py"
    script.write_text("print('hi')\n")

    async def fake_finalize(*a, **k):
        raise RuntimeError("finalize blew up")

    monkeypatch.setattr(script_runner, "_interpret_and_finalize", fake_finalize)

    conn = MagicMock()
    conn.send_command = AsyncMock(return_value=SimpleNamespace(payload={}))
    conn.ack = AsyncMock()
    monkeypatch.setattr(runtime_registry, "get_runtime", lambda rid: conn)

    with pytest.raises(RuntimeError):
        await script_runner._drive_process_remote(_script_record(), RUNTIME, str(script), {})

    conn.ack.assert_not_awaited()
    with temp_db() as s:
        rows = s.query(db.DispatchJournalModel).all()
        assert len(rows) == 1
        assert rows[0].state == "dispatched"


@pytest.mark.asyncio
async def test_run_script_not_dispatched_settles_and_reports_not_connected(
    temp_db, monkeypatch, tmp_path
):
    script = tmp_path / "wf.py"
    script.write_text("print('hi')\n")

    captured = {}

    async def fake_finalize(record, **k):
        captured.update(k)
        return "FAILED-RESULT"

    monkeypatch.setattr(script_runner, "_finalize", fake_finalize)

    conn = MagicMock()
    conn.send_command = AsyncMock(
        side_effect=script_runner_RuntimeNotDispatchedError("channel closed")
    )
    conn.ack = AsyncMock()
    monkeypatch.setattr(runtime_registry, "get_runtime", lambda rid: conn)

    out = await script_runner._drive_process_remote(_script_record(), RUNTIME, str(script), {})

    assert out == "FAILED-RESULT"
    assert "is not connected" in (captured.get("error") or "")
    assert "outcome unknown" not in (captured.get("error") or "")
    conn.ack.assert_not_awaited()
    with temp_db() as s:
        rows = s.query(db.DispatchJournalModel).all()
        assert len(rows) == 1
        assert rows[0].state == "settled"


def script_runner_RuntimeNotDispatchedError(msg):
    from cli_agent_orchestrator.runtime_channel.registry import RuntimeNotDispatchedError

    return RuntimeNotDispatchedError(msg)


# --------------------------------------------------------------------------- #
# orphan RUN_SCRIPT: a failing workflow-journal write is neither acked nor settled
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_orphan_run_script_with_a_failing_journal_write_is_not_acked_or_settled(
    temp_db, monkeypatch
):
    from cli_agent_orchestrator.services import workflow_journal

    def boom(*a, **k):
        raise RuntimeError("workflow journal is read-only")

    monkeypatch.setattr(workflow_journal, "settle_run_state_if_running", boom)

    op_id = "op-orphan-script"
    db.record_dispatch(op_id, CommandType.RUN_SCRIPT.value, RUNTIME, run_id="run-1")
    frame = CommandResultFrame(op_id=op_id, outcome=CommandOutcome.OK, payload={})

    safe = await api._reconcile_orphaned_result(frame, RUNTIME)

    assert safe is False, "a failed workflow-journal write must NOT be acked"
    assert db.get_dispatch_record(op_id)["state"] == "dispatched", "must not settle"
