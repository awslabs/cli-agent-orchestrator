"""Unit tests for the MCP App tools and their registration.

Covers ``render_dashboard`` building a snapshot from HTTP data, correct
``visibility`` annotations per tool, default-off registration returning ``False``,
and the Phase II ``submit_command`` skeleton (classification + scope pre-check).
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.mcp_server import app_tools


class _FakeMCP:
    """Records tool registrations so tests can assert names + ``_meta``."""

    def __init__(self) -> None:
        self.registered: Dict[str, Dict[str, Any]] = {}

    def tool(self, name: str, meta: Dict[str, Any] | None = None):
        def _decorator(fn):
            self.registered[name] = {"fn": fn, "meta": meta}
            return fn

        return _decorator

    # No ``resource`` attr is required; register_apps degrades gracefully without it.


def _fake_get_factory(sessions: List[Dict], terminals_by_session: Dict[str, List[Dict]]):
    """Build a fake ``requests.get`` routing /sessions and per-session terminals."""

    def _fake_get(url: str, params=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if url.endswith("/sessions"):
            resp.json.return_value = sessions
        elif "/terminals" in url and "/sessions/" in url:
            name = url.split("/sessions/")[1].split("/terminals")[0]
            resp.json.return_value = terminals_by_session.get(name, [])
        else:
            resp.json.return_value = {}
        return resp

    return _fake_get


def test_render_dashboard_builds_snapshot_from_http() -> None:
    """render_dashboard aggregates /sessions + per-session terminals into a snapshot."""

    sessions = [{"id": "cao-foo", "name": "cao-foo", "status": "active"}]
    terminals = {"cao-foo": [{"id": "abcd1234", "tmux_session": "cao-foo", "provider": "kiro_cli"}]}

    with patch.object(app_tools.requests, "get", _fake_get_factory(sessions, terminals)):
        snap = app_tools._render_dashboard_impl()

    assert snap["counts"] == {"sessions": 1, "terminals": 1}
    assert snap["terminals"][0]["session_name"] == "cao-foo"
    # default-off => full scope set surfaced to the UI
    assert snap["scopes"] == ["cao:read", "cao:write", "cao:admin"]


def test_cao_fetch_history_returns_normalized_events() -> None:
    """History replay returns events with a kind in the closed vocabulary."""

    from cli_agent_orchestrator.services.event_log_service import EventLog

    log = EventLog()
    log.append("launch", "abcd1234", "cao-foo", {"event_type": "post_create_terminal"})
    with patch.object(app_tools, "get_event_log", return_value=log):
        result = app_tools._cao_fetch_history_impl(limit=10)

    assert len(result["events"]) == 1
    assert result["events"][0]["kind"] == "launch"


def test_subscribe_events_descriptor() -> None:
    """subscribe_events returns the SSE descriptor the iframe connects to."""

    desc = app_tools._subscribe_events_impl()
    assert desc["sse_url"] == "/events"
    assert desc["history_tool"] == "cao_fetch_history"
    assert desc["ring_capacity"] == 500


def test_submit_command_rejects_unknown_kind() -> None:
    """An unknown kind is rejected with success=False and a descriptive error."""

    result = app_tools._submit_command_impl("frobnicate", {})
    assert result["success"] is False
    assert "unknown command kind" in result["error"]


def test_submit_command_classifies_and_passes_default_off() -> None:
    """Known kinds pass the default-off scope pre-check (full scope set).

    With auth off the full scope set is granted, so classification + the scope
    pre-check pass and the command reaches routing. We assert on the classifier
    directly and confirm routing is reached (a missing-field validation error,
    not a scope rejection, for kinds whose payload is empty here).
    """

    assert app_tools._classify_kind("shutdown_session") == app_tools.SCOPE_ADMIN
    assert app_tools._classify_kind("assign") == app_tools.SCOPE_WRITE
    assert app_tools._classify_kind("interrupt") == app_tools.SCOPE_WRITE
    assert app_tools._classify_kind("create_session") == app_tools.SCOPE_WRITE
    assert app_tools._classify_kind("send_message") == app_tools.SCOPE_WRITE
    assert app_tools._classify_kind("pause") == app_tools.SCOPE_WRITE
    assert app_tools._classify_kind("resume") == app_tools.SCOPE_WRITE
    assert app_tools._classify_kind("bogus") is None

    # Default-off: every known kind passes the scope pre-check and reaches
    # routing. With empty payloads, routing returns a field-validation error
    # (proving the pre-check did NOT block on scope).
    for kind in ("shutdown_session", "assign", "interrupt"):
        result = app_tools._submit_command_impl(kind, {})
        assert result["success"] is False
        assert "missing required field" in result["error"]


# ---------------------------------------------------------------------------
# Task 7.2 — submit_command choke-point matrix (classification, unknown-kind
# rejection, scope pass/deny per kind, default-off passes all, and real routing).


def _capture_post(recorder: List[Dict[str, Any]]):
    """Build a fake ``requests.post`` recording (url, params) and returning {}."""

    def _fake_post(url: str, params=None, headers=None, timeout=None):
        recorder.append({"url": url, "params": params})
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"success": True, "id": "newterm1", "session_name": "cao-x"}
        return resp

    return _fake_post


def _capture_delete(recorder: List[Dict[str, Any]]):
    def _fake_delete(url: str, headers=None, timeout=None):
        recorder.append({"url": url})
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"success": True}
        return resp

    return _fake_delete


def test_submit_command_routes_create_session() -> None:
    """create_session routes to POST /sessions with the agent_profile param."""

    calls: List[Dict[str, Any]] = []
    with patch.object(app_tools.requests, "post", _capture_post(calls)):
        result = app_tools._submit_command_impl(
            "create_session", {"agent_profile": "dev", "provider": "kiro_cli"}
        )
    assert result["success"] is True
    assert calls[0]["url"].endswith("/sessions")
    assert calls[0]["params"]["agent_profile"] == "dev"
    assert calls[0]["params"]["provider"] == "kiro_cli"
    assert result["terminal_id"] == "newterm1"


def test_submit_command_routes_send_message_to_inbox() -> None:
    """send_message routes to the inbox endpoint (matches existing semantics)."""

    calls: List[Dict[str, Any]] = []
    with patch.object(app_tools.requests, "post", _capture_post(calls)):
        result = app_tools._submit_command_impl(
            "send_message", {"terminal_id": "abcd1234", "message": "hi"}
        )
    assert result["success"] is True
    assert calls[0]["url"].endswith("/terminals/abcd1234/inbox/messages")
    assert calls[0]["params"]["message"] == "hi"
    assert calls[0]["params"]["sender_id"] == "operator"


def test_submit_command_routes_assign_to_input() -> None:
    """assign routes to /input with orchestration_type=assign (closest existing route)."""

    calls: List[Dict[str, Any]] = []
    with patch.object(app_tools.requests, "post", _capture_post(calls)):
        result = app_tools._submit_command_impl(
            "assign", {"terminal_id": "abcd1234", "message": "do work"}
        )
    assert result["success"] is True
    assert calls[0]["url"].endswith("/terminals/abcd1234/input")
    assert calls[0]["params"]["orchestration_type"] == "assign"


def test_submit_command_routes_interrupt_to_key() -> None:
    """interrupt routes to POST /key with C-c (SIGINT)."""

    calls: List[Dict[str, Any]] = []
    with patch.object(app_tools.requests, "post", _capture_post(calls)):
        result = app_tools._submit_command_impl("interrupt", {"terminal_id": "abcd1234"})
    assert result["success"] is True
    assert calls[0]["url"].endswith("/terminals/abcd1234/key")
    assert calls[0]["params"]["key"] == "C-c"


def test_submit_command_routes_shutdown_session_to_delete() -> None:
    """shutdown_session routes to DELETE /sessions/{name}."""

    calls: List[Dict[str, Any]] = []
    with patch.object(app_tools.requests, "delete", _capture_delete(calls)):
        result = app_tools._submit_command_impl("shutdown_session", {"session_name": "cao-x"})
    assert result["success"] is True
    assert calls[0]["url"].endswith("/sessions/cao-x")


def test_submit_command_pause_resume_unsupported() -> None:
    """pause/resume have no Backplane route -> structured unsupported result."""

    for kind in ("pause", "resume"):
        result = app_tools._submit_command_impl(kind, {"terminal_id": "abcd1234"})
        assert result["success"] is False
        assert result["error"] == "unsupported"


def test_submit_command_scope_deny_per_kind() -> None:
    """A granted set missing the required scope blocks the matching kind."""

    # read-only token: write kinds and admin kinds both blocked.
    with patch.object(app_tools, "get_scopes_for_local_token", return_value=["cao:read"]):
        assert app_tools._submit_command_impl("assign", {})["error"] == "scope cao:write required"
        assert (
            app_tools._submit_command_impl("shutdown_session", {})["error"]
            == "scope cao:admin required"
        )
    # write token: write kinds allowed (reach routing/validation) but admin denied.
    with patch.object(app_tools, "get_scopes_for_local_token", return_value=["cao:write"]):
        assert (
            app_tools._submit_command_impl("shutdown_session", {})["error"]
            == "scope cao:admin required"
        )
        assign_res = app_tools._submit_command_impl("assign", {})
        assert "missing required field" in assign_res["error"]  # passed scope, hit validation


def test_submit_command_denies_when_scope_missing() -> None:
    """A non-empty granted set missing the required scope blocks the command."""

    with patch.object(app_tools, "get_scopes_for_local_token", return_value=["cao:read"]):
        result = app_tools._submit_command_impl("shutdown_session", {})
    assert result["success"] is False
    assert "cao:admin" in result["error"]


def test_register_app_tools_disabled_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """With CAO_MCP_APPS_ENABLED unset, registration is a no-op returning False."""

    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    fake = _FakeMCP()
    assert app_tools.register_app_tools(fake) is False
    assert fake.registered == {}


def test_register_app_tools_enabled_sets_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    """When enabled, the five tools register with correct visibility annotations."""

    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    fake = _FakeMCP()
    assert app_tools.register_app_tools(fake) is True

    assert set(fake.registered) == {
        "render_dashboard",
        "render_agent_view",
        "cao_fetch_history",
        "subscribe_events",
        "render_graph_view",
        "submit_command",
    }
    assert fake.registered["render_dashboard"]["meta"]["ui"]["visibility"] == ["model", "app"]
    assert fake.registered["render_agent_view"]["meta"]["ui"]["visibility"] == ["model", "app"]
    assert fake.registered["cao_fetch_history"]["meta"]["ui"]["visibility"] == ["app"]
    assert fake.registered["subscribe_events"]["meta"]["ui"]["visibility"] == ["app"]
    assert fake.registered["submit_command"]["meta"]["ui"]["visibility"] == ["app"]
    # read tools carry a ui:// resource reference in their _meta.ui.resourceUri
    assert fake.registered["render_dashboard"]["meta"]["ui"]["resourceUri"] == "ui://cao/dashboard"
    # submit_command advertises its required scopes via _meta.ui
    assert fake.registered["submit_command"]["meta"]["ui"]["requiredScopes"] == [
        "cao:write",
        "cao:admin",
    ]


# ---------------------------------------------------------------------------
# Task 12.1 (Unit tier) — remaining choke-point matrix cells.
#
# Req 19.10: an empty agent name (create_session with a blank agent_profile) is
#            intercepted as a validation error before any Backplane call.
# Req 19.12: an oversized task payload is rejected at the choke point with a
#            structured warning rather than forwarded to the Backplane.


def test_submit_command_empty_agent_name_is_validation_error() -> None:
    """create_session with a blank agent_profile -> validation error (Req 19.10).

    The choke point must intercept the empty name and never reach routing/HTTP;
    we patch requests.post to fail loudly if it is called.
    """

    def _boom(*args: Any, **kwargs: Any):  # pragma: no cover - must not run
        raise AssertionError("routing should not be reached for an empty agent name")

    for blank in ("", "   "):
        with patch.object(app_tools.requests, "post", _boom):
            result = app_tools._submit_command_impl("create_session", {"agent_profile": blank})
        assert result["success"] is False
        assert "missing required field: agent_profile" in result["error"]


def test_submit_command_rejects_oversized_payload() -> None:
    """An oversized task payload is rejected before routing (Req 19.12).

    The size guard runs after the scope pre-check (default-off passes) and before
    routing, so an oversized message never reaches the Backplane.
    """

    def _boom(*args: Any, **kwargs: Any):  # pragma: no cover - must not run
        raise AssertionError("routing should not be reached for an oversized payload")

    huge = "x" * (app_tools.MAX_PAYLOAD_CHARS + 100)
    with patch.object(app_tools.requests, "post", _boom):
        result = app_tools._submit_command_impl(
            "send_message", {"terminal_id": "abcd1234", "message": huge}
        )
    assert result["success"] is False
    assert "payload too large" in result["error"]
    assert result["kind"] == "send_message"


def test_submit_command_accepts_payload_at_size_boundary() -> None:
    """A payload just under the cap passes the size guard and reaches routing."""

    calls: List[Dict[str, Any]] = []
    # Leave headroom for the JSON envelope (keys/quotes/braces) around the message.
    message = "y" * (app_tools.MAX_PAYLOAD_CHARS - 200)
    with patch.object(app_tools.requests, "post", _capture_post(calls)):
        result = app_tools._submit_command_impl(
            "send_message", {"terminal_id": "abcd1234", "message": message}
        )
    assert result["success"] is True
    assert calls[0]["url"].endswith("/terminals/abcd1234/inbox/messages")


def test_payload_too_large_helper_is_serialized_size_not_field_count() -> None:
    """The size cap is measured on the serialized payload, catching any big field."""

    assert app_tools._payload_too_large({"message": "ok"}) is None
    big_list = {"allowed_tools": ["t" * 100 for _ in range(400)]}
    assert app_tools._payload_too_large(big_list) is not None


# --- _extract_error_detail -------------------------------------------------


def test_extract_error_detail_non_json_returns_fallback() -> None:
    resp = MagicMock()
    resp.json.side_effect = ValueError("not json")
    assert app_tools._extract_error_detail(resp, "fallback") == "fallback"


def test_extract_error_detail_uses_detail_field() -> None:
    resp = MagicMock()
    resp.json.return_value = {"detail": "boom"}
    assert app_tools._extract_error_detail(resp, "fallback") == "boom"


def test_extract_error_detail_non_dict_or_empty_returns_fallback() -> None:
    resp = MagicMock()
    resp.json.return_value = ["not", "a", "dict"]
    assert app_tools._extract_error_detail(resp, "fallback") == "fallback"
    resp.json.return_value = {"detail": ""}
    assert app_tools._extract_error_detail(resp, "fallback") == "fallback"


# --- async tool wrappers delegate to their _impl functions -----------------


@pytest.mark.asyncio
async def test_async_tool_wrappers_delegate_to_impls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registered async tools are thin wrappers over the tested _impl funcs."""

    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    fake = _FakeMCP()
    assert app_tools.register_app_tools(fake) is True
    reg = {name: entry["fn"] for name, entry in fake.registered.items()}

    with patch.object(app_tools, "_render_dashboard_impl", return_value={"v": "dash"}):
        assert await reg["render_dashboard"]() == {"v": "dash"}
    with patch.object(app_tools, "_render_agent_view_impl", return_value={"v": "agent"}) as m:
        assert await reg["render_agent_view"]("t1") == {"v": "agent"}
        m.assert_called_once_with("t1")
    with patch.object(app_tools, "_cao_fetch_history_impl", return_value={"events": []}):
        assert await reg["cao_fetch_history"](limit=5) == {"events": []}
    with patch.object(app_tools, "_subscribe_events_impl", return_value={"sse_url": "/events"}):
        assert await reg["subscribe_events"]() == {"sse_url": "/events"}
    with patch.object(app_tools, "_submit_command_impl", return_value={"success": True}) as m:
        assert await reg["submit_command"]("send_message", {"x": 1}) == {"success": True}
        m.assert_called_once_with("send_message", {"x": 1})


