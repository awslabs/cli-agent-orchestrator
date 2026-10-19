"""The HTTP create boundary, opt-in MCP surface and remote placement refusal."""

import json
import logging
from test.services.test_ephemeral_service import CALLER, SPEC, create_store  # noqa: F401
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.utils import orchestration


def client():
    from cli_agent_orchestrator.api.main import app

    return TestClient(app, base_url="http://localhost")


def test_post_success(create_store):
    response = client().post("/ephemeral-agents", params={"caller_id": CALLER}, json=SPEC)
    assert response.status_code == 201
    assert response.json()["effective_tools"] == ["fs_read", "@cao-mcp-server"]


@pytest.mark.parametrize("value", [False, "true", None])
def test_disabled_is_dependency_before_body(create_store, value):
    config = create_store[4]
    if value is None:
        config["ephemeral"].pop("enabled")
    else:
        config["ephemeral"]["enabled"] = value
    response = client().post("/ephemeral-agents", content="not JSON")
    assert response.status_code == 404
    assert response.json()["detail"]["rule"] == "ephemeral_disabled"


@pytest.mark.parametrize(
    "body",
    [
        [],
        "private-description",
        7,
        {**SPEC, "private unknown key": "private-description"},
        {**SPEC, "brief": ["private-description"]},
    ],
)
def test_invalid_shape_never_echoes_values(create_store, body, caplog):
    response = client().post("/ephemeral-agents", params={"caller_id": CALLER}, json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert set(detail) == {"kind", "rule", "message"}
    assert detail["kind"] == "ephemeral_policy" and detail["rule"] == "invalid_spec"
    assert "private-description" not in response.text + caplog.text
    assert "private unknown key" not in response.text + caplog.text


@pytest.mark.parametrize("caller", [None, "malformed-id", "deadbeef"])
def test_post_creator_is_not_fastapi_validation(create_store, caller):
    response = client().post(
        "/ephemeral-agents", params={} if caller is None else {"caller_id": caller}, json=SPEC
    )
    assert response.status_code == 400
    assert response.json()["detail"]["rule"] == "creator_unresolved"


def test_shape_precedes_block_and_block_precedes_creator(create_store):
    create_store[4]["ephemeral"]["max_depth"] = 2
    response = client().post("/ephemeral-agents", json={**SPEC, "tools": ["*"]})
    assert response.status_code == 422
    response = client().post("/ephemeral-agents", json=SPEC)
    assert (
        response.status_code == 400
        and response.json()["detail"]["rule"] == "policy_config_error:max_depth"
    )


@pytest.mark.parametrize("state", ["launched", "gc"])
@pytest.mark.parametrize("may_delegate", [False, True])
def test_ephemeral_creator_always_refused(create_store, monkeypatch, state, may_delegate):
    service, factory, _, _, config = create_store
    config["ephemeral"]["child_may_delegate"] = may_delegate
    result = service.create_ephemeral_agent(SPEC, CALLER)
    with factory() as db:
        row = db.get(database.EphemeralAgentModel, result["name"])
        row.launched_terminal_id = CALLER
        row.state = state
        db.commit()
    response = client().post("/ephemeral-agents", params={"caller_id": CALLER}, json=SPEC)
    assert response.status_code == 400 and response.json()["detail"]["rule"] == "max_depth_exceeded"
    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    monkeypatch.setattr(
        server.mcp_utils,
        "get_json",
        lambda *a, **k: {
            "id": CALLER,
            "session_name": "cao-session",
            "provider": "claude_code",
            "ephemeral": True,
            "allowed_tools": ["@cao-mcp-server"],
        },
    )
    monkeypatch.setattr(server.requests, "get", Mock(return_value=Mock(status_code=404)))
    assert "max_depth_exceeded" in server._tool_denied_reason("create_ephemeral_agent")
    if may_delegate:
        for tool in (
            "handoff",
            "assign",
            "workflow_resume",
            "workflow_start",
            "assign_elastic",
            "workflow_run",
        ):
            assert server._tool_denied_reason(tool) is None


@pytest.mark.parametrize("value", [False, "true", None, True])
@pytest.mark.asyncio
async def test_mcp_registration_is_strict_and_factored(create_store, value):
    from fastmcp import FastMCP

    target = FastMCP("registration test")
    server._register_ephemeral_tool(target, value)
    tools = await target.list_tools()
    assert ("create_ephemeral_agent" in {tool.name for tool in tools}) is (value is True)


@pytest.mark.asyncio
async def test_mcp_forwarding_and_server_toggle(create_store, monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    monkeypatch.setattr(server, "_tool_denied_reason", lambda _: None)
    reply = {"name": "Ramones-log_triage-3f9a", "notes": ["launch unavailable"]}
    post = Mock(return_value=Mock(status_code=201, json=lambda: reply))
    monkeypatch.setattr(server.requests, "post", post)
    result = await server.create_ephemeral_agent("log_triage", "Inspect logs.", tools=["fs_read"])
    assert result == reply
    assert post.call_args.kwargs["params"] == {"caller_id": CALLER}
    assert set(post.call_args.kwargs["json"]) == {
        "spec_version",
        "purpose",
        "brief",
        "description",
        "provider",
        "tools",
        "model_tier",
        "effort",
    }
    post.return_value = Mock(
        status_code=404,
        json=lambda: {
            "detail": {
                "kind": "ephemeral_policy",
                "rule": "ephemeral_disabled",
                "message": "ephemeral policy: ephemeral_disabled",
            }
        },
    )
    result = await server.create_ephemeral_agent("log_triage", "Inspect logs.", tools=["fs_read"])
    assert result == {
        "success": False,
        "rule": "ephemeral_disabled",
        "message": "ephemeral policy: ephemeral_disabled",
    }


@pytest.mark.asyncio
async def test_mcp_requires_bound_caller(create_store, monkeypatch):
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    post = Mock(side_effect=AssertionError("must not forward"))
    monkeypatch.setattr(server.requests, "post", post)
    result = await server.create_ephemeral_agent("log_triage", "Inspect logs.")
    assert result["rule"] == "creator_unresolved"
    post.assert_not_called()


@pytest.mark.parametrize("path", ["assign", "handoff", "elastic"])
@pytest.mark.asyncio
async def test_remote_reserved_name_refused_before_placement(create_store, monkeypatch, path):
    name = "Ramones-log_triage-3f9a"
    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    monkeypatch.setattr(server, "_tool_denied_reason", lambda _: None)
    remote = Mock(side_effect=AssertionError("must not place remotely"))
    monkeypatch.setattr(orchestration, "_assign_remote", remote)
    monkeypatch.setattr(orchestration, "_resolve_remote_provider", remote)
    monkeypatch.setattr(server.requests, "post", remote)
    if path == "assign":
        result = orchestration._assign_impl(name, "brief", target_host="remote")
        assert result["success"] is False and "remote_placement_not_allowed" in result["message"]
    elif path == "handoff":
        result = await orchestration._handoff_impl(name, "brief", target_host="remote")
        assert not result.success and "remote_placement_not_allowed" in result.message
    else:
        result = await server.assign_elastic(name, "brief")
        assert result["success"] is False and "remote_placement_not_allowed" in result["message"]
    remote.assert_not_called()


def test_unreadable_settings_refused_before_json(create_store, monkeypatch):
    def unreadable():
        raise settings_service.SettingsUnreadableError(
            settings_service.SETTINGS_FILE, OSError("unreadable")
        )

    monkeypatch.setattr(settings_service, "_load_or_raise", unreadable)
    response = client().post("/ephemeral-agents", content="not JSON")
    assert response.status_code == 400
    assert response.json()["detail"]["rule"] == "policy_config_error:settings_unreadable"
    assert server._ephemeral_enabled_at_startup() is False


def test_non_json_invalid_spec(create_store):
    response = client().post("/ephemeral-agents", content="not JSON")
    assert response.status_code == 422
    assert response.json()["detail"]["rule"] == "invalid_spec"


@pytest.mark.parametrize("grants,status", [(["fs_read"], 400), (["*"], 201)])
def test_raw_post_requires_cao_server_or_wildcard(create_store, grants, status):
    with create_store[1]() as db:
        db.get(database.TerminalModel, CALLER).allowed_tools = json.dumps(grants)
        db.commit()
    response = client().post("/ephemeral-agents", params={"caller_id": CALLER}, json=SPEC)
    assert response.status_code == status
    if status == 400:
        assert response.json()["detail"]["rule"] == "tool_exceeds_creator"


@pytest.mark.asyncio
async def test_registered_tool_refused_by_real_server_after_toggle(create_store, monkeypatch):
    from fastmcp import FastMCP

    target = FastMCP("toggle test")
    server._register_ephemeral_tool(target, server._ephemeral_enabled_at_startup())
    assert "create_ephemeral_agent" in {tool.name for tool in await target.list_tools()}
    create_store[4]["ephemeral"]["enabled"] = False
    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    monkeypatch.setattr(server, "_tool_denied_reason", lambda _: None)
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda url, **kw: client().post("/ephemeral-agents", params=kw["params"], json=kw["json"]),
    )
    result = await server.create_ephemeral_agent("log_triage", "Inspect logs.")
    assert result["success"] is False and result["rule"] == "ephemeral_disabled"
    assert not (create_store[2] / "ephemeral").exists()


def test_invalid_caller_never_injects_refusal_log(create_store, caplog):
    value = "private-description\nforged warning"
    response = client().post("/ephemeral-agents", params={"caller_id": value}, json=SPEC)
    assert response.status_code == 400
    assert value not in caplog.text + response.text
    assert "forged warning" not in caplog.text


@pytest.mark.parametrize(
    "denied,rule",
    [
        ("cannot authorize missing caller", "creator_unresolved"),
        ("@cao-mcp-server is denied", "tool_exceeds_creator"),
        ("max_depth_exceeded", "max_depth_exceeded"),
    ],
)
@pytest.mark.asyncio
async def test_mcp_gate_denials_redacted_and_never_forward(
    create_store, monkeypatch, denied, rule, caplog
):
    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    monkeypatch.setattr(server, "_tool_denied_reason", lambda _: denied)
    post = Mock(side_effect=AssertionError("no POST"))
    monkeypatch.setattr(server.requests, "post", post)
    result = await server.create_ephemeral_agent(
        "log_triage", "private-brief", description="private-description"
    )
    assert result["success"] is False and result["rule"] == rule
    assert "private-brief" not in str(result) + caplog.text
    assert "private-description" not in str(result) + caplog.text
    assert len([r for r in caplog.records if "ephemeral refusal" in r.message]) == 1
    post.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_connection_failure_is_redacted(create_store, monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    monkeypatch.setattr(server, "_tool_denied_reason", lambda _: None)
    monkeypatch.setattr(
        server.requests, "post", Mock(side_effect=server.requests.ConnectionError("private-brief"))
    )
    result = await server.create_ephemeral_agent("log_triage", "private-brief")
    assert result["rule"] == "creator_unresolved"
    assert "private-brief" not in str(result)


@pytest.mark.parametrize("body", [{"detail": "insufficient scope"}, [], {"detail": None}])
@pytest.mark.asyncio
async def test_mcp_unstructured_server_error_fails_redacted(
    create_store, monkeypatch, body, caplog
):
    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    monkeypatch.setattr(server, "_tool_denied_reason", lambda _: None)
    monkeypatch.setattr(
        server.requests, "post", Mock(return_value=Mock(status_code=403, json=lambda: body))
    )
    result = await server.create_ephemeral_agent(
        "log_triage", "private-brief", description="private-description"
    )
    assert result == {
        "success": False,
        "rule": "unexpected_failure",
        "message": "ephemeral policy: unexpected_failure",
    }
    assert "private-brief" not in caplog.text and "private-description" not in caplog.text


# Canonical advertised schema of the original FastMCP function registration,
# pinned so the raw-argument tool keeps it byte-identical.
PINNED_CREATE_TOOL_SCHEMA = '{"additionalProperties":false,"properties":{"brief":{"type":"string"},"description":{"anyOf":[{"type":"string"},{"type":"null"}],"default":null},"effort":{"anyOf":[{"enum":["low","medium","high","auto"],"type":"string"},{"type":"null"}],"default":null},"model_tier":{"anyOf":[{"enum":["small","medium","large","auto"],"type":"string"},{"type":"null"}],"default":null},"provider":{"anyOf":[{"enum":["claude_code","codex"],"type":"string"},{"type":"null"}],"default":null},"purpose":{"type":"string"},"tools":{"anyOf":[{"items":{"enum":["fs_read","fs_list","fs_write","execute_bash","web_fetch"],"type":"string"},"type":"array"},{"type":"null"}],"default":null}},"required":["purpose","brief"],"type":"object"}'


@pytest.mark.asyncio
async def test_real_fastmcp_schema_matches_review_base(create_store):
    from fastmcp import FastMCP

    target = FastMCP("schema compatibility")
    server._register_ephemeral_tool(target, True)
    tool = (await target.list_tools())[0]
    assert (
        json.dumps(tool.parameters, sort_keys=True, separators=(",", ":"))
        == PINNED_CREATE_TOOL_SCHEMA
    )


@pytest.mark.parametrize("field", ["brief", "description", "tools"])
@pytest.mark.asyncio
async def test_real_fastmcp_invalid_values_are_server_redacted(create_store, monkeypatch, field):
    from fastmcp import FastMCP

    private = "AKIAIOSFODNN7EXAMPLE"
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    capture = Capture()
    roots = [logging.getLogger("fastmcp"), logging.getLogger()]
    for logger in roots:
        logger.addHandler(capture)
    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    monkeypatch.setattr(server, "_tool_denied_reason", lambda _: None)
    post = Mock(
        side_effect=lambda url, **kw: client().post(
            "/ephemeral-agents", params=kw["params"], json=kw["json"]
        )
    )
    monkeypatch.setattr(server.requests, "post", post)
    target = FastMCP("real validation boundary")
    server._register_ephemeral_tool(target, True)
    args = {"purpose": "log_triage", "brief": "Inspect logs.", field: [private]}
    try:
        try:
            result = await target.call_tool("create_ephemeral_agent", args)
            returned = str(result)
        except Exception as exc:
            result = None
            returned = str(exc)
    finally:
        for logger in roots:
            logger.removeHandler(capture)
    assert private not in returned
    assert all(private not in record for record in records)
    assert result is not None
    assert result.structured_content["success"] is False
    assert result.structured_content["rule"] == "invalid_spec"
    post.assert_called_once()


@pytest.mark.parametrize("status", [500, 502])
@pytest.mark.asyncio
async def test_non_json_server_reply_is_unexpected_and_redacted(
    create_store, monkeypatch, caplog, status
):
    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    monkeypatch.setattr(server, "_tool_denied_reason", lambda _: None)
    response = server.requests.models.Response()
    response.status_code = status
    response._content = b"<html>private response body</html>"
    with pytest.raises(server.requests.exceptions.JSONDecodeError):
        response.json()
    monkeypatch.setattr(server.requests, "post", Mock(return_value=response))
    result = await server.create_ephemeral_agent("log_triage", "private-brief")
    assert result == {
        "success": False,
        "rule": "unexpected_failure",
        "message": "ephemeral policy: unexpected_failure",
    }
    assert "private response body" not in str(result) + caplog.text
    assert "private-brief" not in str(result) + caplog.text


@pytest.mark.parametrize("path", ["assign", "handoff"])
@pytest.mark.asyncio
async def test_remote_refusal_uses_shared_redacted_logger(create_store, monkeypatch, caplog, path):
    caller = "private-caller\nforged-warning"
    monkeypatch.setenv("CAO_TERMINAL_ID", caller)
    if path == "assign":
        result = orchestration._assign_impl(
            "Ramones-log_triage-3f9a", "Inspect logs.", target_host="remote"
        )
        assert result["success"] is False
    else:
        result = await orchestration._handoff_impl(
            "Ramones-log_triage-3f9a", "Inspect logs.", target_host="remote"
        )
        assert result.success is False
    assert "private-caller" not in caplog.text and "forged-warning" not in caplog.text
    records = [r for r in caplog.records if "remote_placement_not_allowed" in r.getMessage()]
    assert len(records) == 1
    assert "caller=-" in records[0].getMessage()


@pytest.mark.parametrize(
    "case", ["model", "secret_key", "allowed_tools", "prompt", "missing_brief", "empty"]
)
@pytest.mark.asyncio
async def test_real_fastmcp_arity_is_server_redacted(create_store, monkeypatch, case):
    from fastmcp import FastMCP

    private = "AKIAIOSFODNN7EXAMPLE"
    cases = {
        "model": ({"purpose": "log_triage", "brief": private, "model": private}, ["model"]),
        "secret_key": ({"purpose": "log_triage", "brief": private, private: private}, [private]),
        "allowed_tools": (
            {"purpose": "log_triage", "brief": "Inspect logs.", "allowed_tools": [private]},
            ["allowed_tools"],
        ),
        "prompt": (
            {"purpose": "log_triage", "prompt": private, "description": private},
            ["prompt"],
        ),
        "missing_brief": ({"purpose": private}, []),
        "empty": ({}, []),
    }
    args, unknown_keys = cases[case]
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    capture = Capture()
    roots = [logging.getLogger("fastmcp"), logging.getLogger()]
    for logger in roots:
        logger.addHandler(capture)
    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    monkeypatch.setattr(server, "_tool_denied_reason", lambda _: None)
    post = Mock(
        side_effect=lambda url, **kw: client().post(
            "/ephemeral-agents", params=kw["params"], json=kw["json"]
        )
    )
    monkeypatch.setattr(server.requests, "post", post)
    target = FastMCP("raw arity boundary")
    server._register_ephemeral_tool(target, True)
    try:
        try:
            result = await target.call_tool("create_ephemeral_agent", args)
            returned = str(result)
        except Exception as exc:
            result = None
            returned = str(exc)
    finally:
        for logger in roots:
            logger.removeHandler(capture)
    assert private not in returned
    assert all(private not in record for record in records)
    for key in unknown_keys:
        assert key not in returned
        assert all(key not in record for record in records)
    assert result is not None
    assert result.structured_content["success"] is False
    assert result.structured_content["rule"] == "invalid_spec"
    if unknown_keys:
        assert "unknown field" in result.structured_content["message"]
    post.assert_called_once()
    assert post.call_args.kwargs["json"] == {"spec_version": 1, **args}
    assert post.call_args.kwargs["params"] == {"caller_id": CALLER}


@pytest.mark.asyncio
async def test_other_tool_keeps_argument_binding(create_store):
    from fastmcp import FastMCP

    target = FastMCP("ordinary binding")
    server._register_ephemeral_tool(target, True)
    invoked = Mock()

    @target.tool()
    def ordinary(value: str) -> str:
        invoked()
        return value

    with pytest.raises(Exception):
        await target.call_tool("ordinary", {"value": "valid", "unexpected": "extra"})
    invoked.assert_not_called()


@pytest.mark.parametrize(
    "denial,rule",
    [
        (None, "creator_unresolved"),
        ("@cao-mcp-server is denied", "tool_exceeds_creator"),
        ("max_depth_exceeded", "max_depth_exceeded"),
    ],
)
@pytest.mark.asyncio
async def test_real_raw_tool_gate_precedes_body_validation(create_store, monkeypatch, denial, rule):
    from fastmcp import FastMCP

    if denial is None:
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    else:
        monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
        monkeypatch.setattr(server, "_tool_denied_reason", lambda _: denial)
    post = Mock(side_effect=AssertionError("gate must not forward"))
    monkeypatch.setattr(server.requests, "post", post)
    target = FastMCP("gate before shape")
    server._register_ephemeral_tool(target, True)
    result = await target.call_tool("create_ephemeral_agent", {"prompt": "private text"})
    assert result.structured_content["rule"] == rule
    post.assert_not_called()


@pytest.mark.parametrize("enabled", [False, "true", None])
@pytest.mark.asyncio
async def test_unregistered_raw_tool_is_inert(create_store, enabled, monkeypatch):
    from fastmcp import FastMCP

    target = FastMCP("disabled raw tool")
    server._register_ephemeral_tool(target, enabled)
    post = Mock(side_effect=AssertionError("unregistered must not forward"))
    monkeypatch.setattr(server.requests, "post", post)
    with pytest.raises(Exception):
        await target.call_tool("create_ephemeral_agent", {"prompt": "private text"})
    post.assert_not_called()
