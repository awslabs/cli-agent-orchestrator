"""The shared MCP endpoint, exercised by real MCP clients over real HTTP (#745).

Two acceptance criteria could not be honestly claimed from the existing tests:

- "Shared MCP requests retain distinct authenticated caller contexts; supported
  stdio clients can use forwarding compatibility." The identity middleware was
  tested by calling ``on_call_tool`` in-process with a bare ``object()`` as the
  context. That proves the branch, not that an MCP client's call carries the
  identity through the transport - and there was no stdio forwarding path at all.
- "MCP compatibility is demonstrated with the selected SDKs/clients, not assumed
  from a specification link." Nothing here had ever completed an ``initialize``
  handshake with the installed SDK.

So these tests start the endpoint on a real socket and drive it with the
official ``mcp`` client SDK, including through the stdio shim as a child
process. The tool under test returns the identity the server resolved, which is
the only thing that distinguishes "the header arrived" from "the header was
accepted and then ignored".
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest
import uvicorn
from fastmcp import FastMCP
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from cli_agent_orchestrator.mcp_server.caller_context import (
    CALLER_TERMINAL_HEADER,
    resolve_caller_terminal_id,
)
from cli_agent_orchestrator.mcp_server.http_hosting import (
    MCP_HTTP_PATH,
    RUNTIME_TOKEN_HEADER,
    build_http_app,
)
from cli_agent_orchestrator.mcp_server.stdio_bridge import SHIM_NAME
from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_MCP_STDIO_BRIDGE_COMMAND,
    resolve_cao_mcp_command,
)

TOKEN = "shared-endpoint-test-token"
CALL_TIMEOUT = 30


def _free_port_socket():
    """A bound listening socket, handed to uvicorn so no port race exists."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    return sock


@pytest.fixture()
def shared_endpoint(monkeypatch):
    """A live shared HTTP MCP endpoint. Yields its URL.

    Built through ``build_http_app`` rather than by attaching the middleware by
    hand, so the tests exercise the same wiring ``cao-mcp-server`` uses.
    """
    monkeypatch.setenv("CAO_RUNTIME_TOKEN", TOKEN)
    # No CAO_TERMINAL_ID in the server's own env: if a test ever sees an
    # identity, it came from the request, not from the process.
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)

    mcp = FastMCP("cao-mcp-server-under-test")

    @mcp.tool()
    def whoami() -> str:
        """Report the caller identity the server resolved for this request."""
        return resolve_caller_terminal_id() or "<none>"

    build_http_app(mcp)
    app = mcp.http_app(path=MCP_HTTP_PATH, transport="http")

    sock = _free_port_socket()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()

    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - startup failure
            server.should_exit = True
            raise RuntimeError("shared MCP endpoint did not start")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}{MCP_HTTP_PATH}"

    server.should_exit = True
    thread.join(timeout=30)
    with contextlib.suppress(OSError):
        sock.close()


@contextlib.asynccontextmanager
async def _transport(url, headers):
    """Streamable HTTP streams with CAO's headers on every request.

    Headers ride on the httpx client rather than a per-call argument, which is
    how the shim carries them too - the endpoint authenticates every request,
    not just the handshake.
    """
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            yield read, write


@contextlib.asynccontextmanager
async def _session(url, headers):
    """An initialized MCP session over Streamable HTTP."""
    async with _transport(url, headers) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), CALL_TIMEOUT)
            yield session


def _headers(caller=None, token=TOKEN):
    headers = {}
    if token is not None:
        headers[RUNTIME_TOKEN_HEADER] = token
    if caller is not None:
        headers[CALLER_TERMINAL_HEADER] = caller
    return headers


def _text(result):
    return "".join(block.text for block in result.content if getattr(block, "text", None))


def _flatten_error(exc):
    """All the text in an exception, including an ExceptionGroup's members.

    A failure raised inside the streamable-HTTP task group surfaces as a
    ``BaseExceptionGroup``, so ``str(exc)`` alone does not contain the server's
    message.
    """
    parts = [f"{type(exc).__name__}: {exc}"]
    for inner in getattr(exc, "exceptions", ()):
        parts.append(_flatten_error(inner))
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        parts.append(_flatten_error(cause))
    return " | ".join(parts)


