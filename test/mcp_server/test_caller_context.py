"""Per-request caller identity for the shared MCP endpoint (#745)."""

import asyncio

import pytest

from cli_agent_orchestrator.mcp_server import caller_context as cc


class TestResolver:
    def test_env_fallback_when_no_context(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
        assert cc.resolve_caller_terminal_id() == "abcd1234"

    def test_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
        assert cc.resolve_caller_terminal_id() is None

    def test_context_overrides_env(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "aaaa0000")
        token = cc.set_caller_terminal_id("bbbb1111")
        try:
            assert cc.resolve_caller_terminal_id() == "bbbb1111"
        finally:
            cc.reset_caller_terminal_id(token)
        # After reset, resolution returns to the env value.
        assert cc.resolve_caller_terminal_id() == "aaaa0000"

    def test_env_is_lenient_lookup(self, monkeypatch):
        # resolve is a pure lookup: it does not validate the env value, so the
        # existing leniency of direct-env callers is preserved. Strictness lives
        # in _current_terminal_id (which validates on top) and at set() time.
        monkeypatch.setenv("CAO_TERMINAL_ID", "NOT-HEX")
        assert cc.resolve_caller_terminal_id() == "NOT-HEX"

    def test_malformed_context_raises(self):
        # The HTTP header path DOES validate — an authenticated request cannot
        # bind a malformed identity.
        with pytest.raises(cc.CallerIdentityError):
            cc.set_caller_terminal_id("bad!")

    @pytest.mark.asyncio
    async def test_context_is_isolated_between_concurrent_tasks(self, monkeypatch):
        """Two concurrent requests must not see each other's identity — the
        property a process-global env var cannot provide (#745)."""
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
        seen = {}

        async def request(name, tid):
            token = cc.set_caller_terminal_id(tid)
            try:
                await asyncio.sleep(0.01)
                seen[name] = cc.resolve_caller_terminal_id()
            finally:
                cc.reset_caller_terminal_id(token)

        # Each task runs in its own contextvars copy.
        await asyncio.gather(
            asyncio.create_task(request("a", "aaaa0000")),
            asyncio.create_task(request("b", "bbbb1111")),
        )
        assert seen == {"a": "aaaa0000", "b": "bbbb1111"}


class TestHttpHostingGate:
    def test_build_http_app_fails_closed_without_token(self, monkeypatch):
        monkeypatch.delenv("CAO_RUNTIME_TOKEN", raising=False)
        from fastmcp import FastMCP

        from cli_agent_orchestrator.mcp_server.http_hosting import SharedTokenError, build_http_app

        with pytest.raises(SharedTokenError):
            build_http_app(FastMCP("test"))

    def test_build_http_app_attaches_middleware_with_token(self, monkeypatch):
        monkeypatch.setenv("CAO_RUNTIME_TOKEN", "tok")
        monkeypatch.setenv("CAO_MCP_HTTP_PORT", "9899")
        from fastmcp import FastMCP

        from cli_agent_orchestrator.mcp_server.http_hosting import build_http_app

        app, host, port = build_http_app(FastMCP("test"))
        assert port == 9899
        assert host == "127.0.0.1"


class TestMiddlewareAuth:
    @pytest.mark.asyncio
    async def test_rejects_bad_token(self, monkeypatch):
        from cli_agent_orchestrator.mcp_server.http_hosting import CallerIdentityMiddleware

        mw = CallerIdentityMiddleware("expected-token")

        async def call_next(ctx):
            return "ran"

        # No request headers → empty token → rejected.
        with pytest.raises(ValueError, match="unauthorized"):
            await mw.on_call_tool(object(), call_next)
