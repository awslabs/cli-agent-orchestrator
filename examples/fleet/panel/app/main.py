"""CAO Fleet Panel — FastAPI aggregate + control API, serves CAO's own dashboard."""
import asyncio
import base64
import binascii
import hmac
import os
import re

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import client, config

app = FastAPI(title="CAO Fleet Panel")

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
    if not _SAFE_SEGMENT.match(value or ""):
        raise HTTPException(status_code=400, detail=f"invalid {kind}")
    return value


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
    return await call_next(request)


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

    return {"machines": await asyncio.gather(*[probe(m) for m in machines])}


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