class TestOfficialSdkRoundTrip:
    @pytest.mark.asyncio
    async def test_a_real_client_initializes_and_calls_a_tool(self, shared_endpoint):
        async with _session(shared_endpoint, _headers(caller="abcd1234")) as session:
            tools = await asyncio.wait_for(session.list_tools(), CALL_TIMEOUT)
            assert "whoami" in {t.name for t in tools.tools}

            result = await asyncio.wait_for(session.call_tool("whoami"), CALL_TIMEOUT)
            assert result.isError is False
            # The identity the SERVER resolved, not what the client sent: this
            # is what separates "header arrived" from "header was ignored".
            assert _text(result) == "abcd1234"

    @pytest.mark.asyncio
    async def test_the_handshake_negotiates_a_version_the_sdk_supports(self, shared_endpoint):
        """Compatibility demonstrated against the installed SDK, not a spec link.

        ``initialize`` is where an incompatible pair fails, and it fails before
        any tool runs - so a negotiated version is also the evidence that an
        unsupported combination would be rejected before dispatch.
        """
        from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

        async with _transport(shared_endpoint, _headers()) as (rd, wr):
            async with ClientSession(rd, wr) as session:
                init = await asyncio.wait_for(session.initialize(), CALL_TIMEOUT)

        assert init.protocolVersion in SUPPORTED_PROTOCOL_VERSIONS
        assert init.serverInfo.name == "cao-mcp-server-under-test"

    @pytest.mark.asyncio
    async def test_concurrent_sessions_keep_distinct_caller_identities(self, shared_endpoint):
        """The reason a process-global CAO_TERMINAL_ID cannot be used.

        Both calls are in flight against one server process at the same time. A
        shared endpoint that resolved identity from its own environment - or that
        leaked one request's context into the next - returns the same answer
        twice here.
        """

        async def ask(caller):
            async with _session(shared_endpoint, _headers(caller=caller)) as session:
                result = await asyncio.wait_for(session.call_tool("whoami"), CALL_TIMEOUT)
                return _text(result)

        first, second = await asyncio.gather(ask("aaaa1111"), ask("bbbb2222"))
        assert first == "aaaa1111"
        assert second == "bbbb2222"

    @pytest.mark.asyncio
    async def test_sequential_calls_do_not_inherit_the_previous_identity(self, shared_endpoint):
        """One process serves many agents in turn; the reset has to hold."""
        async with _session(shared_endpoint, _headers(caller="cccc3333")) as session:
            assert _text(await asyncio.wait_for(session.call_tool("whoami"), CALL_TIMEOUT)) == (
                "cccc3333"
            )
        async with _session(shared_endpoint, _headers()) as session:
            # No identity presented, so none may be resolved - not the last
            # caller's.
            assert _text(await asyncio.wait_for(session.call_tool("whoami"), CALL_TIMEOUT)) == (
                "<none>"
            )


class TestAuthOverTheWire:
    """Every request, not just the tool call.

    The check used to hang off ``on_call_tool``, which FastMCP dispatches only
    for ``tools/call``. ``initialize`` and ``tools/list`` went to the base class's
    pass-through, so an unauthenticated client could open a session and read the
    whole tool surface — every name, description and argument schema of the
    control plane — and was refused only when it tried to use one. These tests
    drive the handshake itself, because that is where the hole was (Copilot review
    on #802).
    """

    @pytest.mark.asyncio
    async def test_a_wrong_token_cannot_even_open_a_session(self, shared_endpoint, caplog):
        with pytest.raises(BaseException) as excinfo:
            async with _session(shared_endpoint, _headers(caller="abcd1234", token="wrong")):
                pass
        # The client is told only that its request was rejected — the endpoint
        # does not describe its own auth to an unauthenticated caller — so the
        # refusal is asserted at the server, where the reason is recorded.
        assert "McpError" in _flatten_error(excinfo.value)
        assert "unauthorized" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_no_token_at_all_cannot_even_open_a_session(self, shared_endpoint, caplog):
        with pytest.raises(BaseException) as excinfo:
            async with _session(shared_endpoint, _headers(caller="abcd1234", token=None)):
                pass
        assert "McpError" in _flatten_error(excinfo.value)
        assert "unauthorized" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_an_unauthenticated_client_cannot_enumerate_the_tools(self, shared_endpoint):
        """The disclosure the old gate allowed, asserted at the transport.

        ``tools/list`` without an initialized session — a raw POST, exactly what
        a client that skipped the handshake would send.
        """
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        async with httpx.AsyncClient() as client:
            response = await client.post(
                shared_endpoint,
                json=body,
                headers={"Accept": "application/json, text/event-stream"},
                timeout=CALL_TIMEOUT,
            )
        assert "whoami" not in response.text

    @pytest.mark.asyncio
    async def test_the_right_token_still_lists_the_tools(self, shared_endpoint):
        """The gate refuses the unauthenticated, not everyone."""
        async with _session(shared_endpoint, _headers(caller="abcd1234")) as session:
            tools = await asyncio.wait_for(session.list_tools(), CALL_TIMEOUT)
        assert "whoami" in {t.name for t in tools.tools}


def _shim_params(url, env_extra):
    """Launch the shim exactly as a provider would: a resolved command + env."""
    command, args = resolve_cao_mcp_command(CAO_MCP_STDIO_BRIDGE_COMMAND, [])
    env = {k: v for k, v in os.environ.items() if k != "CAO_TERMINAL_ID"}
    env["CAO_MCP_HTTP_URL"] = url
    env.update(env_extra)
    return StdioServerParameters(command=command, args=args, env=env)


