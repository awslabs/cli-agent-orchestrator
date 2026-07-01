"""CAO Fleet Panel — FastAPI aggregate + control API, serves the static UI."""
import asyncio
import os

import httpx
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import client, config

app = FastAPI(title="CAO Fleet Panel")

_STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


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
async def launch(name: str, body: dict = Body(default={})):
    m = _machine_or_404(name)
    base = config.base_url(m)
    agent = body.get("agent_profile") or "developer"
    provider = body.get("provider") or "claude_code"
    wd = body.get("working_directory")
    task = body.get("task")
    session_name = body.get("session_name") or ("cao-panel-" + os.urandom(3).hex())
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
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            return await client.get_session(c, base, session_name)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")


@app.post("/api/machines/{name}/sessions/{session_name}/send")
async def send(name: str, session_name: str, body: dict = Body(default={})):
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
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            return await client.shutdown(c, base, session_name)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")


@app.get("/api/machines/{name}/terminals/{terminal_id}/output")
async def terminal_output(name: str, terminal_id: str, mode: str = "last"):
    m = _machine_or_404(name)
    base = config.base_url(m)
    async with httpx.AsyncClient(timeout=client.TIMEOUT) as c:
        try:
            return await client.terminal_output(c, base, terminal_id, mode)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name}: {exc}")


@app.get("/api/machines/{name}/terminals/{terminal_id}/screen")
async def terminal_screen(name: str, terminal_id: str, ansi: bool = True):
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
async def terminal_key(name: str, terminal_id: str, body: dict = Body(default={})):
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
async def terminal_input(name: str, terminal_id: str, body: dict = Body(default={})):
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
