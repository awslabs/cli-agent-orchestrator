"""stdio-to-HTTP forwarding shim for the shared MCP endpoint (#745).

Every shipped provider spawns its MCP server as a stdio child process and
identifies itself by the ``CAO_TERMINAL_ID`` in that child's environment. In a
cluster that is the wrong shape twice over: the tools would run in the agent's
own pod rather than against the shared endpoint, and a shared endpoint cannot
use one process-global terminal id to speak for several agents.

This shim keeps the provider side exactly as it is - a stdio child called with
a command - and turns it into a client of the shared endpoint:

    provider  --stdio-->  cao-mcp-stdio-bridge  --HTTP-->  cao-mcp-server

The process env that used to select the identity now becomes the per-request
caller header, so a provider needs no change at all: point its configured
command at ``cao-mcp-stdio-bridge`` instead of ``cao-mcp-server`` and the same
``CAO_TERMINAL_ID`` it already injects arrives as
``X-CAO-Caller-Terminal-Id`` on every forwarded call.

**This is not another control server.** It registers no tools of its own, holds
no state, reads no database, and makes no orchestration decisions. Its whole
tool surface is whatever the shared endpoint advertises, discovered at
connection time via the MCP protocol - if the endpoint gains a tool, the shim
forwards it without a code change here.

Failure posture: a missing token or URL is fatal at startup, never a silent
fallback to running the tools locally. A shim that quietly became an in-pod
server would reintroduce exactly the per-agent control server this removes.
"""

import logging
import os
import pathlib
import sys

from cli_agent_orchestrator.mcp_server.caller_context import CALLER_TERMINAL_HEADER
from cli_agent_orchestrator.mcp_server.http_hosting import (
    RUNTIME_TOKEN_ENV,
    RUNTIME_TOKEN_HEADER,
    shared_endpoint_url,
)
from cli_agent_orchestrator.utils.mcp_resolution import RUNTIME_TOKEN_FILE_ENV

logger = logging.getLogger(__name__)

SHIM_NAME = "cao-mcp-stdio-bridge"


def build_forward_headers() -> dict:
    """Headers every forwarded request carries.

    Raises SystemExit when the shared token is absent: without it the endpoint
    would reject every call anyway, and failing here makes the cause legible in
    the provider's own startup output instead of as an auth error per tool call.
    """
    token = os.environ.get(RUNTIME_TOKEN_ENV, "").strip()
    if not token:
        # A FILE is the credential-safe delivery path for a provider whose MCP
        # config is serialized into argv or into a config file it re-reads. The
        # value would be world-readable there (argv is readable by every local
        # process); a path is not, and the file itself is 0600.
        #
        # This exists because the two previous mechanisms each failed one way: the
        # value in the config exposed it, and relying on the child inheriting the
        # parent environment is not portable — Kimi does not pass its environment
        # to MCP subprocesses, so omitting the value there made the shim exit at
        # startup and every forwarded tool call fail (Copilot review on #802).
        token_file = os.environ.get(RUNTIME_TOKEN_FILE_ENV, "").strip()
        if token_file:
            try:
                token = pathlib.Path(token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SystemExit(
                    f"{SHIM_NAME} could not read {RUNTIME_TOKEN_FILE_ENV}=" f"{token_file}: {exc}"
                ) from exc
    if not token:
        raise SystemExit(
            f"{SHIM_NAME} requires {RUNTIME_TOKEN_ENV} or {RUNTIME_TOKEN_FILE_ENV} to "
            "authenticate against the shared MCP endpoint; refusing to start "
            "unauthenticated"
        )
    headers = {RUNTIME_TOKEN_HEADER: token}

    caller = os.environ.get("CAO_TERMINAL_ID", "").strip()
    if caller:
        headers[CALLER_TERMINAL_HEADER] = caller
    else:
        # Not fatal: operator-facing use of the endpoint has no terminal
        # identity. Loud, because a *provider* reaching here means its terminal
        # id never made it into the child env, and its tool calls will be
        # rejected by the endpoint's identity gate rather than misattributed.
        logger.warning(
            "%s: CAO_TERMINAL_ID is unset, so forwarded calls carry no caller "
            "identity. Agent tool calls that require one will be refused.",
            SHIM_NAME,
        )
    return headers


def resolve_shared_endpoint_url() -> str:
    """The shared endpoint URL, required to be explicit for this shim.

    ``shared_endpoint_url()`` falls back to the local bind host — correct for its
    same-host callers, but for this shim that fallback silently points at
    ``http://127.0.0.1:<port>`` and reintroduces the per-agent, in-pod MCP server
    the shim exists to remove (Copilot follow-up on #802). The module contract is
    that a missing URL is fatal, so require ``CAO_MCP_HTTP_URL`` explicitly here
    and fail closed, the same posture as the missing token.
    """
    if not os.environ.get("CAO_MCP_HTTP_URL", "").strip():
        raise SystemExit(
            f"{SHIM_NAME} requires CAO_MCP_HTTP_URL to name the shared MCP endpoint; "
            "refusing to start against a local fallback address"
        )
    return shared_endpoint_url()


def build_proxy(url: str, headers: dict):
    """A stdio MCP server whose entire tool surface is the remote endpoint's."""
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import StreamableHttpTransport

    backend = Client(StreamableHttpTransport(url=url, headers=headers))
    return FastMCP.as_proxy(backend, name=SHIM_NAME)


def main():
    """Entry point for the ``cao-mcp-stdio-bridge`` console script."""
    logging.basicConfig(
        # stderr, never stdout: stdout is the MCP framing channel and one stray
        # log line there corrupts the session.
        stream=sys.stderr,
        level=os.environ.get("CAO_LOG_LEVEL", "WARNING").upper(),
    )
    url = resolve_shared_endpoint_url()
    headers = build_forward_headers()
    logger.info("%s forwarding stdio to %s", SHIM_NAME, url)
    build_proxy(url, headers).run()


if __name__ == "__main__":  # pragma: no cover
    main()
