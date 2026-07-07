"""CAO Fleet Panel — FastAPI aggregate + control API, serves the static UI."""
import asyncio
import base64
import binascii
import hmac
import os
import re

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import client, config

app = FastAPI(title="CAO Fleet Panel")

_STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

# cao-server session names / terminal ids are interpolated into upstream request
# paths; keep them to a safe charset so a crafted value can't traverse to another
# endpoint on the (already-trusted) node.
_SAFE_SEGMENT = re.compile(r"\A[A-Za-z0-9._-]+\Z")


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


app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(_STATIC, "index.html"))


def run():
    import uvicorn
    uvicorn.run(app, host=config.PANEL_HOST, port=config.PANEL_PORT)