# ---------------------------------------------------------------------------
# H3 — internal MCP->API auth: bearer forwarding + missing-token error.


def test_auth_headers_empty_when_no_local_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-off: no Authorization header is built for the internal hop."""

    monkeypatch.setattr(app_tools, "get_local_bearer", lambda: None)
    assert app_tools._auth_headers() == {}


def test_auth_headers_set_when_local_bearer_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a local token, the internal hop carries a bearer Authorization header."""

    monkeypatch.setattr(app_tools, "get_local_bearer", lambda: "machine-token")
    assert app_tools._auth_headers() == {"Authorization": "Bearer machine-token"}


def test_post_json_forwards_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """_post_json attaches the bearer header when a local token is configured."""

    captured: Dict[str, Any] = {}

    def _fake_post(url, params=None, headers=None, timeout=None):  # noqa: ANN001
        captured["headers"] = headers
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {}
        return resp

    monkeypatch.setattr(app_tools, "get_local_bearer", lambda: "machine-token")
    monkeypatch.setattr(app_tools.requests, "post", _fake_post)
    app_tools._post_json("/sessions", {"agent_profile": "dev"})
    assert captured["headers"] == {"Authorization": "Bearer machine-token"}


def test_post_json_no_header_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-off: _post_json sends headers=None (byte-for-byte unchanged)."""

    captured: Dict[str, Any] = {}

    def _fake_post(url, params=None, headers=None, timeout=None):  # noqa: ANN001
        captured["headers"] = headers
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {}
        return resp

    monkeypatch.setattr(app_tools, "get_local_bearer", lambda: None)
    monkeypatch.setattr(app_tools.requests, "post", _fake_post)
    app_tools._post_json("/sessions", {"agent_profile": "dev"})
    assert captured["headers"] is None


def test_get_json_and_delete_json_forward_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads and deletes both carry the bearer when configured (consistent hop)."""

    seen: Dict[str, Any] = {}

    def _fake_get(url, params=None, headers=None, timeout=None):  # noqa: ANN001
        seen["get"] = headers
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = []
        return resp

    def _fake_delete(url, headers=None, timeout=None):  # noqa: ANN001
        seen["delete"] = headers
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {}
        return resp

    monkeypatch.setattr(app_tools, "get_local_bearer", lambda: "tok")
    monkeypatch.setattr(app_tools.requests, "get", _fake_get)
    monkeypatch.setattr(app_tools.requests, "delete", _fake_delete)
    app_tools._get_json("/sessions")
    app_tools._delete_json("/sessions/cao-x")
    assert seen["get"] == {"Authorization": "Bearer tok"}
    assert seen["delete"] == {"Authorization": "Bearer tok"}


