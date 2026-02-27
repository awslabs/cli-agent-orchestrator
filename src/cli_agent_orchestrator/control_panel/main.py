"""Control Panel FastAPI server - middleware layer between frontend and cao-server."""

import logging
import os
import re
import secrets
import sqlite3
import subprocess
import asyncio
import json
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import requests
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from cli_agent_orchestrator.constants import (
    API_BASE_URL,
    DATABASE_FILE,
    DB_DIR,
    LOCAL_AGENT_STORE_DIR,
)

logger = logging.getLogger(__name__)

# Control panel server configuration
CONTROL_PANEL_HOST = os.getenv("CONTROL_PANEL_HOST", "localhost")
CONTROL_PANEL_PORT = int(os.getenv("CONTROL_PANEL_PORT", "8000"))

# CAO server URL (the actual backend)
CAO_SERVER_URL = os.getenv("CAO_SERVER_URL", API_BASE_URL)
CONSOLE_PASSWORD = os.getenv("CAO_CONSOLE_PASSWORD", "admin")
SESSION_COOKIE_NAME = "cao_console_session"
SESSION_TTL_SECONDS = int(os.getenv("CAO_CONSOLE_SESSION_TTL_SECONDS", "43200"))

# CORS origins for frontend
CONTROL_PANEL_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

BUILTIN_AGENT_PROFILES = ("code_supervisor", "developer", "reviewer")

app = FastAPI(
    title="CAO Control Panel API",
    description="FastAPI interface layer for the CAO frontend control panel",
    version="1.0.0",
)

if CONSOLE_PASSWORD == "admin":
    logger.warning(
        "CAO_CONSOLE_PASSWORD not set. Using insecure default password 'admin'. "
        "Set CAO_CONSOLE_PASSWORD in production."
    )

_service_started_at = datetime.now(timezone.utc)
_sessions: Dict[str, float] = {}


