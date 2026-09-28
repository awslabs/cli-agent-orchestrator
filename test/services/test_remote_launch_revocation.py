"""A revoked owner may not start work on the remote launch route (#745).

``POST /runtimes/{id}/terminals`` (via ``launch_remote_terminal``) must return
403 BEFORE it journals or dispatches anything when the owner principal is
revoked. The owner-id-to-Principal conversion is the same one inbox delivery
uses (``Principal.parse``), and the detail text matches the main.py routes.
"""

import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import cli_agent_orchestrator.clients.database as db
import cli_agent_orchestrator.runtime_channel.api as api
from cli_agent_orchestrator.runtime_channel.protocol import CommandOutcome
from cli_agent_orchestrator.security.principal import revocation

RUNTIME = "worker-1"
TID = "abcd1234"
OWNER = "https://idp.example/#auth0|member"


@pytest.fixture()
def temp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False})
    db.Base.metadata.create_all(engine)
    TempSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db, "SessionLocal", TempSession)
    return TempSession


@pytest.fixture(autouse=True)
def _clean_revocation():
    revocation.reset()
    yield
    revocation.reset()


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


@pytest.mark.asyncio
async def test_a_revoked_owner_is_403_and_nothing_is_journaled(temp_db, monkeypatch):
    revocation.reset([OWNER])
    conn = MagicMock()
    conn.send_command = AsyncMock(return_value=_launch_ok())
    conn.ack = AsyncMock()
    registry = MagicMock()
    registry.get_runtime.return_value = conn
    monkeypatch.setattr(api, "runtime_registry", registry)

    with pytest.raises(api.HTTPException) as ei:
        await api.launch_remote_terminal(RUNTIME, _body(), owner_id=OWNER)

    assert ei.value.status_code == 403
    assert "revoked" in ei.value.detail
    # Nothing was journaled and nothing was dispatched.
    conn.send_command.assert_not_awaited()
    with temp_db() as s:
        assert s.query(db.DispatchJournalModel).count() == 0


@pytest.mark.asyncio
async def test_an_unrevoked_owner_launches_normally(temp_db, monkeypatch):
    revocation.reset()  # nobody revoked
    conn = MagicMock()
    conn.send_command = AsyncMock(return_value=_launch_ok())
    conn.ack = AsyncMock()
    registry = MagicMock()
    registry.get_runtime.return_value = conn
    monkeypatch.setattr(api, "runtime_registry", registry)
    monkeypatch.setattr(api, "db_create_terminal", MagicMock())

    terminal = await api.launch_remote_terminal(RUNTIME, _body(), owner_id=OWNER)

    assert terminal.id == TID
    conn.send_command.assert_awaited()
