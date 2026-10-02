"""Registry truth, not PATCHable metadata, controls ephemeral delegation."""

from datetime import datetime, timedelta
from pathlib import Path
from test.utils.test_agent_profiles_ephemeral import DOCUMENT, NAME, stores  # noqa: F401
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.services import settings_service, terminal_service
from cli_agent_orchestrator.utils import orchestration

TOOLS = ["assign", "handoff", "assign_elastic", "workflow_run", "workflow_resume", "workflow_start"]


@pytest.fixture
def registry(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    database.create_terminal(
        "abcd1234",
        "session",
        "window",
        "claude_code",
        agent_profile="developer",
        allowed_tools=["@cao-mcp-server"],
        caller_id="1234abcd",
    )
    monkeypatch.setattr(
        terminal_service.status_monitor, "get_status", lambda _: TerminalStatus.IDLE
    )
    monkeypatch.setattr(terminal_service, "get_deferred_init_failure", lambda *_: None)
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setattr(
        server.mcp_utils, "get_json", lambda *_, **__: terminal_service.get_terminal("abcd1234")
    )
    monkeypatch.setattr(server.requests, "get", Mock(return_value=Mock(status_code=404)))
    monkeypatch.setattr(
        settings_service,
        "SETTINGS_FILE",
        Path("/nonexistent/e1a-test-settings") / "test-settings.json",
    )
    yield factory
    engine.dispose()


def seed(registry, state):
    now = datetime.now()
    with registry() as db:
        db.execute(
            text(
                "INSERT INTO ephemeral_agents (name, owner_kind, owner_id, session_name, state, "
                "launched_terminal_id, provider, effective_tools, created_at, expires_at, "
                "spec_sha256, profile_sha256, audit_path) VALUES "
                "(:name, 'terminal', '1234abcd', 'session', :state, 'abcd1234', 'claude_code', "
                "'[]', :now, :expiry, 'spec', 'profile', 'audit')"
            ),
            {"name": NAME, "state": state, "now": now, "expiry": now + timedelta(minutes=15)},
        )
        db.commit()


@pytest.mark.parametrize("state", ["launched", "gc"])
@pytest.mark.parametrize("tool", TOOLS)
def test_seeded_registry_denies_real_context(registry, state, tool):
    seed(registry, state)
    assert server._get_terminal_context_from_env()["ephemeral"] is True
    assert "ephemeral" in server._tool_denied_reason(tool)


@pytest.mark.parametrize("tool", TOOLS)
def test_child_may_delegate_lifts_denial(registry, monkeypatch, tool):
    seed(registry, "gc")
    monkeypatch.setattr(
        settings_service, "_load_or_raise", lambda: {"ephemeral": {"child_may_delegate": True}}
    )
    assert server._tool_denied_reason(tool) is None


@pytest.mark.parametrize("tool", TOOLS)
def test_registry_lookup_failure_denies(registry, tool):
    from cli_agent_orchestrator.api.main import app

    database.EphemeralAgentModel.__table__.drop(registry.kw["bind"])
    assert server._tool_denied_reason(tool) is not None
    response = TestClient(app, base_url="http://localhost").get("/terminals/abcd1234")
    assert response.status_code == 500
    assert "ephemeral" not in response.json()


@pytest.mark.parametrize("bound, forged", [(False, True), (True, False)])
def test_metadata_patch_cannot_change_identity_or_gate(registry, bound, forged):
    from cli_agent_orchestrator.api.main import app

    if bound:
        seed(registry, "gc")
    client = TestClient(app, base_url="http://localhost")
    response = client.patch(
        "/terminals/abcd1234/metadata", json={"metadata": {"ephemeral": forged}}
    )
    assert response.status_code == 200
    assert client.get("/terminals/abcd1234").json()["ephemeral"] is bound
    assert (server._tool_denied_reason("assign") is not None) is bound


def test_send_message_remains_allowed(registry, monkeypatch):
    seed(registry, "gc")
    post = Mock(return_value={"success": True})
    monkeypatch.setattr(server, "_send_message_impl", post)
    import asyncio

    assert asyncio.run(server.send_message("done", receiver_id="1234abcd"))["success"]
    post.assert_called_once_with("1234abcd", "done")


def test_empty_registry_is_inert_on_every_read(registry):
    assert Terminal(**terminal_service.get_terminal("abcd1234")).ephemeral is False
    assert database.get_terminal_metadata("abcd1234")["ephemeral"] is False
    for rows in (
        database.list_all_terminals(),
        database.list_terminals_by_session("session"),
        database.list_terminals_in_sessions(["session"]),
    ):
        assert rows[0]["ephemeral"] is False
    assert server._tool_denied_reason("assign") is None
    assert database.get_ephemeral_agent(NAME) is None


@pytest.mark.parametrize("tool", ["workflow_run", "workflow_resume", "workflow_start"])
@pytest.mark.asyncio
async def test_workflow_tools_enforce_gate(registry, monkeypatch, tool):
    seed(registry, "launched")
    post = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(server.requests, "post", post)
    result = await getattr(server, tool)("run")
    assert result["ok"] is False
    assert "ephemeral" in result["error"]
    post.assert_not_called()


@pytest.mark.parametrize("path", ["assign", "handoff", "handoff_nowait"])
@pytest.mark.asyncio
async def test_target_without_claim_refuses_before_tool_inheritance(stores, monkeypatch, path):
    (stores[1] / f"{NAME}.md").write_text(DOCUMENT)
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    get = Mock(
        return_value=Mock(
            status_code=200,
            json=lambda: {
                "provider": "claude_code",
                "session_name": "session",
                "allowed_tools": ["*"],
            },
        )
    )
    monkeypatch.setattr(orchestration.requests, "get", get)
    inheritance = Mock(side_effect=AssertionError("must not inherit"))
    monkeypatch.setattr(orchestration, "_resolve_child_allowed_tools", inheritance)
    post = Mock(side_effect=AssertionError("must not launch"))
    monkeypatch.setattr(orchestration.requests, "post", post)
    if path == "assign":
        result = orchestration._assign_impl(NAME, "task", working_directory="/workspace")
        assert result["success"] is False
        assert "claim" in result["message"]
    else:
        result = await orchestration._handoff_impl(
            NAME, "task", working_directory="/workspace", wait=path != "handoff_nowait"
        )
        assert result.success is False
        assert "claim" in result.message
    inheritance.assert_not_called()
    post.assert_not_called()


def test_registry_reader_returns_seeded_facts_and_never_writes(registry):
    seed(registry, "gc")
    facts = database.get_ephemeral_agent(NAME)
    assert facts["state"] == "gc"
    assert facts["effective_tools"] == []
    assert facts["launched_terminal_id"] == "abcd1234"
    assert database.is_ephemeral_terminal("abcd1234") is True
    assert database.is_ephemeral_terminal("1234abcd") is False
    for rows in (
        database.list_all_terminals(),
        database.list_terminals_by_session("session"),
        database.list_terminals_in_sessions(["session"]),
    ):
        assert rows[0]["ephemeral"] is True
    with registry() as db:
        assert db.execute(text("SELECT count(*) FROM ephemeral_agents")).scalar() == 1


@pytest.mark.parametrize("value", [None, "true", False, 1, {}, []])
def test_setting_requires_literal_operator_boolean(monkeypatch, value):
    monkeypatch.setattr(
        settings_service, "_load_or_raise", lambda: {"ephemeral": {"child_may_delegate": value}}
    )
    assert settings_service.child_may_delegate() is False


def test_policy_read_failure_is_not_permission(registry, monkeypatch):
    seed(registry, "gc")
    monkeypatch.setattr(
        settings_service,
        "_load_or_raise",
        Mock(
            side_effect=settings_service.SettingsUnreadableError(
                Path("/scratch/settings.json"), PermissionError("unreadable")
            )
        ),
    )
    assert server._tool_denied_reason("assign") is not None


@pytest.mark.parametrize("tool", ["assign", "handoff", "assign_elastic"])
@pytest.mark.asyncio
async def test_delegation_tools_enforce_ephemeral_gate(registry, monkeypatch, tool):
    seed(registry, "gc")
    impl = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(server, "_assign_impl", impl)
    monkeypatch.setattr(server, "_handoff_impl", impl)
    if tool == "assign_elastic":
        result = await server.assign_elastic(agent_profile="developer", message="task")
    else:
        result = await getattr(server, tool)(agent_profile="developer", message="task")
    if tool == "handoff":
        assert result.success is False
        assert "ephemeral" in result.message
    else:
        assert result["success"] is False
        assert "ephemeral" in result["message" if tool == "assign_elastic" else "error"]
    impl.assert_not_called()


@pytest.mark.parametrize(
    "reader", ["list_all_terminals", "list_terminals_by_session", "list_terminals_in_sessions"]
)
def test_registry_membership_is_batched_for_terminal_lists(registry, reader):
    from sqlalchemy import event

    for terminal_id in ("1234abcd", "abcd5678"):
        database.create_terminal(terminal_id, "session", "window", "claude_code")
    seed(registry, "gc")
    selects = []
    engine = registry.kw["bind"]

    def count_selects(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        args = (
            ()
            if reader == "list_all_terminals"
            else (("session",) if reader == "list_terminals_by_session" else (["session"],))
        )
        rows = getattr(database, reader)(*args)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)
    assert len(selects) <= 2
    assert {row["id"] for row in rows if row["ephemeral"]} == {"abcd1234"}


def test_registry_has_future_binding_columns(registry):
    from sqlalchemy import inspect

    columns = {
        column["name"]: column
        for column in inspect(registry.kw["bind"]).get_columns("ephemeral_agents")
    }
    assert {"bound_at", "idempotency_key"} <= columns.keys()
    assert columns["bound_at"]["nullable"] is True
    assert columns["idempotency_key"]["nullable"] is True


@pytest.mark.parametrize("handler", ["_create_terminal", "_resolve_handoff_provider"])
def test_installed_handler_guard_does_not_read_source(stores, monkeypatch, handler):
    from cli_agent_orchestrator.utils import agent_profiles

    source = Mock(side_effect=AssertionError("installed guard must not read a profile"))
    monkeypatch.setattr(agent_profiles, "resolve_agent_profile_source", source)
    monkeypatch.setattr(orchestration, "_current_terminal_id", lambda: None)
    monkeypatch.setattr(orchestration, "resolve_provider", lambda *a, **k: "claude_code")
    monkeypatch.setattr(
        orchestration.requests,
        "post",
        Mock(
            return_value=Mock(
                json=lambda: {
                    "id": "abcd1234",
                    "session_name": "session",
                    "provider": "claude_code",
                }
            )
        ),
    )
    getattr(orchestration, handler)("ordinary")
    source.assert_not_called()


@pytest.mark.parametrize("tool", ["workflow_run", "workflow_resume", "workflow_start"])
@pytest.mark.parametrize("caller", ["installed", "unbound"])
@pytest.mark.asyncio
async def test_workflows_allow_installed_and_unbound_callers(registry, monkeypatch, tool, caller):
    import json

    with registry() as db:
        row = db.get(database.TerminalModel, "abcd1234")
        row.allowed_tools = json.dumps(["fs_read"])
        db.commit()
    if caller == "unbound":
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
        monkeypatch.setattr(
            server.mcp_utils,
            "get_json",
            Mock(side_effect=AssertionError("operator has no terminal lookup")),
        )
    post = Mock(
        return_value=Mock(
            status_code=202 if tool == "workflow_start" else 200,
            json=lambda: {"run_id": "run", "state": "completed"},
        )
    )
    monkeypatch.setattr(server.requests, "post", post)
    result = await getattr(server, tool)("run")
    assert result["ok"] is True
    post.assert_called_once()


@pytest.mark.parametrize("tool", ["workflow_run", "workflow_resume", "workflow_start"])
@pytest.mark.parametrize("status_code", [404, 500])
@pytest.mark.asyncio
async def test_workflows_refuse_unresolved_callers(registry, monkeypatch, tool, status_code):
    import requests

    response = requests.Response()
    response.status_code = status_code
    monkeypatch.setattr(
        server.mcp_utils, "get_json", Mock(side_effect=requests.HTTPError(response=response))
    )
    post = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(server.requests, "post", post)
    result = await getattr(server, tool)("run")
    assert result["ok"] is False
    assert "cannot authorize" in result["error"]
    post.assert_not_called()


@pytest.mark.parametrize("tool", TOOLS)
def test_old_server_without_marker_keeps_installed_caller_compatible(registry, monkeypatch, tool):
    metadata = terminal_service.get_terminal("abcd1234")
    metadata.pop("ephemeral")
    monkeypatch.setattr(server.mcp_utils, "get_json", lambda *a, **k: metadata)
    assert server._get_terminal_context_from_env()["ephemeral"] is False
    assert server._tool_denied_reason(tool) is None
