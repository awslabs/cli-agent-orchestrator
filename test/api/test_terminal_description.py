"""Description persistence and HTTP contracts using an isolated SQLite database."""

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.services import session_service, terminal_service
from cli_agent_orchestrator.utils.description import normalize_description


@pytest.fixture
def description_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'description.db'}")
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine))
    created = db.create_terminal(
        "abcd1234", "cao-demo", "w", "kiro_cli", "developer", description="Initial responsibility"
    )
    assert created["description"] == "Initial responsibility"
    yield
    engine.dispose()


def test_migration_preserves_old_rows(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE terminals (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO terminals VALUES ('old')")
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    db._migrate_terminals_schema()
    db._migrate_terminals_schema()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT description FROM terminals").fetchall() == [(None,)]
        assert [row[1] for row in conn.execute("PRAGMA table_info(terminals)")].count(
            "description"
        ) == 1


@pytest.mark.parametrize("value", [None, "", "  ", "  Review\ncode  ", "😀" * 64])
def test_patch_persists_and_reads_every_projection(client, description_db, value):
    with patch.object(
        terminal_service.status_monitor, "get_status", return_value=TerminalStatus.COMPLETED
    ):
        response = client.patch("/terminals/abcd1234/description", json={"description": value})
    assert response.status_code == 200
    expected = normalize_description(value)
    assert response.json()["description"] == expected
    assert response.json()["id"] == "abcd1234"
    assert response.json()["status"] == "completed"
    assert db.get_terminal_metadata("abcd1234")["description"] == expected
    assert db.list_terminals_by_session("cao-demo")[0]["description"] == expected
    assert db.list_terminals_in_sessions(["cao-demo"])[0]["description"] == expected
    assert db.list_all_terminals()[0]["description"] == expected
    assert db.get_terminal_metadata("abcd1234")["metadata"] is None


@pytest.mark.parametrize("body", [{}, {"description": "x" * 65}, {"description": "😀" * 65}])
def test_patch_rejects_invalid_and_preserves_value(client, description_db, body):
    db.update_terminal_description("abcd1234", "Previous")
    response = client.patch("/terminals/abcd1234/description", json=body)
    assert response.status_code == 422
    assert db.get_terminal_metadata("abcd1234")["description"] == "Previous"


def test_missing_terminal_is_404(client, description_db):
    assert (
        client.patch("/terminals/ffffffff/description", json={"description": None}).status_code
        == 404
    )


def test_clear_and_delete(description_db):
    assert terminal_service.update_description("abcd1234", "Review")
    assert terminal_service.update_description("abcd1234", "   ")
    assert db.get_terminal_metadata("abcd1234")["description"] is None
    assert db.delete_terminal("abcd1234")
    assert not terminal_service.update_description("abcd1234", "Again")
    assert db.get_terminal_metadata("abcd1234") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,extra,expected",
    [
        (None, {}, None),
        ("demo", {}, "demo"),
        ("cao-demo", {}, "cao-demo"),
        ("demo", {"description": None}, None),
        ("demo", {"description": ""}, ""),
        ("demo", {"description": "Worker"}, "Worker"),
    ],
)
async def test_session_name_copy_only_when_omitted(name, extra, expected):
    with (
        patch.object(
            session_service, "create_terminal", new=AsyncMock(return_value=MagicMock())
        ) as create,
        patch.object(session_service, "dispatch_plugin_event"),
    ):
        await session_service.create_session("kiro_cli", "developer", session_name=name, **extra)
    assert create.call_args.kwargs["description"] == expected


@pytest.mark.parametrize(
    "body,expected",
    [
        ({}, False),
        ({"description": None}, True),
        ({"description": ""}, True),
        ({"description": "Review"}, True),
    ],
)
def test_http_preserves_description_presence(client, body, expected):
    terminal = Terminal(id="abcd1234", name="w", provider="kiro_cli", session_name="cao-demo")
    with patch.object(
        session_service, "create_session", new=AsyncMock(return_value=terminal)
    ) as create:
        response = client.post(
            "/sessions?provider=kiro_cli&agent_profile=developer&session_name=demo", json=body
        )
    assert response.status_code == 201
    assert ("description" in create.call_args.kwargs) is expected
    if expected:
        assert create.call_args.kwargs["description"] == body["description"]
    assert response.json()["description"] is None


@pytest.mark.asyncio
async def test_idempotent_retry_keeps_operator_edit(description_db):
    initial = normalize_description("  Initial\nresponsibility ", truncate=True)
    args = (
        "kiro_cli",
        "developer",
        "cao-demo",
        None,
        None,
        None,
        False,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    fingerprint = terminal_service._request_fingerprint(*args, description=initial)
    db.create_terminal(
        "feed1234",
        "cao-demo",
        "w2",
        "kiro_cli",
        "developer",
        description=initial,
        idempotency_key="retry",
        request_fingerprint=fingerprint,
    )
    terminal_service.update_description("feed1234", "Operator edit")
    with patch.object(
        terminal_service,
        "get_terminal",
        return_value={
            "id": "feed1234",
            "name": "w2",
            "provider": "kiro_cli",
            "session_name": "cao-demo",
            "description": db.get_terminal_metadata("feed1234")["description"],
        },
    ):
        retry = await terminal_service.create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-demo",
            idempotency_key="retry",
            description="  Initial\nresponsibility ",
        )
        assert retry.description == "Operator edit"
        with pytest.raises(terminal_service.IdempotencyKeyConflict):
            await terminal_service.create_terminal(
                "kiro_cli",
                "developer",
                session_name="cao-demo",
                idempotency_key="retry",
                description="Other",
            )


@pytest.mark.parametrize(
    "text,expected",
    [
        (None, None),
        ("  ", None),
        (" a\r\nb ", "a b"),
        ("😀" * 65, "😀" * 64),
        ("a\x00\x07\b\x1b\t\x7f\x9fb", "ab"),
        ("a\r\nb\x85c\u2028d\u2029e", "a b c d e"),
        ("\x00\x1b\t\x7f\x9f", None),
        ("😀" * 64 + "\x00", "😀" * 64),
    ],
)
def test_creation_normalization(text, expected):
    assert normalize_description(text, truncate=True) == expected


@pytest.mark.parametrize("length", [64, 65])
def test_patch_filters_controls_before_length_validation(client, description_db, length):
    value = "😀" * length + "\x00\t\x7f"
    response = client.patch("/terminals/abcd1234/description", json={"description": value})
    assert response.status_code == (200 if length == 64 else 422)
    if length == 64:
        assert response.json()["description"] == "😀" * 64
