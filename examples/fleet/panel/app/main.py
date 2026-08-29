"""CAO Fleet Panel — FastAPI aggregate + control API, serves CAO's own dashboard."""
import asyncio
import base64
import binascii
import hmac
import os
import re

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import websockets
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import client, config


@asynccontextmanager
async def lifespan(_app):
    """Keep the registry snapshot fresh when it comes from a ConfigMap.

    The first read is awaited BEFORE the panel serves anything, so the readiness
    probe's own `/api/fleet` call is already answering from the API server rather
    than from whatever happened to be mounted. It is not fatal if it fails —
    `load_machines()` falls back to the file, and the task below keeps trying —
    because a panel that is up and one sync period stale is worth more than a pod
    that CrashLoops while an operator fixes an RBAC binding.
    """
    if not config.FLEET_CONFIGMAP:
        yield
        return
    error = await config.refresh_configmap()
    if error:
        print(f"[panel] fleet ConfigMap unavailable, falling back to {config.FLEET_CONFIG}: {error}")
    task = asyncio.create_task(config.watch_configmap())
    try:
        yield
    finally:
        task.cancel()
        # Awaited, not just cancelled: an un-awaited cancelled task logs
        # "Task exception was never retrieved" on the way out, which reads as a
        # shutdown fault in the pod's last lines of log.
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="CAO Fleet Panel", lifespan=lifespan)

_ROOT = os.path.dirname(os.path.dirname(__file__))

# Two possible front ends, resolved once at import.
#
# `web_ui/` is CAO's own dashboard (repository `web/`), built with
# `--base=/proxy/panel/` and copied in beside `app/` by Dockerfile.panel. It is
# the same UI a single cao-server serves, so a fleet is operated with the tool
# people already know instead of a second, thinner one.
#
# `static/` is the panel's original hand-written UI. Kept as the fallback
# because `examples/fleet/panel` is runnable straight from a checkout, where
# nothing has been built and a hard dependency on `web_ui/` would serve 404 for
# every asset — the same silent-omission trap cao-server avoids by mounting its
# own bundle only when `index.html` is actually on disk.
_WEB_UI = os.path.join(_ROOT, "web_ui")
_STATIC = os.path.join(_ROOT, "static")
_UI_ROOT = _WEB_UI if os.path.isfile(os.path.join(_WEB_UI, "index.html")) else _STATIC

# Root-level files the front end asks for by name (favicons today). Narrow on
# purpose: the route that serves these is a single path segment, so it must not
# become a way to read arbitrary files out of the image.
_ROOT_ASSET = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]*\.[A-Za-z0-9]{1,8}\Z")

# cao-server session names / terminal ids are interpolated into upstream request
# paths; keep them to a safe charset so a crafted value can't traverse to another
# endpoint on the (already-trusted) node.
_SAFE_SEGMENT = re.compile(r"\A[A-Za-z0-9._-]+\Z")

# --- node pass-through ------------------------------------------------------
#
# The dashboard the panel serves is cao-server's own, so it asks for cao-server
# paths (`/sessions`, `/terminals/{id}/screen`, ...). `/nodes/{name}/<path>`
# forwards one of those to the named node, which is what lets one page drive a
# whole fleet: the browser picks a node, every request carries it, and the panel
# resolves it against the registry.
#
# Whitelisted by FIRST PATH SEGMENT rather than by full path. cao-server has
# ~100 routes and the dashboard already uses eight namespaces; enumerating every
# one would mean editing the panel each time the dashboard gains a call, which is
# the coupling this whole change exists to remove. Segments are stable, and the
# line they draw is the one worth drawing:
#
#   - `internal/` is the agent-facing memory RPC (an in-process convenience for
#     tooling on the node), never a browser call. Left out.
#   - `agui/` and `events` are the AG-UI transport; the dashboard does not use
#     them, so they stay unreachable until something does.
#   - `.well-known/` describes the node's own OAuth resource. Meaningless when
#     read through the panel.
#
# Adding a namespace is a one-line change here.
_NODE_API_PREFIXES = frozenset({
    "agents", "flows", "graph", "health", "memory",
    "sessions", "settings", "skills", "terminals", "workflows",
})

