"""Scoped MCP access to one long-lived, simulation-only controller."""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth import require_scopes
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_access_token
from simulation import Identifier, World
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

VIEWER_HTML = Path(__file__).resolve().parent / "viewer.html"

LOGGER = logging.getLogger("transport")


def allowed_hosts() -> list[str] | None:
    """Extra Host names the controller accepts, from TRANSPORT_ALLOWED_HOSTS.

    The controller accepts only the loopback names (127.0.0.1, localhost, ::1)
    in the Host header, so that a web page cannot use DNS rebinding to read the
    live view. A port forward that rewrites Host to its own name needs that name
    here, for example TRANSPORT_ALLOWED_HOSTS='*.cloudfront.net'.
    """
    names = [name.strip() for name in os.environ.get("TRANSPORT_ALLOWED_HOSTS", "").split(",")]
    return [name for name in names if name] or None


def save_snapshot(path: Path, state: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(state, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def controller_client(url: str, token: str) -> Client:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.path != "/mcp"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("the example only connects to http://127.0.0.1:PORT/mcp")

    def local_http_client(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        *,
        follow_redirects: bool = False,
    ) -> httpx2.AsyncClient:
        # FastMCP requests redirects by default; this connection must remain local.
        return httpx2.AsyncClient(
            headers=headers,
            timeout=timeout if timeout is not None else httpx2.Timeout(20),
            auth=auth,
            trust_env=False,
            follow_redirects=False,
        )

    return Client(
        StreamableHttpTransport(url, auth=token, httpx_client_factory=local_http_client),
        timeout=20,
    )


def make_server(world: World, tokens: dict[str, dict], snapshot: Path) -> FastMCP:
    @asynccontextmanager
    async def lifespan(_server):
        world.start()
        try:
            yield {}
        finally:
            save_snapshot(snapshot, world.close())

    server = FastMCP(
        "CAO cross-zone transport simulator",
        auth=StaticTokenVerifier(tokens, required_scopes=["observe"]),
        lifespan=lifespan,
    )

    # Read-only live view, served on the same 127.0.0.1 controller.
    # It exposes only observe() - the measured poses, custody, and command
    # history that any observe-scoped agent already sees - never a credential.
    # Loopback-bound, so it is reachable only from this machine (in the
    # workshop, through the Code Editor's own port forwarding).
    @server.custom_route("/", methods=["GET"], include_in_schema=False)
    async def viewer(_request: Request) -> HTMLResponse:
        return HTMLResponse(VIEWER_HTML.read_text(encoding="utf-8"))

    @server.custom_route("/state", methods=["GET"], include_in_schema=False)
    async def state(_request: Request) -> JSONResponse:
        return JSONResponse(world.observe())

    def zone() -> str:
        token = get_access_token()
        actor = token.claims.get("zone") if token else None
        if not isinstance(actor, str) or actor not in world.scene.zones:
            raise AuthorizationError("a configured zone credential is required")
        return actor

    def log_call(tool: str, **arguments: str) -> None:
        # One line for each tool call, so the operator sees which agent does what.
        token = get_access_token()
        claims = token.claims if token else {}
        if isinstance(claims.get("zone"), str):
            caller = f"{claims['zone']} zone worker"
        else:
            caller = str(claims.get("role") or (token.client_id if token else "unknown"))
        details = ", ".join(f"{key}={value}" for key, value in arguments.items())
        LOGGER.info("%s called %s(%s)", caller, tool, details)

    @server.tool(auth=require_scopes("observe"), annotations={"readOnlyHint": True})
    def observe() -> dict:
        """Read current measured poses, custody, capabilities, and command outcomes.

        Positions are metres in this shared simulation; observed_at is UTC.
        An accepted/running command is not a completed transport. A missing
        command is unknown, not successful. Check poses as well as status.
        """
        log_call("observe")
        return world.observe()

    @server.tool(auth=require_scopes("act"))
    def move(
        command_id: Identifier,
        robot: Identifier,
        payload: Identifier,
        destination: Identifier,
    ) -> dict:
        """Request one bounded, rigid-carry simulated transport leg in your zone.

        The robot must already be at the payload. This returns accepted, not
        proof of arrival: use observe afterwards. Reuse the same command_id
        only to reconcile the identical call after a lost reply, never to
        request another motion. Your zone comes from authentication, not text.
        """
        log_call(
            "move", command_id=command_id, robot=robot, payload=payload, destination=destination
        )
        return world.move(zone(), command_id, robot, payload, destination)

    @server.tool(auth=require_scopes("act"))
    def offer_handoff(
        command_id: Identifier, payload: Identifier, receiver_zone: Identifier
    ) -> dict:
        """Offer custody only when the measured payload is at a shared dock.

        The sender retains custody until the receiving zone accepts this
        offer. An offered payload cannot move while the handoff is pending.
        """
        log_call(
            "offer_handoff", command_id=command_id, payload=payload, receiver_zone=receiver_zone
        )
        return world.offer(zone(), command_id, payload, receiver_zone)

    @server.tool(auth=require_scopes("act"))
    def accept_handoff(
        command_id: Identifier,
        payload: Identifier,
        robot: Identifier,
        offer_id: Identifier,
    ) -> dict:
        """Accept an explicit offer after rechecking both payload and receiver pose.

        Requires your zone's capable robot at the offered dock. Never call
        this on the strength of another CLI agent's completion message alone.
        """
        log_call(
            "accept_handoff", command_id=command_id, payload=payload, robot=robot, offer_id=offer_id
        )
        return world.accept(zone(), command_id, payload, robot, offer_id)

    @server.tool(auth=require_scopes("operate"))
    def stop_simulation() -> dict:
        """Operator-only stop and permanent motion lockout for this run."""
        log_call("stop_simulation")
        state = world.stop()
        save_snapshot(snapshot, state)
        return state

    return server