class TestStdioForwardingShim:
    @pytest.mark.asyncio
    async def test_a_stdio_client_reaches_the_shared_endpoint(self, shared_endpoint):
        """The forwarding-compatibility criterion, end to end.

        A stdio-only provider spawns a command and injects ``CAO_TERMINAL_ID``.
        Neither of those changes here; the identity crosses a process boundary
        and an HTTP hop and still arrives as this request's caller.
        """
        params = _shim_params(
            shared_endpoint, {"CAO_RUNTIME_TOKEN": TOKEN, "CAO_TERMINAL_ID": "dddd4444"}
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), CALL_TIMEOUT)
                result = await asyncio.wait_for(session.call_tool("whoami"), CALL_TIMEOUT)

        assert result.isError is False
        assert _text(result) == "dddd4444"

    @pytest.mark.asyncio
    async def test_the_shim_advertises_the_endpoints_tools_and_nothing_of_its_own(
        self, shared_endpoint
    ):
        """It is a forwarder, not a second control server.

        An extra tool appearing here would mean the shim had grown its own
        surface - the thing #745 says a shim must not become.
        """
        params = _shim_params(
            shared_endpoint, {"CAO_RUNTIME_TOKEN": TOKEN, "CAO_TERMINAL_ID": "eeee5555"}
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), CALL_TIMEOUT)
                tools = await asyncio.wait_for(session.list_tools(), CALL_TIMEOUT)

        assert {t.name for t in tools.tools} == {"whoami"}

    def test_the_shim_refuses_to_start_unauthenticated(self, shared_endpoint):
        """No token means no start - never a quiet fallback to local tools.

        Run as a subprocess because that is how the failure reaches an operator:
        as the child's exit status and stderr, in the provider's own log.
        """
        command, args = resolve_cao_mcp_command(CAO_MCP_STDIO_BRIDGE_COMMAND, [])
        env = {k: v for k, v in os.environ.items() if k != "CAO_RUNTIME_TOKEN"}
        env["CAO_MCP_HTTP_URL"] = shared_endpoint
        proc = subprocess.run(
            [command, *args], env=env, capture_output=True, text=True, timeout=120
        )
        assert proc.returncode != 0
        assert "CAO_RUNTIME_TOKEN" in proc.stderr

    def test_the_shim_refuses_to_start_without_a_shared_url(self, monkeypatch):
        """A missing URL is fatal too — never a quiet fallback to the local bind
        address, which would reintroduce the per-agent in-pod server this shim
        removes (Copilot follow-up on #802)."""
        from cli_agent_orchestrator.mcp_server import stdio_bridge

        monkeypatch.setenv("CAO_RUNTIME_TOKEN", "tok")
        monkeypatch.delenv("CAO_MCP_HTTP_URL", raising=False)
        with pytest.raises(SystemExit, match="CAO_MCP_HTTP_URL"):
            stdio_bridge.resolve_shared_endpoint_url()

    def test_the_shim_command_resolves_without_depending_on_path(self):
        """Bundled like cao-mcp-server, so it needs the same resolution.

        A provider config that kept the bare name would silently start an agent
        with no orchestration tools wherever the script dir is off the agent
        subprocess's PATH.
        """
        command, args = resolve_cao_mcp_command(CAO_MCP_STDIO_BRIDGE_COMMAND, [])
        assert command != CAO_MCP_STDIO_BRIDGE_COMMAND, "bare name was passed through unresolved"
        if args:
            assert args[:2] == ["-m", "cli_agent_orchestrator.mcp_server.stdio_bridge"]
        else:
            assert os.path.isabs(command)

    def test_the_shim_is_declared_as_a_console_script(self):
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        assert f'"{CAO_MCP_STDIO_BRIDGE_COMMAND}" =' in pyproject.read_text()


class TestForwardHeaders:
    def test_a_missing_caller_id_is_a_warning_not_a_failure(self, monkeypatch, caplog):
        """Operator use of the endpoint has no terminal identity.

        Refusing to start would break that, but staying silent would hide a
        provider whose terminal id never reached the child env - so it warns.
        """
        from cli_agent_orchestrator.mcp_server.stdio_bridge import build_forward_headers

        monkeypatch.setenv("CAO_RUNTIME_TOKEN", TOKEN)
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
        with caplog.at_level("WARNING"):
            headers = build_forward_headers()
        assert CALLER_TERMINAL_HEADER not in headers
        assert headers[RUNTIME_TOKEN_HEADER] == TOKEN
        assert SHIM_NAME in caplog.text

    def test_the_url_prefers_the_advertised_address_over_the_bind_address(self, monkeypatch):
        from cli_agent_orchestrator.mcp_server.http_hosting import shared_endpoint_url

        # A server binding 0.0.0.0 is not an address a client can dial; the
        # advertised URL is the one callers use.
        monkeypatch.setenv("CAO_MCP_HTTP_HOST", "0.0.0.0")
        monkeypatch.delenv("CAO_MCP_HTTP_URL", raising=False)
        assert "0.0.0.0" not in shared_endpoint_url()

        monkeypatch.setenv("CAO_MCP_HTTP_URL", "https://cao-mcp.example.internal/mcp")
        assert shared_endpoint_url() == "https://cao-mcp.example.internal/mcp"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