# Request headers forwarded upstream. An allowlist, not a denylist, because the
# one header that must NOT travel is `Authorization`: it carries the PANEL's
# shared token, which is not the node's credential and has no business leaving
# this process. `Cookie` and `Host` are dropped for the same reason.
_FORWARD_REQUEST_HEADERS = frozenset({
    "accept", "accept-encoding", "accept-language", "cache-control",
    "content-type", "last-event-id",
})

# Response headers dropped on the way back. Hop-by-hop headers describe the
# panel<->node connection, not this one, and uvicorn writes its own `date` and
# `server`. Everything else (content-type, content-encoding, content-length,
# cache-control, ...) is passed through untouched, so a raw byte stream stays
# consistent with the headers that describe it.
_DROP_RESPONSE_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "date", "server",
})

# A stream has no idea when it will next produce a byte, so it gets no read
# timeout. Everything else keeps the fleet-wide 8s ceiling.
_STREAM_TIMEOUT = httpx.Timeout(30.0, connect=4.0, read=None)


def _safe_segment(value, kind):
    # `.` and `..` are excluded by name rather than by charset: dots are legal in
    # a session name, so the pattern must allow them, but a bare `..` arriving
    # percent-encoded (`%2e%2e`, which nothing on the way in normalizes) would be
    # interpolated straight into an upstream path and resolve to a DIFFERENT
    # endpoint on the node.
    if value in (".", "..") or not _SAFE_SEGMENT.match(value or ""):
        raise HTTPException(status_code=400, detail=f"invalid {kind}")
    return value


# The terminal socket's credential. Set by the middleware below on any
# already-authenticated HTTP response, and accepted on nothing but the WebSocket
# route — see both for why the socket needs a cookie at all.
_WS_COOKIE = "cao_panel_ws"


def _token_ok(header):
    """True when the Authorization header carries the configured shared token."""
    token = config.PANEL_TOKEN
    if not header:
        return False
    scheme, _, value = header.partition(" ")
    scheme = scheme.lower()
    if scheme == "bearer":
        presented = value.strip()
    elif scheme == "basic":
        try:
            presented = base64.b64decode(value.strip()).decode("utf-8").partition(":")[2]
        except (binascii.Error, UnicodeDecodeError):
            return False
    else:
        return False
    return hmac.compare_digest(presented, token)


