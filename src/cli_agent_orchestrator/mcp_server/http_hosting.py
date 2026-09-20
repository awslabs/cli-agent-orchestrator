"""Shared HTTP hosting for cao-mcp-server (#745).

Running the stdio entry point in a Deployment does not create a shared network
service — it serves one client over stdin/stdout. This module adds the
supported shared mode: FastMCP's Streamable HTTP transport plus a middleware
that resolves the caller's terminal identity per request instead of from a
process-global ``CAO_TERMINAL_ID``.

Two gates, in order, before any tool runs:

1. **Shared-token auth.** Every request must carry ``X-CAO-Runtime-Token``
   matching ``CAO_RUNTIME_TOKEN``. Absent config → the endpoint refuses to
   start, so a shared endpoint is never brought up unauthenticated. (#774
   replaces this shared token with per-caller delegated credentials whose
   verified subject becomes the identity directly.)
2. **Per-request identity.** The caller's terminal id is read from
   ``X-CAO-Caller-Terminal-Id`` and bound to a request-scoped context for the
   duration of the call, then reset. An agent-supplied id is only trusted
   because the token gate already proved the caller is an authorized runtime;
   the id selects WHICH terminal the runtime acts as, it is not itself the
   authorization.

stdio hosting is untouched: it has no middleware and resolves identity from the
process env, exactly as before.
"""

import hmac
import logging
import os

from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext

from cli_agent_orchestrator.mcp_server.caller_context import (
    CALLER_TERMINAL_HEADER,
    CallerIdentityError,
    reset_caller_terminal_id,
    set_caller_terminal_id,
)

logger = logging.getLogger(__name__)

RUNTIME_TOKEN_HEADER = "x-cao-runtime-token"
RUNTIME_TOKEN_ENV = "CAO_RUNTIME_TOKEN"

# FastMCP's Streamable HTTP default mount path. Named here so the server, the
# stdio forwarding shim and the tests all derive the endpoint from one place
# rather than each hardcoding "/mcp".
MCP_HTTP_PATH = "/mcp"
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 9890


class SharedTokenError(RuntimeError):
    """Raised at startup when shared HTTP hosting has no token configured."""


class CallerIdentityMiddleware(Middleware):
    """Authenticate the request and bind its caller terminal id per request."""

    def __init__(self, expected_token: str):
        self._expected_token = expected_token

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        headers = get_http_headers()
        presented = headers.get(RUNTIME_TOKEN_HEADER, "")
        if not hmac.compare_digest(presented, self._expected_token):
            # Never fall through to an anonymous/global identity — refuse.
            raise ValueError("unauthorized: missing or invalid runtime token")

        caller = headers.get(CALLER_TERMINAL_HEADER) or None
        try:
            token = set_caller_terminal_id(caller)
        except CallerIdentityError as e:
            raise ValueError(str(e)) from e
        try:
            return await call_next(context)
        finally:
            # Reset so one request's identity never leaks into the next served
            # by this shared process.
            reset_caller_terminal_id(token)


def build_http_app(mcp):
    """Attach the identity middleware and return (mcp, host, port).

    Fails closed: raises if ``CAO_RUNTIME_TOKEN`` is unset, so a shared HTTP
    endpoint cannot be started without authentication.
    """
    expected = os.environ.get(RUNTIME_TOKEN_ENV, "").strip()
    if not expected:
        raise SharedTokenError(
            f"shared HTTP MCP hosting requires {RUNTIME_TOKEN_ENV} to be set; refusing to "
            "start an unauthenticated shared endpoint"
        )
    mcp.add_middleware(CallerIdentityMiddleware(expected))
    host, port = configured_host_port()
    return mcp, host, port


def configured_host_port():
    """The (host, port) the shared endpoint binds, from env or defaults."""
    host = os.environ.get("CAO_MCP_HTTP_HOST", DEFAULT_HTTP_HOST)
    port = int(os.environ.get("CAO_MCP_HTTP_PORT", str(DEFAULT_HTTP_PORT)))
    return host, port


def shared_endpoint_url() -> str:
    """URL a client should dial for the shared HTTP MCP endpoint.

    ``CAO_MCP_HTTP_URL`` wins when set, because a bind address is not an
    advertised address: in a cluster the endpoint binds ``0.0.0.0`` inside its
    pod and callers reach it through a Service DNS name. Falling back to the
    bind host is only correct for the same-host case.
    """
    explicit = os.environ.get("CAO_MCP_HTTP_URL", "").strip()
    if explicit:
        return explicit
    host, port = configured_host_port()
    if host in ("0.0.0.0", "::", ""):
        host = DEFAULT_HTTP_HOST
    return f"http://{host}:{port}{MCP_HTTP_PATH}"
