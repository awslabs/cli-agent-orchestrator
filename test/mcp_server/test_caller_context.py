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


class TestAnAnonymousRequestDoesNotBorrowTheHostsIdentity:
    """A request that named no caller must resolve to nobody, not to the process.

    The shared endpoint runs inside one agent's runtime, so ``CAO_TERMINAL_ID``
    is set in that process — it names the hosting agent. Binding ``None`` used
    to be indistinguishable from "no request in flight", so a request that
    passed the token gate without the caller header resolved to the host's own
    terminal id and acted as that agent: read its context, sent messages as it
    (review finding 7 on #802). The stdio path, where the env var genuinely
    does name the caller, is unaffected.
    """

    def test_a_request_with_no_caller_header_resolves_to_nobody(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "ffff9999")
        token = cc.set_caller_terminal_id(None)
        try:
            assert cc.resolve_caller_terminal_id() is None
        finally:
            cc.reset_caller_terminal_id(token)

    def test_an_empty_header_value_is_anonymous_too(self, monkeypatch):
        """An absent header and a blank one are the same claim: none."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "ffff9999")
        token = cc.set_caller_terminal_id("")
        try:
            assert cc.resolve_caller_terminal_id() is None
        finally:
            cc.reset_caller_terminal_id(token)

    def test_resetting_the_request_restores_the_process_identity(self, monkeypatch):
        """The stdio server and the CLI still read the env var; only the window
        of an in-flight request is closed to it."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "ffff9999")
        token = cc.set_caller_terminal_id(None)
        cc.reset_caller_terminal_id(token)
        assert cc.resolve_caller_terminal_id() == "ffff9999"

    def test_an_anonymous_request_does_not_leak_into_a_named_one(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "ffff9999")
        anon = cc.set_caller_terminal_id(None)
        try:
            named = cc.set_caller_terminal_id("cccc2222")
            try:
                assert cc.resolve_caller_terminal_id() == "cccc2222"
            finally:
                cc.reset_caller_terminal_id(named)
            assert cc.resolve_caller_terminal_id() is None
        finally:
            cc.reset_caller_terminal_id(anon)

    @pytest.mark.asyncio
    async def test_a_named_request_beside_an_anonymous_one_keeps_its_own_answer(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "ffff9999")
        seen = {}

        async def request(name, tid):
            token = cc.set_caller_terminal_id(tid)
            try:
                await asyncio.sleep(0.01)
                seen[name] = cc.resolve_caller_terminal_id()
            finally:
                cc.reset_caller_terminal_id(token)

        await asyncio.gather(
            asyncio.create_task(request("named", "aaaa0000")),
            asyncio.create_task(request("anon", None)),
        )
        assert seen == {"named": "aaaa0000", "anon": None}

    @pytest.mark.asyncio
    async def test_the_middleware_binds_anonymity_for_a_headerless_request(self, monkeypatch):
        """End to end through the middleware: authenticated, no caller header."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "ffff9999")
        from cli_agent_orchestrator.mcp_server import http_hosting

        monkeypatch.setattr(
            http_hosting, "get_http_headers", lambda: {http_hosting.RUNTIME_TOKEN_HEADER: "tok"}
        )
        mw = http_hosting.CallerIdentityMiddleware("tok")
        seen = {}

        async def call_next(_ctx):
            seen["resolved"] = cc.resolve_caller_terminal_id()
            return "ran"

        assert await mw.on_call_tool(object(), call_next) == "ran"
        assert seen["resolved"] is None
        # And the request's anonymity does not outlive it.
        assert cc.resolve_caller_terminal_id() == "ffff9999"

    @pytest.mark.asyncio
    async def test_the_middleware_binds_the_header_when_it_is_present(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "ffff9999")
        from cli_agent_orchestrator.mcp_server import http_hosting

        monkeypatch.setattr(
            http_hosting,
            "get_http_headers",
            lambda: {
                http_hosting.RUNTIME_TOKEN_HEADER: "tok",
                cc.CALLER_TERMINAL_HEADER: "dddd3333",
            },
        )
        mw = http_hosting.CallerIdentityMiddleware("tok")
        seen = {}

        async def call_next(_ctx):
            seen["resolved"] = cc.resolve_caller_terminal_id()
            return "ran"

        await mw.on_call_tool(object(), call_next)
        assert seen["resolved"] == "dddd3333"

    def test_the_tools_refuse_rather_than_act_as_the_host(self, monkeypatch):
        """What the resolver's ``None`` buys the callers above it: the identity
        helpers report "no caller" instead of naming the hosting agent."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "ffff9999")
        from cli_agent_orchestrator.utils.orchestration import _current_terminal_id

        token = cc.set_caller_terminal_id(None)
        try:
            assert _current_terminal_id() is None
        finally:
            cc.reset_caller_terminal_id(token)
        assert _current_terminal_id() == "ffff9999"


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