def test_submit_command_auth_enabled_without_local_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth on + no local token -> structured misconfig error, never routed."""

    monkeypatch.setenv("AUTH0_DOMAIN", "example.auth0.com")
    monkeypatch.delenv("CAO_AUTH_LOCAL_TOKEN", raising=False)
    # The scope pre-check must PASS (full set) so the misconfig check is what
    # blocks; patch it explicitly to avoid touching a real JWKS.
    monkeypatch.setattr(
        app_tools, "get_scopes_for_local_token", lambda: ["cao:read", "cao:write", "cao:admin"]
    )

    def _boom(*args: Any, **kwargs: Any):  # pragma: no cover - must not run
        raise AssertionError("routing must not be reached when the token is missing")

    monkeypatch.setattr(app_tools.requests, "post", _boom)
    result = app_tools._submit_command_impl("create_session", {"agent_profile": "dev"})
    assert result["success"] is False
    assert result["kind"] == "create_session"
    assert "CAO_AUTH_LOCAL_TOKEN" in result["error"]


# ---------------------------------------------------------------------------
# render_graph_view — B4 graph renderer tool wrapping GET /graph/{provider}.


def test_render_graph_view_wraps_get_json_with_params() -> None:
    """render_graph_view calls _get_json with the provider path and scope/scope_id params."""

    captured: Dict[str, Any] = {}

    def _fake_get_json(path: str, **params: Any) -> Any:
        captured["path"] = path
        captured["params"] = params
        return {"nodes": [], "edges": [], "meta": {}}

    with patch.object(app_tools, "_get_json", _fake_get_json):
        result = app_tools._render_graph_view_impl("memory", scope="global", scope_id="s1")

    assert captured["path"] == "/graph/memory"
    assert captured["params"] == {"scope": "global", "scope_id": "s1"}
    assert result == {"nodes": [], "edges": [], "meta": {}}


def test_render_graph_view_propagates_http_error() -> None:
    """A requests.HTTPError from _get_json propagates out of render_graph_view (no swallow)."""

    def _boom(path: str, **params: Any) -> Any:
        raise app_tools.requests.HTTPError("boom")

    with patch.object(app_tools, "_get_json", _boom):
        with pytest.raises(app_tools.requests.HTTPError):
            app_tools._render_graph_view_impl("memory")


def test_register_app_tools_registers_render_graph_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """render_graph_view registers with model+app visibility and the graph resource URI."""

    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    fake = _FakeMCP()
    assert app_tools.register_app_tools(fake) is True

    assert "render_graph_view" in fake.registered
    meta = fake.registered["render_graph_view"]["meta"]["ui"]
    assert meta["visibility"] == ["model", "app"]
    assert meta["resourceUri"] == "ui://cao/graph"
    assert meta["requiredScopes"] == []


@pytest.mark.asyncio
async def test_render_graph_view_tool_delegates_to_impl(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registered async render_graph_view tool is a thin wrapper over the _impl."""

    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
    fake = _FakeMCP()
    assert app_tools.register_app_tools(fake) is True
    reg = {name: entry["fn"] for name, entry in fake.registered.items()}

    with patch.object(app_tools, "_render_graph_view_impl", return_value={"nodes": []}) as mocked:
        assert await reg["render_graph_view"]("memory", scope="global") == {"nodes": []}
        mocked.assert_called_once_with("memory", scope="global", scope_id=None)


def test_submit_command_forwards_bearer_when_token_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth on + local token -> command routes and carries the bearer header."""

    monkeypatch.setenv("AUTH0_DOMAIN", "example.auth0.com")
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "local-machine-token")
    # Avoid a real JWKS round-trip during the UX scope pre-check.
    monkeypatch.setattr(
        app_tools, "get_scopes_for_local_token", lambda: ["cao:read", "cao:write", "cao:admin"]
    )

    captured: Dict[str, Any] = {}

    def _fake_post(url, params=None, headers=None, timeout=None):  # noqa: ANN001
        captured["headers"] = headers
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"id": "t1", "session_name": "cao-x"}
        return resp

    monkeypatch.setattr(app_tools.requests, "post", _fake_post)
    result = app_tools._submit_command_impl("create_session", {"agent_profile": "dev"})
    assert result["success"] is True
    assert captured["headers"] == {"Authorization": "Bearer local-machine-token"}
