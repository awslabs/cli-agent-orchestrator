"""Scoped MCP access to one long-lived cell controller.

Each agent holds its own credential. The scopes decide which tools it can call,
and the ``robot`` claim decides which robot it can command: a tool argument or a
prompt cannot widen either.

* ``observe`` - every agent: read the measured state and command records.
* ``walk`` - the humanoid operator: ``walk_to`` with its own robot.
* ``pick`` - the arm operator: ``pick_and_place`` with its own robot.
* ``operate`` - the human operator: ``stop_cell``.

The same 127.0.0.1 controller also serves a read-only live view: ``GET /`` is
the page, ``GET /state`` is observe() plus the frame numbers of the pictures,
and ``GET /frame/<robot>`` is the latest picture of that robot's world. They
show only what an observe credential already sees, never a credential.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from cell import Cell, Identifier, Refused
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import AuthorizationError, ToolError
from fastmcp.server.auth import require_scopes
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_access_token
from live_view import LiveView
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

LOGGER = logging.getLogger("cell")
VIEWER_HTML = Path(__file__).resolve().parent / "viewer.html"


def allowed_hosts() -> list[str] | None:
    """Extra Host names the controller accepts, from CELL_ALLOWED_HOSTS.

    The controller accepts only the loopback names (127.0.0.1, localhost, ::1)
    in the Host header, so that a web page cannot use DNS rebinding to read the
    live view. A port forward that rewrites Host to its own name needs that name
    here, for example CELL_ALLOWED_HOSTS='*.cloudfront.net'.
    """
    names = [name.strip() for name in os.environ.get("CELL_ALLOWED_HOSTS", "").split(",")]
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
        # This connection must stay local: no proxy from the environment, no redirects.
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


def make_server(cell: Cell, tokens: dict[str, dict], snapshot: Path) -> FastMCP:
    view = LiveView(cell)

    @asynccontextmanager
    async def lifespan(_server):
        cell.start()
        view.start()
        try:
            yield {}
        finally:
            view.close()
            save_snapshot(snapshot, cell.close())

    server = FastMCP(
        "CAO policy-driven cell",
        auth=StaticTokenVerifier(tokens, required_scopes=["observe"]),
        lifespan=lifespan,
    )

    # Read-only live view on the same loopback controller. No credential: it
    # shows only measured state, the scene and pictures, which every observe
    # credential already reads, and it can neither command nor step a robot.
    @server.custom_route("/", methods=["GET"], include_in_schema=False)
    async def viewer(_request: Request) -> HTMLResponse:
        return HTMLResponse(
            VIEWER_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"}
        )

    @server.custom_route("/state", methods=["GET"], include_in_schema=False)
    async def state(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"cell": cell.observe(), "view": view.state()}, headers={"Cache-Control": "no-store"}
        )

    @server.custom_route("/frame/{robot}", methods=["GET"], include_in_schema=False)
    async def frame(request: Request) -> Response:
        robot = request.path_params["robot"]
        picture = view.jpeg(robot) if robot in view.worlds() else None
        if picture is None:
            return Response(status_code=404)
        return Response(picture, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    def caller() -> tuple[str, str | None]:
        token = get_access_token()
        if token is None:
            raise AuthorizationError("a credential is required")
        robot = token.claims.get("robot")
        return str(token.claims.get("role") or token.client_id), (
            robot if isinstance(robot, str) else None
        )

    def own_robot(requested: str) -> str:
        role, robot = caller()
        if robot is None or requested != robot:
            raise AuthorizationError(f"the {role} credential commands {robot!r}, not {requested!r}")
        return role

    def call(tool: str, run, **arguments: str) -> dict:
        role, _ = caller()
        LOGGER.info(
            "%s called %s(%s)", role, tool, ", ".join(f"{k}={v}" for k, v in arguments.items())
        )
        try:
            return run()
        except Refused as refused:
            raise ToolError(str(refused)) from refused

    @server.tool(auth=require_scopes("observe"), annotations={"readOnlyHint": True})
    def observe() -> dict:
        """Read the measured state of the cell and every command record.

        Positions are metres in the cell frame; observed_at is UTC. An accepted
        or running command is not a finished one. A succeeded command is
        succeeded only because the measured state says so: check the evidence.
        """
        return call("observe", cell.observe)

    @server.tool(auth=require_scopes("walk"))
    def walk_to(command_id: Identifier, robot: Identifier, location: Identifier) -> dict:
        """Ask your humanoid to walk to a named cell location with its locomotion policy.

        Returns accepted, not arrival: call observe until the command is
        succeeded or failed. Reuse a command_id only to re-read the identical
        call after a lost reply. Your robot comes from your credential.
        """
        own_robot(robot)
        return call(
            "walk_to",
            lambda: cell.walk_to(caller()[0], command_id, robot, location),
            command_id=command_id,
            robot=robot,
            location=location,
        )

    @server.tool(auth=require_scopes("pick"))
    def pick_and_place(
        command_id: Identifier, robot: Identifier, part: Identifier, bin: Identifier
    ) -> dict:
        """Ask your arm to move one part from the table into one bin.

        The cell refuses while any humanoid is inside the arm's exclusion
        radius, measured, not reported. Returns accepted, not placement: call
        observe until the command is succeeded or failed. The evidence says
        whether a grasp assist held the part.
        """
        own_robot(robot)
        return call(
            "pick_and_place",
            lambda: cell.pick_and_place(caller()[0], command_id, robot, part, bin),
            command_id=command_id,
            robot=robot,
            part=part,
            bin=bin,
        )

    @server.tool(auth=require_scopes("operate"))
    def stop_cell() -> dict:
        """Operator only: refuse every further command in this run."""
        state = call("stop_cell", cell.stop)
        save_snapshot(snapshot, state)
        return state

    return server
