"""Scoped MCP access to one long-lived, simulation-only factory controller."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx2
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth import require_scopes
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_access_token
from pydantic import Field
from simulation import Identifier, World
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

LOGGER = logging.getLogger("factory")
VIEWER_HTML = Path(__file__).resolve().parent / "viewer.html"
# Pictures of the Open Duck Mini for the live view, rendered from its model:
# one for each team color, and one for a blocked robot.
SPRITES = Path(__file__).resolve().parent / "sprites"


def allowed_hosts() -> list[str] | None:
    """Extra Host names the controller accepts, from FACTORY_ALLOWED_HOSTS.

    The controller accepts only the loopback names (127.0.0.1, localhost, ::1)
    in the Host header, so that a web page cannot use DNS rebinding to read the
    live view. A port forward that rewrites Host to its own name needs that name
    here, for example FACTORY_ALLOWED_HOSTS=ide.example.com.
    """
    names = [name.strip() for name in os.environ.get("FACTORY_ALLOWED_HOSTS", "").split(",")]
    return [name for name in names if name] or None


def viewer_page() -> str:
    """The live view, with the robot pictures inlined, so that it loads nothing else."""
    sprites = {
        path.stem.removeprefix("robot-"): "data:image/png;base64,"
        + base64.b64encode(path.read_bytes()).decode("ascii")
        for path in sorted(SPRITES.glob("robot-*.png"))
    }
    return VIEWER_HTML.read_text(encoding="utf-8").replace("__ROBOT_SPRITES__", json.dumps(sprites))


# wait_team holds a tool call open for up to this time. The MCP client of a
# connect relay must wait longer than that.
MAX_WAIT_SECONDS = 45
CLIENT_TIMEOUT_SECONDS = 60
WAIT_POLL_SECONDS = 0.25
Count = Annotated[int, Field(ge=1, le=24)]
Seconds = Annotated[float, Field(gt=0, le=60)]
Move = Literal["spin", "turn", "run"]


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
            timeout=timeout if timeout is not None else httpx2.Timeout(CLIENT_TIMEOUT_SECONDS),
            auth=auth,
            trust_env=False,
            follow_redirects=False,
        )

    return Client(
        StreamableHttpTransport(url, auth=token, httpx_client_factory=local_http_client),
        timeout=CLIENT_TIMEOUT_SECONDS,
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
        "CAO hierarchical factory simulator",
        auth=StaticTokenVerifier(tokens, required_scopes=["observe"]),
        lifespan=lifespan,
    )

    # Read-only live view, served on the same 127.0.0.1 controller, as in the
    # cross-zone transport example. It exposes only observe(): the measured
    # poses, team states, parts, and command history that any observe-scoped
    # agent already sees, never a credential.
    @server.custom_route("/", methods=["GET"], include_in_schema=False)
    async def viewer(_request: Request) -> HTMLResponse:
        return HTMLResponse(viewer_page())

    @server.custom_route("/state", methods=["GET"], include_in_schema=False)
    async def state(_request: Request) -> JSONResponse:
        return JSONResponse(world.observe())

    def zone() -> str:
        token = get_access_token()
        actor = token.claims.get("zone") if token else None
        if not isinstance(actor, str) or actor not in world.scene.teams:
            raise AuthorizationError("a configured team manager credential is required")
        return actor

    def log_call(tool: str, **arguments) -> None:
        # One line for each tool call, so the operator sees which agent does what.
        token = get_access_token()
        claims = token.claims if token else {}
        if isinstance(claims.get("zone"), str):
            caller = f"{claims['zone']} team manager"
        else:
            caller = str(claims.get("role") or (token.client_id if token else "unknown"))
        details = ", ".join(f"{key}={value}" for key, value in arguments.items())
        LOGGER.info("%s called %s(%s)", caller, tool, details)

    @server.tool(auth=require_scopes("observe"), annotations={"readOnlyHint": True})
    def observe() -> dict:
        """Read the measured floor: every team, robot, buffer, part, and command.

        Positions are metres in this shared simulation; observed_at is UTC.
        For each team: status (idle, forming, ready, running, blocked,
        starved, done), cycle_seconds, the robots with their measured poses
        and states, and the program counters (output, target,
        avg_output_interval_seconds, blocked_seconds, starved_seconds).
        """
        log_call("observe")
        return world.observe()

    @server.tool(auth=require_scopes("act"))
    def form_team(command_id: Identifier, formation: Identifier) -> dict:
        """Drive every robot of your team to its slot of the named formation.

        Returns accepted, not arrival. The command finishes when every robot
        is at its slot; use wait_team or observe. Your team comes from your
        credential, not from text.
        """
        log_call("form_team", command_id=command_id, formation=formation)
        return world.form_team(zone(), command_id, formation)

    @server.tool(auth=require_scopes("act", "line"))
    def start_line(command_id: Identifier, parts: Count, cycle_seconds: Seconds) -> dict:
        """Start the line of a serial or parallel team for a number of parts.

        cycle_seconds is the work time of one robot on one part. The team must
        be in formation. The program runs on its own until its output reaches
        the target. Reuse a command_id only to reconcile the identical call.
        """
        log_call("start_line", command_id=command_id, parts=parts, cycle_seconds=cycle_seconds)
        return world.start_line(zone(), command_id, parts, cycle_seconds)

    @server.tool(auth=require_scopes("act", "ship"))
    def start_shipping(
        command_id: Identifier,
        units: Count,
        batch: Annotated[int, Field(ge=1, le=8)],
        routine: Annotated[list[Move], Field(min_length=1, max_length=6)],
    ) -> dict:
        """Start the shipping program of a formation team.

        The formation loads ``batch`` units from its input buffer, does the
        moves of ``routine`` in order (spin, turn, and exactly one run to the
        dock and back), and repeats until ``units`` units are at the dock.
        """
        log_call("start_shipping", command_id=command_id, units=units, batch=batch, routine=routine)
        return world.start_shipping(zone(), command_id, units, batch, list(routine))

    @server.tool(auth=require_scopes("act", "line"))
    def set_pace(command_id: Identifier, cycle_seconds: Seconds) -> dict:
        """Change the cycle time of your serial or parallel team.

        New work uses the new cycle time; work in progress keeps its timer.
        """
        log_call("set_pace", command_id=command_id, cycle_seconds=cycle_seconds)
        return world.set_pace(zone(), command_id, cycle_seconds)

    @server.tool(auth=require_scopes("act"), annotations={"readOnlyHint": True})
    async def wait_team(
        until: Literal["done", "done_or_blocked"] = "done_or_blocked",
        timeout_seconds: Annotated[int, Field(ge=1, le=MAX_WAIT_SECONDS)] = 30,
    ) -> dict:
        """Wait until your team's program is done or blocked, or until the timeout.

        Returns the event (done, blocked, formed, stopped, no_program, or
        timeout) and the status of your team. With until="done", a blocked
        team does not end the wait. A timeout is not a failure: call again.
        """
        actor = zone()
        log_call("wait_team", until=until, timeout_seconds=timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        threshold = world.scene.blocked_report_seconds
        while True:
            with world.lock:
                status = world.team_status(actor)
                state = world.teams[actor]
                stopped = world.stopped
                blocked_for = state.program.blocked_for if state.program else 0.0
            event = None
            if stopped:
                event = "stopped"
            elif status["status"] == "done":
                event = "done"
            elif status["status"] == "ready" and state.program is None:
                event = "formed"
            elif status["status"] == "idle":
                event = "no_program"
            elif until == "done_or_blocked" and blocked_for >= threshold:
                event = "blocked"
            elif time.monotonic() >= deadline:
                event = "timeout"
            if event:
                return {
                    "event": event,
                    "team": status,
                    "simulation_seconds": round(world.now(), 2),
                }
            await asyncio.sleep(WAIT_POLL_SECONDS)

    @server.tool(auth=require_scopes("operate"))
    def stop_simulation() -> dict:
        """Operator-only stop and permanent motion lockout for this run."""
        log_call("stop_simulation")
        state = world.stop()
        save_snapshot(snapshot, state)
        return state

    return server