@app.middleware("http")
async def _require_token(request: Request, call_next):
    # Opt-in: only enforced when CAO_PANEL_TOKEN is set. Guards the whole origin
    # (page + static + API) so a browser prompts once and reuses the credential.
    if config.PANEL_TOKEN and not _token_ok(request.headers.get("authorization")):
        return JSONResponse(
            {"detail": "authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="CAO Fleet Panel"'},
        )
    response = await call_next(request)
    if config.PANEL_TOKEN and request.cookies.get(_WS_COOKIE) != config.PANEL_TOKEN:
        # A browser cannot set a request header on a WebSocket handshake, so the
        # terminal socket cannot present the Authorization header this middleware
        # demands — and ASGI does not run HTTP middleware for a WebSocket scope
        # at all, so the socket would otherwise be the one unguarded route on the
        # origin. Hand the already-authenticated browser a credential it WILL
        # send on a same-origin handshake.
        #
        # HttpOnly so script cannot read it, SameSite=strict so it is not sent
        # from another site at all, and accepted ONLY on the WebSocket route
        # (never in place of the header above), which keeps it off every
        # state-changing HTTP path and out of CSRF reach.
        response.set_cookie(
            _WS_COOKIE,
            config.PANEL_TOKEN,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
    return response


def _machine_or_404(name):
    for m in config.load_machines():
        if m["name"] == name:
            return m
    raise HTTPException(status_code=404, detail=f"unknown machine '{name}'")


@app.get("/api/fleet")
async def fleet():
    machines = config.load_machines()

    async def probe(m):
        base = config.base_url(m)
        entry = {
            "name": m["name"], "label": m["label"], "host": m["host"],
            "role": m.get("role"), "online": False, "claude": None, "sessions": [],
        }
        async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
            try:
                h = await client.health(c, base)
                entry["online"] = True
                entry["claude"] = (h.get("components") or {}).get("claude")
                entry["sessions"] = await client.list_sessions(c, base)
            except Exception as exc:  # offline / unreachable — isolate
                entry["error"] = type(exc).__name__
        return entry

    # `source` is additive — every existing client reads `machines` and ignores
    # the rest — and it is the only place a fallback to the mounted file is
    # visible. Without it, a panel whose ConfigMap read is being denied looks
    # identical to one whose workers are not registering.
    return {
        "machines": await asyncio.gather(*[probe(m) for m in machines]),
        "source": config.configmap_status(),
    }


@app.post("/api/machines/{name}/launch")
async def launch(name: str, body: dict = Body(default_factory=dict)):
    m = _machine_or_404(name)
    base = config.base_url(m)
    agent = body.get("agent_profile") or "developer"
    provider = body.get("provider") or "claude_code"
    wd = body.get("working_directory")
    task = body.get("task")
    session_name = body.get("session_name") or ("fleet-panel-" + os.urandom(3).hex())
    _safe_segment(session_name, "session_name")
    async with httpx.AsyncClient(timeout=client.LAUNCH_TIMEOUT) as c:
        try:
            term = await client.launch(c, base, agent, provider, session_name, wd)
        except httpx.HTTPStatusError as exc:
            detail = (exc.response.text or "").strip() or str(exc)
            raise HTTPException(status_code=502, detail=f"{name} launch failed: {detail}")
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name} launch failed: {type(exc).__name__}: {exc}")
        tid = term.get("id")
        task_sent = False
        if task and tid:
            try:
                await client.send_message(c, base, tid, task)
                task_sent = True
            except httpx.HTTPError:
                task_sent = False
    return {"machine": name, "session_name": session_name, "terminal_id": tid, "task_sent": task_sent}


@app.get("/api/machines/{name}/sessions/{session_name}")
async def session_detail(name: str, session_name: str):
    _safe_segment(session_name, "session_name")
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            return await client.get_session(c, base, session_name)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")


@app.post("/api/machines/{name}/sessions/{session_name}/send")
async def send(name: str, session_name: str, body: dict = Body(default_factory=dict)):
    _safe_segment(session_name, "session_name")
    msg = body.get("message")
    if not msg:
        raise HTTPException(status_code=400, detail="message required")
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            detail = await client.get_session(c, base, session_name)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")
        terminals = detail.get("terminals") or []
        if not terminals:
            raise HTTPException(status_code=404, detail="no terminals in session")
        tid = terminals[0]["id"]
        try:
            await client.send_message(c, base, tid, msg)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")
    return {"machine": name, "session_name": session_name, "terminal_id": tid, "sent": True}


@app.post("/api/machines/{name}/sessions/{session_name}/shutdown")
async def shutdown(name: str, session_name: str):
    _safe_segment(session_name, "session_name")
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            return await client.shutdown(c, base, session_name)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")


@app.get("/api/machines/{name}/terminals/{terminal_id}/output")
async def terminal_output(name: str, terminal_id: str, mode: str = "last"):
    _safe_segment(terminal_id, "terminal_id")
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            return await client.terminal_output(c, base, terminal_id, mode)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")


@app.get("/api/machines/{name}/terminals/{terminal_id}/screen")
async def terminal_screen(name: str, terminal_id: str, ansi: bool = True):
    _safe_segment(terminal_id, "terminal_id")
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            return await client.get_screen(c, base, terminal_id, ansi=ansi)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # node has no /screen endpoint yet — degrade to plain-text tail
                out = await client.terminal_output(c, base, terminal_id, "full")
                return {"screen": out.get("output", ""), "ansi": False, "fallback": True}
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")


@app.post("/api/machines/{name}/terminals/{terminal_id}/key")
async def terminal_key(name: str, terminal_id: str, body: dict = Body(default_factory=dict)):
    _safe_segment(terminal_id, "terminal_id")
    key = body.get("key")
    if not key:
        raise HTTPException(status_code=400, detail="key required")
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            return await client.send_key(c, base, terminal_id, key)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")


@app.post("/api/machines/{name}/terminals/{terminal_id}/input")
async def terminal_input(name: str, terminal_id: str, body: dict = Body(default_factory=dict)):
    _safe_segment(terminal_id, "terminal_id")
    text = body.get("text")
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            return await client.send_input(c, base, terminal_id, text)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")


@app.get("/api/machines/{name}/providers")
async def machine_providers(name: str):
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            return await client.list_providers(c, base)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")


@app.get("/api/machines/{name}/profiles")
async def machine_profiles(name: str):
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            return await client.list_profiles(c, base)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")


@app.get("/api/machines/{name}/terminals/{terminal_id}/working-directory")
async def terminal_wd(name: str, terminal_id: str):
    _safe_segment(terminal_id, "terminal_id")
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            return await client.working_directory(c, base, terminal_id)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")


def _proxy_timeout(method, path, accept):
    if "text/event-stream" in accept:
        return _STREAM_TIMEOUT
    # POST /sessions blocks until the agent CLI reaches a ready prompt.
    if method == "POST" and path == "sessions":
        return client.LAUNCH_TIMEOUT
    return client.TIMEOUT


@app.api_route(
    "/nodes/{name}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def node_proxy(name: str, path: str, request: Request):
    """Forward one cao-server request to a registered node.

    Streams in both directions rather than buffering, so the workflow event
    stream (SSE, open for the life of a run) arrives frame by frame instead of
    at the end.
    """
    m = _machine_or_404(name)
    segments = path.split("/")
    if segments[0] not in _NODE_API_PREFIXES:
        raise HTTPException(status_code=404, detail=f"'{segments[0]}' is not proxied")
    if ".." in segments:
        # httpx leaves dot segments in the path; a downstream server may resolve
        # them, which would step outside the namespace just checked.
        raise HTTPException(status_code=400, detail="invalid path")

    accept = request.headers.get("accept", "")
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() in _FORWARD_REQUEST_HEADERS
    }
    body = await request.body()

    c = httpx.AsyncClient(timeout=_proxy_timeout(request.method, path, accept))
    try:
        upstream = await c.send(
            c.build_request(
                request.method,
                f"{config.base_url(m)}/{path}",
                params=request.url.query,
                content=body,
                headers=headers,
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await c.aclose()
        raise HTTPException(status_code=502, detail=f"{name}: {type(exc).__name__}: {exc}")

    async def _release():
        await upstream.aclose()
        await c.aclose()

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers={
            k: v for k, v in upstream.headers.items()
            if k.lower() not in _DROP_RESPONSE_HEADERS
        },
        background=BackgroundTask(_release),
    )


def _ws_authorized(ws):
    """Whether a terminal-socket handshake may open a PTY on a node.

    Two credentials, because there are two kinds of caller. A reverse proxy or a
    native client sets `Authorization` (nginx injecting it upstream is how the
    published deployment works). A browser cannot set a header on a handshake, so
    it presents the HttpOnly cookie `_require_token` gave it.
    """
    if not config.PANEL_TOKEN:
        return True
    if _token_ok(ws.headers.get("authorization")):
        return True
    return hmac.compare_digest(ws.cookies.get(_WS_COOKIE, ""), config.PANEL_TOKEN)


def _ws_same_origin(ws):
    """Whether the handshake is same-origin with the panel.

    Cross-site WebSocket hijacking (CWE-1385) guard, and the reason the cookie
    above is safe to accept: a WebSocket is not subject to the Same-Origin
    Policy, and CORS middleware never sees a WebSocket scope, so a page on any
    site the operator visits could otherwise open this socket and get keystroke
    injection on a node — RCE. Absent `Origin` means a non-browser caller, which
    had to present the header credential to get here.

    `X-Forwarded-Host` counts, and is safe to trust for exactly this check: the
    panel is normally published behind a reverse proxy, whose `Host` is the
    upstream it dialled (`127.0.0.1:9888`) and never the name in the browser's
    Origin — comparing against `Host` alone would reject every real terminal. It
    cannot be abused, because JavaScript cannot set ANY request header on a
    WebSocket handshake, so the attacker page this check exists to stop is the one
    caller that cannot send it.

    Host and port only, not scheme. A TLS-terminating proxy gives the panel plain
    HTTP, so the browser's `https://` Origin can never match the scheme the panel
    sees — comparing it would reject every deployment that has TLS, which is the
    only kind that should be reachable off a private network.
    """
    origin = ws.headers.get("origin")
    if not origin:
        return True
    allowed = {h for h in (ws.headers.get("x-forwarded-host"), ws.headers.get("host")) if h}
    return urlsplit(origin).netloc in allowed


@app.websocket("/nodes/{name}/terminals/{terminal_id}/ws")
async def node_terminal_ws(ws: WebSocket, name: str, terminal_id: str):
    """Bridge a browser's terminal socket to a node's cao-server.

    This is what makes the fleet view a terminal and not a screenshot: the
    dashboard's xterm.js attaches to a real PTY on the node, keystrokes and all.
    Frames are relayed verbatim in both directions — cao-server sends terminal
    bytes as binary and takes JSON text control messages, and neither is
    interpreted here.

    The upstream connection deliberately sends NO `Origin`. Forwarding the
    browser's would be worse than useless: the node compares it against ITS own
    Host, which a proxied origin never matches. A header-less handshake is what
    cao-server treats as a non-browser client, gated by its own
    `CAO_WS_ALLOWED_CLIENTS` IP allowlist, which must include the panel. The
    browser-side origin check above is the panel's own responsibility.
    """
    if not _ws_authorized(ws) or not _ws_same_origin(ws):
        # Close before accept: no PTY is ever attached to a rejected handshake.
        await ws.close(code=1008)
        return
    try:
        machine = _machine_or_404(name)
        _safe_segment(terminal_id, "terminal_id")
    except HTTPException:
        await ws.close(code=1008)
        return

    # The query string travels: cao-server reads `?token=` from it when ITS auth
    # layer is enabled, which is the node's credential, not the panel's.
    query = ws.scope.get("query_string", b"").decode("latin-1")
    upstream_url = (
        f"{config.ws_url(machine)}/terminals/{terminal_id}/ws" + (f"?{query}" if query else "")
    )
    try:
        # max_size=None: a terminal repaint after a resize can exceed the 1 MiB
        # default frame cap, and the library's answer to an oversized frame is to
        # drop the connection.
        upstream = await websockets.connect(upstream_url, max_size=None, open_timeout=8)
    except Exception as exc:
        # Accept-then-close, so the browser gets a close event it can read rather
        # than a failed handshake it cannot.
        #
        # "Refused" and "unreachable" are told apart because they are different
        # operator problems — a terminal that no longer exists versus a node that
        # is down — and because cao-server cannot tell them apart FOR us: it
        # closes 4003/4004/4401/4403 before accepting, which uvicorn turns into a
        # bare HTTP 403 on the handshake, so its close code never reaches the
        # wire. The status is all that survives, and it is worth relaying.
        rejected = isinstance(exc, websockets.exceptions.InvalidStatus)
        await ws.accept()
        await ws.close(
            code=1008 if rejected else 1011,
            reason=(f"node refused the attach (HTTP {exc.response.status_code})"
                    if rejected else "node unreachable"),
        )
        return

    await ws.accept()

    async def to_node():
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])

    async def to_browser():
        async for frame in upstream:
            if isinstance(frame, bytes):
                await ws.send_bytes(frame)
            else:
                await ws.send_text(frame)

    # Either side ending ends the bridge: a closed PTY should close the browser's
    # socket, and a closed tab should stop reading from the node.
    tasks = [asyncio.create_task(to_node()), asyncio.create_task(to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await upstream.close()
        try:
            await ws.close()
        except RuntimeError:
            pass  # already closed by the browser


# Mounted and declared AFTER every API route, so no front-end file can shadow
# one. `assets/` is what Vite emits; `static/` is the legacy UI's own tree and
# stays mounted either way, since it is where the fallback's assets live.
if os.path.isdir(os.path.join(_UI_ROOT, "assets")):
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(_UI_ROOT, "assets")),
        name="assets",
    )
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(_UI_ROOT, "index.html"))


@app.get("/{filename}")
async def root_asset(filename: str):
    """Serve a root-level front-end file, e.g. `favicon.svg`.

    Last route registered, so it cannot shadow `/api/...`. The name is matched
    against `_ROOT_ASSET` and then resolved and confined to `_UI_ROOT`, so a
    percent-encoded traversal reaches a 404 rather than the filesystem.
    """
    if not _ROOT_ASSET.match(filename):
        raise HTTPException(status_code=404, detail="not found")
    path = os.path.realpath(os.path.join(_UI_ROOT, filename))
    if os.path.dirname(path) != os.path.realpath(_UI_ROOT) or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)


def run():
    import uvicorn
    uvicorn.run(app, host=config.PANEL_HOST, port=config.PANEL_PORT)