def _init_organization_db() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DATABASE_FILE)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS org_teams (
                leader_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS org_worker_links (
                worker_id TEXT PRIMARY KEY,
                leader_id TEXT NOT NULL,
                linked_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _register_team(leader_id: str) -> None:
    with sqlite3.connect(str(DATABASE_FILE)) as conn:
        conn.execute(
            """
            INSERT INTO org_teams (leader_id, created_at)
            VALUES (?, ?)
            ON CONFLICT(leader_id) DO NOTHING
            """,
            (leader_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def _set_worker_link(worker_id: str, leader_id: str) -> None:
    with sqlite3.connect(str(DATABASE_FILE)) as conn:
        conn.execute(
            """
            INSERT INTO org_worker_links (worker_id, leader_id, linked_at)
            VALUES (?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                leader_id=excluded.leader_id,
                linked_at=excluded.linked_at
            """,
            (worker_id, leader_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def _remove_worker_link(worker_id: str) -> None:
    with sqlite3.connect(str(DATABASE_FILE)) as conn:
        conn.execute("DELETE FROM org_worker_links WHERE worker_id = ?", (worker_id,))
        conn.commit()


def _list_worker_links() -> Dict[str, str]:
    with sqlite3.connect(str(DATABASE_FILE)) as conn:
        rows = conn.execute("SELECT worker_id, leader_id FROM org_worker_links").fetchall()
    return {str(worker_id): str(leader_id) for worker_id, leader_id in rows}


def _list_teams() -> set[str]:
    with sqlite3.connect(str(DATABASE_FILE)) as conn:
        rows = conn.execute("SELECT leader_id FROM org_teams").fetchall()
    return {str(leader_id) for (leader_id,) in rows}


def _list_available_agent_profiles() -> List[str]:
    names: set[str] = set(BUILTIN_AGENT_PROFILES)

    try:
        builtin_store = resources.files("cli_agent_orchestrator.agent_store")
        for child in builtin_store.iterdir():
            child_name = str(child.name)
            if child_name.endswith(".md"):
                names.add(Path(child_name).stem)
    except Exception as exc:
        logger.warning("Failed to list built-in agent profiles: %s", exc)

    try:
        if LOCAL_AGENT_STORE_DIR.exists():
            for child in LOCAL_AGENT_STORE_DIR.iterdir():
                if child.is_file() and child.suffix == ".md":
                    names.add(child.stem)
    except Exception as exc:
        logger.warning("Failed to list local agent profiles: %s", exc)

    return sorted(names)


def _create_local_agent_profile(
    name: str,
    description: str,
    system_prompt: str,
    provider: Optional[str],
) -> Path:
    normalized_name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", normalized_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid profile name. Use letters, numbers, underscore, or hyphen.",
        )

    LOCAL_AGENT_STORE_DIR.mkdir(parents=True, exist_ok=True)
    profile_path = LOCAL_AGENT_STORE_DIR / f"{normalized_name}.md"

    if profile_path.exists():
        raise HTTPException(status_code=409, detail="Agent profile already exists")

    frontmatter_lines = [
        "---",
        f"name: {normalized_name}",
        f"description: {description.strip()}",
    ]
    if provider and provider.strip():
        frontmatter_lines.append(f"provider: {provider.strip()}")
    frontmatter_lines.append("---")

    content = "\n".join(frontmatter_lines) + "\n\n" + system_prompt.strip() + "\n"
    profile_path.write_text(content, encoding="utf-8")

    return profile_path


def _validate_profile_name(profile_name: str) -> str:
    normalized_name = profile_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", normalized_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid profile name. Use letters, numbers, underscore, or hyphen.",
        )
    return normalized_name


def _profile_file_path(profile_name: str) -> Path:
    normalized_name = _validate_profile_name(profile_name)
    return LOCAL_AGENT_STORE_DIR / f"{normalized_name}.md"


_init_organization_db()


class LoginRequest(BaseModel):
    password: str = Field(min_length=1)


class AgentMessageRequest(BaseModel):
    message: str = Field(min_length=1)


class InboxMessageRequest(BaseModel):
    message: str = Field(min_length=1)
    sender_id: Optional[str] = None


class OrgLinkRequest(BaseModel):
    worker_id: str = Field(min_length=1)
    leader_id: Optional[str] = None


class OrgCreateRequest(BaseModel):
    role_type: Literal["main", "worker"]
    agent_profile: str = Field(min_length=1)
    provider: Optional[str] = None
    leader_id: Optional[str] = None
    working_directory: Optional[str] = None


class AgentProfileCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    provider: Optional[str] = None


class AgentProfileUpdateRequest(BaseModel):
    content: str = Field(min_length=1)


def _cleanup_expired_sessions() -> None:
    now = time.time()
    expired_tokens = [token for token, expires_at in _sessions.items() if expires_at <= now]
    for token in expired_tokens:
        _sessions.pop(token, None)


def _session_expires_at(token: str) -> Optional[float]:
    _cleanup_expired_sessions()
    return _sessions.get(token)


def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    return _session_expires_at(token) is not None


def _create_session() -> str:
    _cleanup_expired_sessions()
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL_SECONDS
    return token


def _build_cookie_response(payload: Dict[str, Any], token: Optional[str]) -> JSONResponse:
    response = JSONResponse(payload)
    if token is None:
        response.delete_cookie(SESSION_COOKIE_NAME, samesite="lax")
        return response

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


def _request_cao(
    method: str,
    path: str,
    params: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> requests.Response:
    url = f"{CAO_SERVER_URL}{path}"
    response = requests.request(method=method, url=url, params=params, json=json_body, timeout=30)
    response.raise_for_status()
    return response


def _response_json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _get_terminals_from_sessions() -> list[Dict[str, Any]]:
    sessions_response = _request_cao("GET", "/sessions")
    sessions_data = _response_json_or_text(sessions_response)
    if not isinstance(sessions_data, list):
        return []

    terminals: list[Dict[str, Any]] = []
    for session in sessions_data:
        session_name = session.get("name")
        if not session_name:
            continue
        try:
            terminals_response = _request_cao("GET", f"/sessions/{session_name}/terminals")
            session_terminals = _response_json_or_text(terminals_response)
            if isinstance(session_terminals, list):
                terminals.extend(session_terminals)
        except Exception as exc:
            logger.warning("Failed to fetch terminals for session %s: %s", session_name, exc)

    enriched_terminals: list[Dict[str, Any]] = []
    for terminal in terminals:
        terminal_id = terminal.get("id")
        terminal_info = dict(terminal)
        if terminal_id:
            try:
                details_response = _request_cao("GET", f"/terminals/{terminal_id}")
                details_data = _response_json_or_text(details_response)
                if isinstance(details_data, dict):
                    terminal_info.update(details_data)
            except Exception as exc:
                logger.warning("Failed to fetch terminal details %s: %s", terminal_id, exc)

        profile = str(terminal_info.get("agent_profile", "")).lower()
        terminal_info["is_main"] = "supervisor" in profile
        enriched_terminals.append(terminal_info)

    return enriched_terminals


def _resolve_sender_id(receiver_id: str) -> Optional[str]:
    try:
        receiver_response = _request_cao("GET", f"/terminals/{receiver_id}")
        receiver_terminal = _response_json_or_text(receiver_response)
        if not isinstance(receiver_terminal, dict):
            return None
        session_name = receiver_terminal.get("session_name")
        if not session_name:
            return None

        terminals_response = _request_cao("GET", f"/sessions/{session_name}/terminals")
        terminals = _response_json_or_text(terminals_response)
        if not isinstance(terminals, list):
            return None

        for terminal in terminals:
            terminal_id = terminal.get("id")
            profile = str(terminal.get("agent_profile", "")).lower()
            if terminal_id and terminal_id != receiver_id and "supervisor" in profile:
                return terminal_id

        for terminal in terminals:
            terminal_id = terminal.get("id")
            if terminal_id and terminal_id != receiver_id:
                return terminal_id
    except Exception as exc:
        logger.warning("Failed to resolve sender for receiver %s: %s", receiver_id, exc)

    return None


def _build_organization(terminals: List[Dict[str, Any]]) -> Dict[str, Any]:
    terminals_by_id = {
        terminal["id"]: terminal for terminal in terminals if isinstance(terminal.get("id"), str)
    }
    teams_from_db = _list_teams()

    leaders = [terminal for terminal in terminals if terminal.get("is_main")]
    leader_ids = {str(terminal.get("id", "")) for terminal in leaders}
    for leader_id in teams_from_db:
        if leader_id in leader_ids:
            continue
        team_leader = terminals_by_id.get(leader_id)
        if not team_leader:
            continue
        team_leader_copy = dict(team_leader)
        team_leader_copy["is_main"] = True
        team_leader_copy["team_type"] = "independent_worker_team"
        leaders.append(team_leader_copy)
        leader_ids.add(leader_id)

    workers = [
        terminal
        for terminal in terminals
        if str(terminal.get("id", "")) not in leader_ids and not terminal.get("is_main")
    ]
    links_from_db = _list_worker_links()

    for leader in leaders:
        leader_id = str(leader.get("id", ""))
        if leader_id:
            _register_team(leader_id)

    inferred_worker_to_leader: Dict[str, str] = {}
    leaders_by_session: Dict[str, List[str]] = {}
    for leader in leaders:
        session_name = str(leader.get("session_name", ""))
        leader_id = str(leader.get("id", ""))
        if session_name and leader_id:
            leaders_by_session.setdefault(session_name, []).append(leader_id)

    for worker in workers:
        worker_id = str(worker.get("id", ""))
        if not worker_id:
            continue

        if worker_id in links_from_db:
            linked_leader = links_from_db[worker_id]
            if linked_leader in terminals_by_id:
                inferred_worker_to_leader[worker_id] = linked_leader
            continue

        session_name = str(worker.get("session_name", ""))
        session_leaders = leaders_by_session.get(session_name, [])
        if len(session_leaders) == 1:
            leader_id = session_leaders[0]
            inferred_worker_to_leader[worker_id] = leader_id
            _set_worker_link(worker_id, leader_id)

    members_by_leader: Dict[str, List[Dict[str, Any]]] = {}
    for worker in workers:
        worker_id = str(worker.get("id", ""))
        leader_id = inferred_worker_to_leader.get(worker_id)
        if leader_id:
            worker["leader_id"] = leader_id
            members_by_leader.setdefault(leader_id, []).append(worker)

    leader_groups: List[Dict[str, Any]] = []
    for leader in leaders:
        leader_id = str(leader.get("id", ""))
        leader_groups.append(
            {
                "leader": leader,
                "members": sorted(
                    members_by_leader.get(leader_id, []),
                    key=lambda item: str(item.get("last_active", "")),
                    reverse=True,
                ),
            }
        )

    assigned_worker_ids = set(inferred_worker_to_leader.keys())
    unassigned_workers = [
        worker for worker in workers if str(worker.get("id", "")) not in assigned_worker_ids
    ]

    return {
        "leaders": leaders,
        "workers": workers,
        "leader_groups": leader_groups,
        "unassigned_workers": sorted(
            unassigned_workers,
            key=lambda item: str(item.get("last_active", "")),
            reverse=True,
        ),
    }

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CONTROL_PANEL_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path == "/health" or path.startswith("/auth/"):
        return await call_next(request)

    if not _is_authenticated(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    return await call_next(request)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint for the control panel."""
    try:
        # Also check if cao-server is reachable
        response = requests.get(f"{CAO_SERVER_URL}/health", timeout=5)
        cao_status = "healthy" if response.status_code == 200 else "unhealthy"
    except Exception:
        cao_status = "unreachable"

    return {
        "status": "healthy",
        "cao_server_status": cao_status,
    }


@app.post("/auth/login")
async def login(payload: LoginRequest) -> JSONResponse:
    if payload.password != CONSOLE_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid password")

    token = _create_session()
    return _build_cookie_response({"ok": True}, token)


@app.post("/auth/logout")
async def logout(request: Request) -> JSONResponse:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        _sessions.pop(token, None)
    return _build_cookie_response({"ok": True}, None)


@app.get("/auth/me")
async def me(request: Request) -> Dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    expires_at = _session_expires_at(token) if token else None
    return {
        "authenticated": expires_at is not None,
        "session_expires_at": int(expires_at) if expires_at else None,
    }


@app.get("/console/overview")
async def console_overview() -> Dict[str, Any]:
    try:
        terminals = _get_terminals_from_sessions()
        provider_counts = Counter(str(t.get("provider", "unknown")) for t in terminals)
        status_counts = Counter(str(t.get("status", "unknown")) for t in terminals)
        profile_counts = Counter(str(t.get("agent_profile", "unknown")) for t in terminals)
        main_agents = [t for t in terminals if t.get("is_main")]
        uptime_seconds = int((datetime.now(timezone.utc) - _service_started_at).total_seconds())

        return {
            "uptime_seconds": uptime_seconds,
            "agents_total": len(terminals),
            "main_agents_total": len(main_agents),
            "worker_agents_total": len(terminals) - len(main_agents),
            "provider_counts": dict(provider_counts),
            "status_counts": dict(status_counts),
            "profile_counts": dict(profile_counts),
            "main_agents": main_agents,
        }
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch CAO data: {exc}")


@app.get("/console/agents")
async def console_agents() -> Dict[str, Any]:
    try:
        terminals = _get_terminals_from_sessions()
        terminals_sorted = sorted(
            terminals,
            key=lambda item: str(item.get("last_active", "")),
            reverse=True,
        )
        return {"agents": terminals_sorted}
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch agents: {exc}")


@app.get("/console/organization")
async def console_organization() -> Dict[str, Any]:
    try:
        terminals = _get_terminals_from_sessions()
        organization = _build_organization(terminals)
        return {
            "leaders_total": len(organization["leaders"]),
            "workers_total": len(organization["workers"]),
            **organization,
        }
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch organization: {exc}")


@app.get("/console/agent-profiles")
async def console_agent_profiles() -> Dict[str, Any]:
    return {"profiles": _list_available_agent_profiles()}


@app.post("/console/agent-profiles")
async def console_create_agent_profile(payload: AgentProfileCreateRequest) -> Dict[str, Any]:
    created_path = _create_local_agent_profile(
        name=payload.name,
        description=payload.description,
        system_prompt=payload.system_prompt,
        provider=payload.provider,
    )
    return {
        "ok": True,
        "profile": payload.name.strip(),
        "file_path": str(created_path),
    }


@app.get("/console/agent-profiles/{profile_name}")
async def console_get_agent_profile(profile_name: str) -> Dict[str, Any]:
    profile_path = _profile_file_path(profile_name)
    if not profile_path.exists():
        raise HTTPException(status_code=404, detail="Agent profile not found")

    return {
        "profile": profile_name,
        "file_path": str(profile_path),
        "content": profile_path.read_text(encoding="utf-8"),
    }


@app.put("/console/agent-profiles/{profile_name}")
async def console_update_agent_profile(
    profile_name: str,
    payload: AgentProfileUpdateRequest,
) -> Dict[str, Any]:
    profile_path = _profile_file_path(profile_name)
    if not profile_path.exists():
        raise HTTPException(status_code=404, detail="Agent profile not found")

    profile_path.write_text(payload.content, encoding="utf-8")
    return {"ok": True, "profile": profile_name, "file_path": str(profile_path)}


@app.post("/console/agent-profiles/{profile_name}/install")
async def console_install_agent_profile(profile_name: str) -> Dict[str, Any]:
    profile_path = _profile_file_path(profile_name)
    if not profile_path.exists():
        raise HTTPException(status_code=404, detail="Agent profile not found")

    try:
        process = subprocess.run(
            ["uv", "run", "cao", "install", str(profile_path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to execute install command: {exc}")

    return {
        "ok": process.returncode == 0,
        "profile": profile_name,
        "command": f"uv run cao install {profile_path}",
        "return_code": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }


@app.post("/console/organization/link")
async def console_link_worker(payload: OrgLinkRequest) -> Dict[str, Any]:
    worker_id = payload.worker_id.strip()
    leader_id = payload.leader_id.strip() if payload.leader_id else None

    try:
        worker_response = _request_cao("GET", f"/terminals/{worker_id}")
        worker_terminal = _response_json_or_text(worker_response)
        if not isinstance(worker_terminal, dict):
            raise HTTPException(status_code=400, detail="Invalid worker terminal")
        worker_profile = str(worker_terminal.get("agent_profile", "")).lower()
        if "supervisor" in worker_profile:
            raise HTTPException(status_code=400, detail="worker_id cannot be a main agent")

        if leader_id:
            leader_response = _request_cao("GET", f"/terminals/{leader_id}")
            leader_terminal = _response_json_or_text(leader_response)
            if not isinstance(leader_terminal, dict):
                raise HTTPException(status_code=400, detail="Invalid leader terminal")
            leader_profile = str(leader_terminal.get("agent_profile", "")).lower()
            if "supervisor" not in leader_profile:
                raise HTTPException(status_code=400, detail="leader_id must be a main agent")
            _register_team(leader_id)
            _set_worker_link(worker_id, leader_id)
        else:
            _remove_worker_link(worker_id)

        return {"ok": True, "worker_id": worker_id, "leader_id": leader_id}
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to link organization: {exc}")


@app.post("/console/organization/create")
async def console_create_org_agent(payload: OrgCreateRequest) -> Dict[str, Any]:
    params: Dict[str, str] = {"agent_profile": payload.agent_profile}
    if payload.provider:
        params["provider"] = payload.provider
    if payload.working_directory:
        params["working_directory"] = payload.working_directory

    try:
        if payload.role_type == "main":
            created_response = _request_cao("POST", "/sessions", params=params)
            created_agent = _response_json_or_text(created_response)
            if isinstance(created_agent, dict) and isinstance(created_agent.get("id"), str):
                _register_team(created_agent["id"])
            return {
                "ok": True,
                "role_type": payload.role_type,
                "leader_id": None,
                "agent": created_agent,
            }

        if payload.leader_id:
            leader_response = _request_cao("GET", f"/terminals/{payload.leader_id}")
            leader_terminal = _response_json_or_text(leader_response)
            if not isinstance(leader_terminal, dict):
                raise HTTPException(status_code=400, detail="Invalid leader_id")
            session_name = leader_terminal.get("session_name")
            if not session_name:
                raise HTTPException(status_code=400, detail="leader has no session")

            created_response = _request_cao(
                "POST",
                f"/sessions/{session_name}/terminals",
                params=params,
            )
            created_agent = _response_json_or_text(created_response)
            if isinstance(created_agent, dict) and isinstance(created_agent.get("id"), str):
                _register_team(payload.leader_id)
                _set_worker_link(created_agent["id"], payload.leader_id)
            return {
                "ok": True,
                "role_type": payload.role_type,
                "leader_id": payload.leader_id,
                "agent": created_agent,
            }

        created_response = _request_cao("POST", "/sessions", params=params)
        created_agent = _response_json_or_text(created_response)
        created_agent_id = created_agent.get("id") if isinstance(created_agent, dict) else None
        if isinstance(created_agent_id, str):
            _register_team(created_agent_id)
        return {
            "ok": True,
            "role_type": payload.role_type,
            "leader_id": created_agent_id if isinstance(created_agent_id, str) else None,
            "agent": created_agent,
        }

    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to create organization agent: {exc}")


@app.post("/console/agents/{terminal_id}/input")
async def send_input_to_agent(terminal_id: str, payload: AgentMessageRequest) -> Dict[str, Any]:
    try:
        response = _request_cao(
            "POST",
            f"/terminals/{terminal_id}/input",
            params={"message": payload.message},
        )
        body = _response_json_or_text(response)
        return {"ok": True, "terminal_id": terminal_id, "result": body}
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to send input: {exc}")


@app.get("/console/agents/{terminal_id}/stream")
async def stream_agent_output(
    terminal_id: str,
    request: Request,
    max_events: Optional[int] = None,
) -> StreamingResponse:
    async def event_generator():
        last_output = ""
        emitted = 0

        while True:
            if await request.is_disconnected():
                break
            if max_events is not None and emitted >= max_events:
                break

            try:
                response = _request_cao(
                    "GET",
                    f"/terminals/{terminal_id}/output",
                    params={"mode": "last"},
                )
                body = _response_json_or_text(response)
                output_text = ""
                if isinstance(body, dict):
                    output_text = str(body.get("output", "")).strip()

                if output_text and output_text != last_output:
                    last_output = output_text
                    emitted += 1
                    payload = json.dumps(
                        {
                            "terminal_id": terminal_id,
                            "output": output_text,
                            "at": int(time.time() * 1000),
                        },
                        ensure_ascii=False,
                    )
                    yield f"data: {payload}\n\n"
            except Exception as exc:
                logger.warning("SSE stream read failed for %s: %s", terminal_id, exc)

            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.post("/console/agents/{receiver_id}/message")
async def send_message_to_agent(receiver_id: str, payload: InboxMessageRequest) -> Dict[str, Any]:
    sender_id = payload.sender_id or _resolve_sender_id(receiver_id)
    if not sender_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot auto-resolve sender_id. Provide sender_id or ensure a supervisor exists.",
        )

    try:
        response = _request_cao(
            "POST",
            f"/terminals/{receiver_id}/inbox/messages",
            params={"sender_id": sender_id, "message": payload.message},
        )
        body = _response_json_or_text(response)
        return {
            "ok": True,
            "receiver_id": receiver_id,
            "sender_id": sender_id,
            "result": body,
        }
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to send inbox message: {exc}")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_to_cao(request: Request, path: str) -> Response:
    """
    Proxy all requests to the cao-server.
    This acts as a middleware layer between the frontend and the actual CAO API.
    """
    # Construct the upstream URL
    upstream_url = f"{CAO_SERVER_URL}/{path}"
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex

    # Forward query parameters
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Prepare headers
    headers = {"X-Request-Id": request_id}
    if request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]
    if request.headers.get("authorization"):
        headers["Authorization"] = request.headers["authorization"]

    # Get request body if present
    body = None
    if request.method in ["POST", "PUT", "PATCH"]:
        try:
            body = await request.body()
        except Exception:
            pass

    try:
        logger.info(
            "Proxying request id=%s method=%s path=/%s upstream=%s",
            request_id,
            request.method,
            path,
            upstream_url,
        )

        # Make request to cao-server
        response = requests.request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            data=body,
            timeout=30,
        )

        # Return the response from cao-server
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers={**dict(response.headers), "X-Request-Id": request_id},
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"Error proxying request to cao-server: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach cao-server: {str(e)}",
        )


def main() -> None:
    """Run the control panel server."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    logger.info(f"Starting CAO Control Panel server on {CONTROL_PANEL_HOST}:{CONTROL_PANEL_PORT}")
    logger.info(f"Proxying requests to cao-server at {CAO_SERVER_URL}")

    uvicorn.run(
        app,
        host=CONTROL_PANEL_HOST,
        port=CONTROL_PANEL_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
