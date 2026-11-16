"""``identity_verified`` on the terminal's API shape (#1131).

The Kiro identity gate (#1074) records on the row whether a ``kiro_cli``
terminal passed its identity check, and refuses input until it has. A client
outside CAO that picks terminals by ``status`` alone cannot see that refusal
coming, so ``GET /terminals/{id}`` and the session's terminal lists report the
stored value as it is: False and True for Kiro, None for other providers and
for Kiro rows older than the column, never coerced to False.
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilities
from cli_agent_orchestrator.services import session_service, terminal_service

_SESSION = "cao-fleet"
_PENDING, _VERIFIED, _OTHER, _LEGACY = "aaaa0001", "aaaa0002", "aaaa0003", "aaaa0004"
_EXPECTED = {_PENDING: False, _VERIFIED: True, _OTHER: None, _LEGACY: None}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine))
    yield engine
    engine.dispose()


@pytest.fixture
def rows(engine):
    # As create_terminal writes them: False with every Kiro row, None otherwise.
    db.create_terminal(_PENDING, _SESSION, "w1", "kiro_cli", "dev", identity_verified=False)
    db.create_terminal(_VERIFIED, _SESSION, "w2", "kiro_cli", "dev", identity_verified=False)
    assert db.mark_terminal_identity_verified(_VERIFIED)
    db.create_terminal(_OTHER, _SESSION, "w3", "claude_code", "dev", identity_verified=None)
    # A Kiro row written before the column existed reads back NULL.
    db.create_terminal(_LEGACY, _SESSION, "w4", "kiro_cli", "dev", identity_verified=None)
    return engine


def _idle(_terminal_id):
    return TerminalStatus.IDLE


@pytest.mark.parametrize("terminal_id", sorted(_EXPECTED))
def test_get_terminal_reports_identity_verified_as_stored(client, rows, terminal_id):
    with patch.object(terminal_service.status_monitor, "get_status", side_effect=_idle):
        response = client.get(f"/terminals/{terminal_id}")
    assert response.status_code == 200
    body = response.json()
    assert "identity_verified" in body
    assert body["identity_verified"] is _EXPECTED[terminal_id]


def test_pending_kiro_terminal_reads_idle_and_unverified(client, rows):
    """The case the issue is about: ``status`` alone says dispatchable, the gate refuses."""
    with patch.object(terminal_service.status_monitor, "get_status", side_effect=_idle):
        body = client.get(f"/terminals/{_PENDING}").json()
    assert body["status"] == "idle"
    assert body["identity_verified"] is False
    assert terminal_service.terminal_input_refusal(_PENDING, db.get_terminal_metadata(_PENDING))


def test_session_terminals_endpoint_reports_identity_verified(client, rows):
    backend = MagicMock()
    backend.session_exists_strict.return_value = False
    with patch.object(session_service, "get_backend", return_value=backend):
        response = client.get(f"/sessions/{_SESSION}/terminals")
    assert response.status_code == 200
    assert {t["id"]: t["identity_verified"] for t in response.json()} == _EXPECTED


def test_session_endpoint_reports_identity_verified(client, rows):
    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.list_sessions.return_value = [{"id": _SESSION, "name": _SESSION, "status": "active"}]
    with (
        patch.object(session_service, "get_backend", return_value=backend),
        patch.object(session_service, "get_session_incarnation", return_value=None),
        patch.object(terminal_service.status_monitor, "get_status", side_effect=_idle),
    ):
        response = client.get(f"/sessions/{_SESSION}")
    assert response.status_code == 200
    assert {t["id"]: t["identity_verified"] for t in response.json()["terminals"]} == _EXPECTED


@pytest.mark.parametrize(
    "read",
    [
        lambda: db.list_terminals_by_session(_SESSION),
        lambda: db.list_terminals_in_sessions([_SESSION]),
        db.list_all_terminals,
    ],
    ids=["by_session", "in_sessions", "all"],
)
def test_terminal_lists_carry_identity_verified(rows, read):
    assert {t["id"]: t["identity_verified"] for t in read()} == _EXPECTED


def test_identity_verified_adds_no_per_row_query(engine):
    """The list reads the value off rows it already fetched: one terminal or four, same queries."""
    statements = []

    def _count(*_args):
        statements.append(1)

    db.create_terminal(_PENDING, _SESSION, "w1", "kiro_cli", "dev", identity_verified=False)
    event.listen(engine, "before_cursor_execute", _count)
    try:
        db.list_terminals_by_session(_SESSION)
        one_row = len(statements)
        for terminal_id in (_VERIFIED, _OTHER, _LEGACY):
            db.create_terminal(terminal_id, _SESSION, terminal_id, "kiro_cli", "dev")
        statements.clear()
        rows = db.list_terminals_by_session(_SESSION)
        assert len(rows) == 4
        assert len(statements) == one_row
    finally:
        event.remove(engine, "before_cursor_execute", _count)


@pytest.fixture
def launch(monkeypatch, tmp_path):
    """The real create path, with the provider, backend and row write stubbed."""
    from test.fixtures.terminal_launch import install_launch_stubs

    install_launch_stubs(monkeypatch, tmp_path)
    # No kiro-cli in the test environment: report a v2-capable one.
    monkeypatch.setattr(
        terminal_service,
        "probe_kiro_capabilities",
        MagicMock(
            return_value=KiroCapabilities(
                version="2.32.0",
                flags=frozenset({"--agent-engine", "--agent", "--trust-all-tools"}),
                agent_engines=frozenset({"v2"}),
            )
        ),
    )
    # The deferred init task is not under test here: only what the create returns.
    monkeypatch.setattr(terminal_service, "_schedule_deferred_init", MagicMock(return_value=None))


@pytest.mark.parametrize(
    "provider, body, expected",
    [
        ("kiro_cli", None, True),
        ("kiro_cli", {"initial_message": "start"}, False),
        ("claude_code", None, None),
        ("claude_code", {"initial_message": "start"}, None),
    ],
    ids=["kiro-sync", "kiro-deferred", "other-sync", "other-deferred"],
)
def test_create_session_reports_identity_verified(client, launch, provider, body, expected):
    """A synchronous Kiro create returns only after its passed check was recorded;
    a deferred one is still being checked; other providers have no check."""
    response = client.post(
        "/sessions",
        params={"agent_profile": "developer", "provider": provider},
        json=body,
    )
    assert response.status_code == 201, response.text
    assert response.json()["identity_verified"] is expected
