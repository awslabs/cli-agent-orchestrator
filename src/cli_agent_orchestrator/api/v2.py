"""V2 API routes for CAO Web UI - Sessions, Agents, Activity, Learning."""
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set
from datetime import datetime

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
from pydantic import BaseModel

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.clients.beads_real import BeadsClient, resolve_workspace, resolve_context_files
from cli_agent_orchestrator.clients.database import get_inbox_messages, get_terminal_by_bead, set_terminal_bead
from cli_agent_orchestrator.utils.context import inject_context_files
from cli_agent_orchestrator.constants import KIRO_AGENTS_DIR, SESSION_PREFIX
from cli_agent_orchestrator.services import terminal_service, session_service, flow_service
from cli_agent_orchestrator.providers.manager import provider_manager

router = APIRouter(prefix="/v2")
beads = BeadsClient(working_dir=str(Path.home() / ".beads-planning"))

# WebSocket connections for terminal streaming
terminal_streams: Dict[str, Set[WebSocket]] = {}
activity_streams: Set[WebSocket] = set()

# Activity log (in-memory, recent 500 entries)
activity_log: List[Dict] = []


class AgentCreate(BaseModel):
    name: str
    description: str = ""
    steering: str = ""


class SessionCreate(BaseModel):
    agent_name: str
    provider: str = "kiro_cli"


class BeadAssign(BaseModel):
    session_id: str


class BeadAssignAgent(BaseModel):
    agent_name: str
    provider: str = "kiro-cli"


class AutoModeToggle(BaseModel):
    enabled: bool


class TerminalInput(BaseModel):
    text: str


class ContextProposal(BaseModel):
    agent_name: str
    changes: str
    reason: str


class ChatDecompose(BaseModel):
    text: str


class EpicCreate(BaseModel):
    title: str
    steps: List[str]
    description: str = ""
    priority: int = 2
    sequential: bool = True
    max_concurrent: int = 3
    labels: Optional[List[str]] = None


class DependencyAdd(BaseModel):
    depends_on: str


# Per-bead locks to prevent concurrent assignment races
_assign_locks: Dict[str, asyncio.Lock] = {}


def _get_assign_lock(bead_id: str) -> asyncio.Lock:
    if bead_id not in _assign_locks:
        _assign_locks[bead_id] = asyncio.Lock()
    return _assign_locks[bead_id]


# --- Agent Discovery ---
def discover_agents() -> List[Dict]:
    """Discover agents from ~/.kiro/agents/"""
    agents = []
    if KIRO_AGENTS_DIR.exists():
        for f in KIRO_AGENTS_DIR.iterdir():
            if f.suffix == ".json" and not f.name.endswith(".bak"):
                try:
                    data = json.loads(f.read_text())
                    agents.append({
                        "name": data.get("name", f.stem),
                        "description": data.get("description", ""),
                        "path": str(f),
                        "last_modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()
                    })
                except:
                    pass
    return agents


def detect_session_status(output: str) -> str:
    """Detect session status from terminal output."""
    if not output or not output.strip():
        return "IDLE"
    
    lines = output.strip().split("\n")[-30:]  # Check last 30 lines
    text = "\n".join(lines)
    last_lines = "\n".join(lines[-5:])
    
    # Check for error indicators first
    if re.search(r"(Traceback \(most recent|raise \w+Error|^\s*\w+Error:)", last_lines):
        return "ERROR"
    
    # Check for kiro/q idle prompt at end (green arrow or prompt)
    # If we see the prompt, agent is idle/ready
    if re.search(r"(>\s*$|❯\s*$|kiro.*>\s*$|q.*>\s*$)", last_lines, re.IGNORECASE | re.MULTILINE):
        return "IDLE"
    
    # Check for permission/approval prompts
    if re.search(r"(allow|deny|yes.*no|approve|confirm|\[y/n\])", last_lines, re.IGNORECASE):
        return "WAITING_INPUT"
    
    # No idle prompt visible = agent is processing/generating
    return "PROCESSING"


def extract_activity(output: str, session_id: str) -> List[Dict]:
    """Extract activity entries from terminal output."""
    entries = []
    lines = output.split("\n")
    
    for line in lines[-100:]:
        if "<invoke" in line:
            match = re.search(r'name="([^"]+)"', line)
            if match:
                entries.append({
                    "type": "tool_call",
                    "tool": match.group(1),
                    "session_id": session_id,
                    "timestamp": datetime.now().isoformat()
                })
        elif re.search(r"(fs_write|fs_read|execute_bash)", line):
            entries.append({
                "type": "file_op",
                "detail": line[:100],
                "session_id": session_id,
                "timestamp": datetime.now().isoformat()
            })
    return entries


# --- Agents API ---
@router.get("/agents")
async def list_agents():
    """List available agents from ~/.kiro/agents/"""
    return discover_agents()


@router.post("/agents", status_code=201)
async def create_agent(req: AgentCreate):
    """Create new agent profile."""
    KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = KIRO_AGENTS_DIR / f"{req.name}.md"
    if path.exists():
        raise HTTPException(400, "Agent already exists")
    content = f"# {req.name}\n\n{req.description}\n\n{req.steering}"
    path.write_text(content)
    return {"name": req.name, "path": str(path)}


@router.get("/agents/{name}")
async def get_agent(name: str):
    """Get agent details."""
    json_path = KIRO_AGENTS_DIR / f"{name}.json"
    md_path = KIRO_AGENTS_DIR / f"{name}.md"
    
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text())
            # Load context file from resources
            context = ""
            context_path = None
            resources = data.get("resources", [])
            for res in resources:
                if res.startswith("file://") and res.endswith(".md"):
                    context_path = res.replace("file://", "")
                    if Path(context_path).exists():
                        context = Path(context_path).read_text()
                    break
            return {
                "name": data.get("name", name),
                "description": data.get("description", ""),
                "path": str(json_path),
                "config": data,
                "context": context,
                "context_path": context_path
            }
        except Exception as e:
            raise HTTPException(500, f"Failed to read agent: {e}")
    elif md_path.exists():
        return {
            "name": name,
            "description": "",
            "path": str(md_path),
            "context": md_path.read_text(),
            "context_path": str(md_path)
        }
    else:
        raise HTTPException(404, "Agent not found")


@router.put("/agents/{name}")
async def update_agent(name: str, req: AgentCreate):
    """Update agent context file."""
    json_path = KIRO_AGENTS_DIR / f"{name}.json"
    
    if json_path.exists():
        data = json.loads(json_path.read_text())
        resources = data.get("resources", [])
        for res in resources:
            if res.startswith("file://") and res.endswith(".md"):
                context_path = Path(res.replace("file://", ""))
                context_path.parent.mkdir(parents=True, exist_ok=True)
                context_path.write_text(req.steering)
                return {"name": name, "context_path": str(context_path)}
    
    # Fallback to md file
    md_path = KIRO_AGENTS_DIR / f"{name}.md"
    if md_path.exists():
        md_path.write_text(req.steering)
        return {"name": name, "context_path": str(md_path)}
    
    raise HTTPException(404, "Agent not found")


@router.delete("/agents/{name}")
async def delete_agent(name: str):
    """Delete agent profile."""
    json_path = KIRO_AGENTS_DIR / f"{name}.json"
    md_path = KIRO_AGENTS_DIR / f"{name}.md"
    
    if json_path.exists():
        json_path.unlink()
        return {"success": True}
    elif md_path.exists():
        md_path.unlink()
        return {"success": True}
    else:
        raise HTTPException(404, "Agent not found")


# --- Sessions API ---
@router.get("/sessions")
async def list_sessions():
    """List active sessions with status."""
    sessions = session_service.list_sessions()
    result = []
    for s in sessions:
        try:
            data = session_service.get_session(s["id"])
            status = "IDLE"
            agent_name = "unknown"
            parent_session = None
            if data.get("terminals"):
                term = data["terminals"][0]
                agent_name = term.get("agent_profile", "unknown")
                parent_terminal_id = term.get("parent_terminal_id")
                if parent_terminal_id:
                    # Find parent session from parent terminal
                    from cli_agent_orchestrator.clients.database import list_all_terminals
                    all_terms = list_all_terminals()
                    parent_term = next((t for t in all_terms if t["id"] == parent_terminal_id), None)
                    if parent_term:
                        parent_session = parent_term.get("tmux_session")
                try:
                    output = terminal_service.get_output(term["id"])
                    status = detect_session_status(output)
                except:
                    pass
            result.append({
                **s, 
                "status": status, 
                "agent_name": agent_name, 
                "terminals": data.get("terminals", []),
                "parent_session": parent_session
            })
        except:
            result.append({**s, "status": "ERROR", "agent_name": "unknown", "terminals": [], "parent_session": None})
    return result


@router.post("/sessions", status_code=201)
async def create_session(req: SessionCreate):
    """Spawn new agent session."""
    terminal = terminal_service.create_terminal(
        provider=req.provider,
        agent_profile=req.agent_name,
        new_session=True
    )
    await broadcast_activity({
        "type": "session_started",
        "session_id": terminal.session_name,
        "agent": req.agent_name,
        "timestamp": datetime.now().isoformat()
    })
    return {
        "id": terminal.session_name,
        "terminal_id": terminal.id,
        "agent": req.agent_name,
        "status": "IDLE"
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """Get session details with status."""
    try:
        data = session_service.get_session(session_id)
        status = "IDLE"
        if data.get("terminals"):
            term = data["terminals"][0]
            try:
                output = terminal_service.get_output(term["id"])
                status = detect_session_status(output)
            except:
                pass
        return {**data, "status": status}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/sessions/{session_id}/children")
async def get_session_children(session_id: str):
    """Get child sessions (subagents) of a session."""
    children = session_service.get_session_children(session_id)
    return {"children": children}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Terminate session and all child sessions (subagents)."""
    try:
        # Get children first for UI feedback
        children = session_service.get_session_children(session_id)
        
        # Broadcast that we're starting cascade delete
        if children:
            await broadcast_activity({
                "type": "session_delete_started",
                "session_id": session_id,
                "children": children,
                "timestamp": datetime.now().isoformat()
            })
        
        # Delete with progress callback
        async def on_progress(action: str, target: str):
            await broadcast_activity({
                "type": "session_delete_progress",
                "action": action,
                "target": target,
                "parent": session_id,
                "timestamp": datetime.now().isoformat()
            })
        
        # Note: Can't use async callback directly, so we'll broadcast after
        result = session_service.delete_session(session_id)
        
        # Clear assignee from any beads assigned to deleted sessions
        for deleted_id in result["deleted"]:
            beads.clear_assignee_by_session(deleted_id)
            await broadcast_activity({
                "type": "session_ended",
                "session_id": deleted_id,
                "timestamp": datetime.now().isoformat()
            })
        
        return {"success": True, "deleted": result["deleted"], "errors": result["errors"]}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/sessions")
async def delete_sessions(session_ids: List[str] = None, all: bool = False):
    """Mass delete sessions. Pass session_ids list or all=true."""
    deleted = []
    errors = []
    
    if all:
        sessions = session_service.list_sessions()
        session_ids = [s["id"] for s in sessions]
    
    if not session_ids:
        raise HTTPException(400, "Provide session_ids or all=true")
    
    for sid in session_ids:
        try:
            session_service.delete_session(sid)
            deleted.append(sid)
        except Exception as e:
            errors.append({"id": sid, "error": str(e)})
    
    return {"deleted": deleted, "errors": errors}

@router.post("/sessions/{session_id}/input")
async def send_input(session_id: str, message: str, raw: bool = False):
    """Send input to session. Set raw=true for direct keystrokes without Enter."""
    try:
        data = session_service.get_session(session_id)
        if not data.get("terminals"):
            raise HTTPException(404, "No terminal in session")
        term_id = data["terminals"][0]["id"]
        terminal_service.send_input(term_id, message, add_enter=not raw)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/sessions/{session_id}/inbox")
async def get_session_inbox(session_id: str):
    """Get pending messages for a session."""
    try:
        data = session_service.get_session(session_id)
        if not data.get("terminals"):
            return {"messages": []}
        
        messages = []
        for term in data["terminals"]:
            term_messages = get_inbox_messages(term["id"], limit=100)
            for msg in term_messages:
                messages.append({
                    "id": msg.id,
                    "from_session": msg.sender_id,
                    "to_session": msg.receiver_id,
                    "content": msg.message,
                    "status": msg.status.value,
                    "timestamp": msg.created_at.isoformat()
                })
        return {"messages": messages}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/sessions/{session_id}/output")
async def get_output(session_id: str, lines: int = 200):
    """Get terminal output."""
    try:
        data = session_service.get_session(session_id)
        if not data.get("terminals"):
            raise HTTPException(404, "No terminal in session")
        term_id = data["terminals"][0]["id"]
        output = terminal_service.get_output(term_id)
        # Strip trailing empty lines but keep the prompt line
        output = output.rstrip('\n') + '\n' if output else ''
        return {"output": output, "status": detect_session_status(output)}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/sessions/{session_id}/context")
async def get_context_usage(session_id: str):
    """Get context window usage by sending /context command."""
    import re
    import time
    try:
        data = session_service.get_session(session_id)
        if not data.get("terminals"):
            raise HTTPException(404, "No terminal in session")
        term_id = data["terminals"][0]["id"]
        
        # Send /context command
        terminal_service.send_input(term_id, "/context")
        time.sleep(2)  # Wait for response
        
        # Get output and parse
        output = terminal_service.get_output(term_id)
        
        # Parse context usage from output
        # Pattern: "Context window: 31.1% used (estimated)"
        total_match = re.search(r'Context window:\s*([\d.]+)%', output)
        tools_match = re.search(r'Tools\s*\S*\s*([\d.]+)%', output)
        files_match = re.search(r'Context files\s*\S*\s*([\d.]+)%', output)
        responses_match = re.search(r'responses\s*\S*\s*([\d.]+)%', output)
        prompts_match = re.search(r'prompts\s*\S*\s*([\d.]+)%', output)
        
        return {
            "total_percent": float(total_match.group(1)) if total_match else 0,
            "tools_percent": float(tools_match.group(1)) if tools_match else 0,
            "files_percent": float(files_match.group(1)) if files_match else 0,
            "responses_percent": float(responses_match.group(1)) if responses_match else 0,
            "prompts_percent": float(prompts_match.group(1)) if prompts_match else 0,
        }
    except ValueError as e:
        raise HTTPException(404, str(e))


# --- WebSocket Streaming ---
@router.websocket("/sessions/{session_id}/stream")
async def stream_terminal(ws: WebSocket, session_id: str):
    """WebSocket for live terminal output using tmux capture-pane."""
    await ws.accept()
    
    try:
        data = session_service.get_session(session_id)
        if not data.get("terminals"):
            await ws.close()
            return
            
        term = data["terminals"][0]
        tmux_session = term["tmux_session"]
        tmux_window = term["tmux_window"]
        target = f"{tmux_session}:{tmux_window}"
        
        import subprocess

        # Initialize last_output to current state so we only send NEW content
        # (the frontend already loaded full history via HTTP GET)
        init_result = subprocess.run(
            ["/usr/local/bin/tmux", "capture-pane", "-t", target, "-p", "-S", "-500"],
            capture_output=True, text=True, timeout=1
        )
        last_output = init_result.stdout or ''

        while True:
            try:
                result = subprocess.run(
                    ["/usr/local/bin/tmux", "capture-pane", "-t", target, "-p", "-S", "-500"],
                    capture_output=True, text=True, timeout=1
                )
                output = result.stdout or ''
                
                if output != last_output:
                    # Send only new content
                    if last_output and output.startswith(last_output):
                        new_content = output[len(last_output):]
                    else:
                        new_content = output
                        
                    if new_content:
                        await ws.send_json({
                            "type": "output", 
                            "data": new_content,
                            "status": detect_session_status(output)
                        })
                    last_output = output
                    
                await asyncio.sleep(0.05)  # 50ms for responsive updates
            except Exception as e:
                logger.error(f"Stream error: {e}")
                await asyncio.sleep(0.2)
                
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")


# --- Terminal (Sub-agent) Endpoints ---
@router.get("/terminals/{terminal_id}/output")
async def get_terminal_output(terminal_id: str):
    """Get terminal output directly by terminal ID."""
    try:
        output = terminal_service.get_output(terminal_id)
        return {"output": output, "status": "IDLE"}
    except Exception as e:
        raise HTTPException(404, f"Terminal not found: {e}")


@router.post("/terminals/{terminal_id}/input")
async def send_terminal_input(terminal_id: str, req: TerminalInput):
    """Send input to terminal directly by terminal ID."""
    try:
        terminal_service.send_input(terminal_id, req.text)
        return {"status": "sent"}
    except Exception as e:
        raise HTTPException(404, f"Terminal not found: {e}")


@router.websocket("/terminals/{terminal_id}/stream")
async def stream_terminal_direct(ws: WebSocket, terminal_id: str):
    """WebSocket for live terminal output by terminal ID."""
    await ws.accept()
    
    try:
        # Initialize to current state so we only send NEW content
        # (the frontend already loaded full history via HTTP GET)
        initial_output = terminal_service.get_output(terminal_id)
        last_output = initial_output
        last_len = len(initial_output)

        while True:
            try:
                output = terminal_service.get_output(terminal_id)

                if len(output) > last_len:
                    new_data = output[last_len:]
                    await ws.send_json({"type": "output", "data": new_data, "status": "IDLE"})
                    last_len = len(output)
                    last_output = output
                elif output != last_output:
                    # Content changed but length didn't (e.g. screen redraw) — update tracking only
                    last_output = output
                    last_len = len(output)
                    
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Terminal stream error: {e}")
                await asyncio.sleep(0.2)
                
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Terminal WebSocket error: {e}")


# --- Activity Feed ---
async def broadcast_activity(entry: Dict):
    """Broadcast activity to all connected clients."""
    activity_log.insert(0, entry)
    if len(activity_log) > 500:
        activity_log.pop()
    
    msg = json.dumps(entry)
    for ws in list(activity_streams):
        try:
            await ws.send_text(msg)
        except:
            activity_streams.discard(ws)


@router.get("/activity")
async def get_activity(session_id: Optional[str] = None, limit: int = 50):
    """Get activity feed."""
    entries = activity_log
    if session_id:
        entries = [e for e in entries if e.get("session_id") == session_id]
    return entries[:limit]


@router.websocket("/activity/stream")
async def stream_activity(ws: WebSocket):
    """WebSocket for live activity feed."""
    await ws.accept()
    activity_streams.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        activity_streams.discard(ws)


# --- Epic Endpoints ---
@router.post("/epics", status_code=201)
async def create_epic(req: EpicCreate):
    """Create an epic with child beads."""
    if not req.steps:
        raise HTTPException(400, "At least one step is required")
    epic = beads.create_epic(
        title=req.title, steps=req.steps, description=req.description,
        priority=req.priority, sequential=req.sequential,
        max_concurrent=req.max_concurrent, labels=req.labels
    )
    children = beads.get_children(epic.id)
    await broadcast_activity({
        "type": "epic_created", "epic_id": epic.id,
        "children_count": len(children),
        "timestamp": datetime.now().isoformat()
    })
    return {"epic": epic.__dict__, "children": [c.__dict__ for c in children]}


@router.get("/epics/{epic_id}")
async def get_epic(epic_id: str):
    """Get epic with children and progress."""
    epic = beads.get(epic_id)
    if not epic:
        raise HTTPException(404, "Epic not found")
    children = beads.get_children(epic_id)
    completed = sum(1 for c in children if c.status == "closed")
    wip = sum(1 for c in children if c.status == "wip")
    return {
        "epic": epic.__dict__,
        "children": [c.__dict__ for c in children],
        "progress": {
            "total": len(children),
            "completed": completed,
            "wip": wip,
            "open": len(children) - completed - wip,
        }
    }


@router.get("/epics/{epic_id}/ready")
async def get_epic_ready(epic_id: str):
    """Get unblocked children ready for assignment."""
    epic = beads.get(epic_id)
    if not epic:
        raise HTTPException(404, "Epic not found")
    ready_tasks = beads.ready(parent_id=epic_id)
    return [t.__dict__ for t in ready_tasks]


@router.post("/beads/{bead_id}/dep", status_code=201)
async def add_dependency(bead_id: str, req: DependencyAdd):
    """Add dependency: bead_id is blocked by req.depends_on."""
    success = beads.add_dependency(bead_id, req.depends_on)
    if not success:
        raise HTTPException(400, "Failed to add dependency")
    return {"success": True, "bead_id": bead_id, "depends_on": req.depends_on}


@router.delete("/beads/{bead_id}/dep/{dep_id}")
async def remove_dependency(bead_id: str, dep_id: str):
    """Remove dependency."""
    success = beads.remove_dependency(bead_id, dep_id)
    if not success:
        raise HTTPException(400, "Failed to remove dependency")
    return {"success": True}


@router.get("/beads/{bead_id}/session")
async def get_bead_session(bead_id: str):
    """Find which session/terminal is working on a bead."""
    terminal = get_terminal_by_bead(bead_id)
    if not terminal:
        raise HTTPException(404, "No session assigned to this bead")
    return terminal


# --- Beads with Assignment ---
@router.post("/beads/{bead_id}/assign")
async def assign_bead(bead_id: str, req: BeadAssign):
    """Assign bead to session."""
    task = beads.get(bead_id)
    if not task:
        raise HTTPException(404, "Bead not found")

    # Update assignee
    task = beads.wip(bead_id, req.session_id)

    # Store bead_id binding on the session's terminal
    try:
        data = session_service.get_session(req.session_id)
        if data.get("terminals"):
            term_id = data["terminals"][0]["id"]
            set_terminal_bead(term_id, bead_id)
            prompt = f"Work on this task: {task.title}\n\n{task.description}"
            terminal_service.send_input(term_id, prompt)
    except:
        pass

    await broadcast_activity({
        "type": "bead_assigned",
        "bead_id": bead_id,
        "session_id": req.session_id,
        "timestamp": datetime.now().isoformat()
    })
    return task.__dict__


@router.post("/tasks/unassign-session/{session_id}")
async def unassign_session_tasks(session_id: str):
    """Unassign all tasks from a session."""
    count = beads.clear_assignee_by_session(session_id)
    return {"unassigned": count}


class ChildBeadCreate(BaseModel):
    title: str
    description: str = ""
    priority: int = 2


@router.post("/beads/{bead_id}/children")
async def create_child_bead(bead_id: str, req: ChildBeadCreate):
    """Create a child bead under a parent."""
    parent = beads.get(bead_id)
    if not parent:
        raise HTTPException(404, "Parent bead not found")
    
    task = beads.create_child(bead_id, req.title, req.description, req.priority)
    
    await broadcast_activity({
        "type": "bead_created",
        "bead_id": task.id,
        "parent_id": bead_id,
        "timestamp": datetime.now().isoformat()
    })
    return task.__dict__


@router.get("/beads/{bead_id}/children")
async def get_child_beads(bead_id: str):
    """Get child beads of a parent."""
    children = beads.get_children(bead_id)
    return [t.__dict__ for t in children]


@router.get("/agents")
async def list_agents():
    """List available agent profiles."""
    agents = []
    agents_dir = Path.home() / ".kiro" / "agents"
    if agents_dir.exists():
        for f in agents_dir.glob("*.json"):
            agents.append({"name": f.stem, "source": "kiro"})
    return agents


@router.post("/beads/{bead_id}/assign-agent")
async def assign_bead_to_agent(bead_id: str, req: BeadAssignAgent):
    """Assign bead to agent profile - spawns new session and starts working."""
    async with _get_assign_lock(bead_id):
        task = beads.get(bead_id)
        if not task:
            raise HTTPException(404, "Bead not found")
        if task.assignee:
            raise HTTPException(409, f"Bead already assigned to {task.assignee}")

        # Spawn new session with agent + bead_id binding
        terminal = terminal_service.create_terminal(
            provider=req.provider,
            agent_profile=req.agent_name,
            new_session=True,
            bead_id=bead_id,
        )
        session_id = terminal.session_name

        # Update bead assignee
        task = beads.wip(bead_id, session_id)

        # Inject context files from bead labels (walks parent chain)
        context_files = resolve_context_files(task, beads)
        if context_files:
            inject_context_files(terminal.id, context_files)

        # Send task to agent
        prompt = f"Work on this task: {task.title}\n\n{task.description}" if task.description else f"Work on this task: {task.title}"
        terminal_service.send_input(terminal.id, prompt)

        await broadcast_activity({
            "type": "bead_assigned",
            "bead_id": bead_id,
            "session_id": session_id,
            "agent": req.agent_name,
            "timestamp": datetime.now().isoformat()
        })
        return {"task": task.__dict__, "session_id": session_id, "terminal_id": terminal.id}


# --- Auto-mode (stored per-session in memory) ---
auto_mode_sessions: Set[str] = set()


@router.post("/sessions/{session_id}/auto-mode")
async def toggle_auto_mode(session_id: str, req: AutoModeToggle):
    """Toggle auto-mode for session."""
    if req.enabled:
        auto_mode_sessions.add(session_id)
    else:
        auto_mode_sessions.discard(session_id)
    return {"session_id": session_id, "auto_mode": req.enabled}


@router.get("/sessions/{session_id}/auto-mode")
async def get_auto_mode(session_id: str):
    """Get auto-mode status."""
    return {"session_id": session_id, "auto_mode": session_id in auto_mode_sessions}


# --- Context Learning ---
context_proposals: List[Dict] = []


def analyze_session_output(output: str) -> Dict:
    """Analyze session output to extract learnings."""
    learnings = {"tools_used": [], "errors": [], "patterns": [], "files_modified": []}
    
    # Extract tool calls
    for match in re.finditer(r'<invoke name="([^"]+)"', output):
        tool = match.group(1)
        if tool not in learnings["tools_used"]:
            learnings["tools_used"].append(tool)
    
    # Extract errors
    for match in re.finditer(r'(error|exception|failed|traceback)[:\s]+([^\n]+)', output, re.I):
        learnings["errors"].append(match.group(2)[:100])
    
    # Extract file paths
    for match in re.finditer(r'(?:fs_write|fs_read|modified|created)[:\s]+([^\s\n]+\.\w+)', output, re.I):
        path = match.group(1)
        if path not in learnings["files_modified"]:
            learnings["files_modified"].append(path)
    
    # Detect patterns
    if "retry" in output.lower() or "again" in output.lower():
        learnings["patterns"].append("Required retries - consider adding error handling guidance")
    if len(learnings["tools_used"]) > 5:
        learnings["patterns"].append("Heavy tool usage - agent is thorough")
    if learnings["errors"]:
        learnings["patterns"].append("Encountered errors - may need better error recovery")
    
    return learnings


# Import the new learning system
from cli_agent_orchestrator.services.learning_service import learning_system


# --- Learning System Routes (specific routes BEFORE parameterized) ---

@router.get("/learn/stats")
async def get_learning_stats():
    """Get learning system statistics."""
    return learning_system.stats()


@router.get("/learn/context")
async def get_active_context():
    """Get all approved context bullets."""
    return {"bullets": learning_system.get_active_context()}


@router.get("/learn/memories")
async def list_memories(limit: int = 50):
    """List all memories in the bank."""
    return learning_system.memories[:limit]


@router.get("/learn/memories/search")
async def search_memories(query: str, limit: int = 5):
    """Search memories relevant to a query."""
    return learning_system.get_relevant_memories(query, limit)


@router.post("/learn/memories")
async def add_memory(title: str, content: str, outcome: str = "success"):
    """Add a human-provided memory (highest priority)."""
    return learning_system.add_human_memory(title, content, outcome)


@router.get("/learn/proposals")
async def list_proposals(status: Optional[str] = None):
    """List context update proposals (deltas)."""
    deltas = learning_system.deltas
    if status:
        return [d for d in deltas if d["status"] == status]
    return deltas


@router.post("/learn/proposals/{delta_id}/approve")
async def approve_proposal(delta_id: str, feedback: str = None):
    """Approve context delta."""
    result = learning_system.approve_delta(delta_id, feedback)
    if not result:
        raise HTTPException(404, "Delta not found")
    return result


@router.post("/learn/proposals/{delta_id}/reject")
async def reject_proposal(delta_id: str, feedback: str = None):
    """Reject context delta."""
    result = learning_system.reject_delta(delta_id, feedback)
    if not result:
        raise HTTPException(404, "Delta not found")
    return result


@router.put("/learn/proposals/{delta_id}")
async def edit_proposal(delta_id: str, bullets: list[str]):
    """Edit a delta's bullets (human refinement)."""
    result = learning_system.edit_delta(delta_id, bullets)
    if not result:
        raise HTTPException(404, "Delta not found")
    return result


@router.post("/learn/terminals/{terminal_id}")
async def learn_from_terminal(terminal_id: str, outcome: str = "neutral"):
    """Analyze terminal for mistakes/corrections and suggest agent.md diff."""
    try:
        output = terminal_service.get_output(terminal_id)
        if not output:
            raise HTTPException(404, "No output in terminal")
        
        term_data = terminal_service.get_terminal(terminal_id)
        agent_name = term_data.get("agent_profile", "unknown")
        
        # Create diff proposal instead of simple extraction
        proposal = learning_system.create_diff_proposal(
            session_id=terminal_id,
            output=output,
            agent_name=agent_name
        )
        proposal["source"] = "live_capture"
        
        return proposal
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/learn/sessions/{session_id}")
async def trigger_learning(session_id: str, outcome: str = "neutral"):
    """Analyze session for mistakes/corrections and suggest agent.md diff."""
    try:
        data = session_service.get_session(session_id)
        if not data.get("terminals"):
            raise HTTPException(404, "No terminal in session")
        
        term_id = data["terminals"][0]["id"]
        output = terminal_service.get_output(term_id)
        agent_name = data["terminals"][0].get("agent_profile", "unknown")
        
        # Create diff proposal
        proposal = learning_system.create_diff_proposal(
            session_id=session_id,
            output=output,
            agent_name=agent_name
        )
        
        return proposal
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/learn/diff-proposals")
async def list_diff_proposals(status: Optional[str] = None):
    """List agent.md diff proposals."""
    return learning_system.get_proposals(status)


@router.post("/learn/diff-proposals/{proposal_id}/approve")
async def approve_diff_proposal(proposal_id: str):
    """Approve and apply a diff proposal to agent.md."""
    result = learning_system.approve_proposal(proposal_id)
    if not result:
        raise HTTPException(404, "Proposal not found")
    return result


@router.post("/learn/diff-proposals/{proposal_id}/reject")
async def reject_diff_proposal(proposal_id: str, feedback: str = None):
    """Reject a diff proposal."""
    result = learning_system.reject_proposal(proposal_id, feedback)
    if not result:
        raise HTTPException(404, "Proposal not found")
    return result


# --- Chat Bar / Task Decomposition ---
@router.post("/beads/decompose")
async def decompose_tasks(req: ChatDecompose):
    """Decompose text into multiple beads using AI."""
    import subprocess
    import tempfile
    
    # Try AI decomposition via kiro-cli
    prompt = f"""Parse this text into tasks. Output JSON array only, no markdown:
[{{"title": "short title", "description": "details", "priority": 1-3}}]

Text:
{req.text}"""
    
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(prompt)
            f.flush()
            result = subprocess.run(
                ['kiro-cli', 'chat', '-m', prompt, '--no-interactive'],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout:
                # Try to parse JSON from output
                import json as json_mod
                output = result.stdout.strip()
                # Find JSON array in output
                start = output.find('[')
                end = output.rfind(']') + 1
                if start >= 0 and end > start:
                    parsed = json_mod.loads(output[start:end])
                    beads_list = []
                    for item in parsed:
                        task = beads.add(
                            item.get('title', 'Task'),
                            item.get('description', ''),
                            item.get('priority', 2)
                        )
                        beads_list.append(task.__dict__)
                    return {"beads": beads_list, "count": len(beads_list)}
    except Exception:
        pass
    
    # Fallback: simple line-based decomposition
    lines = [l.strip() for l in req.text.split("\n") if l.strip()]
    tasks = []
    for line in lines:
        clean = re.sub(r"^[-*•]\s*", "", re.sub(r"^\d+[\.\)]\s*", "", line))
        if clean and len(clean) > 3:
            task = beads.add(clean, "", 2)
            tasks.append(task.__dict__)
    return {"beads": tasks, "count": len(tasks)}


# --- Position Persistence ---
class PositionUpdate(BaseModel):
    x: float
    y: float


# In-memory position storage (would be DB in production)
session_positions: Dict[str, Dict[str, float]] = {}
bead_positions: Dict[str, Dict[str, float]] = {}


def clear_bead_position(bead_id: str):
    """Clear bead position when bead is deleted."""
    bead_positions.pop(bead_id, None)


@router.put("/sessions/{session_id}/position")
async def update_session_position(session_id: str, pos: PositionUpdate):
    """Save agent/session position on map."""
    session_positions[session_id] = {"x": pos.x, "y": pos.y}
    return {"session_id": session_id, "x": pos.x, "y": pos.y}


@router.put("/beads/{bead_id}/position")
async def update_bead_position(bead_id: str, pos: PositionUpdate):
    """Save bead position on map."""
    bead_positions[bead_id] = {"x": pos.x, "y": pos.y}
    return {"bead_id": bead_id, "x": pos.x, "y": pos.y}


@router.get("/map/state")
async def get_map_state():
    """Get all positions for initial map load."""
    return {
        "sessions": session_positions,
        "beads": bead_positions
    }


# --- Ralph Loop CRUD ---
class RalphCreate(BaseModel):
    prompt: str
    min_iterations: int = 3
    max_iterations: int = 10
    agent_count: int = 1


ralph_loops: Dict[str, Dict] = {}


@router.get("/ralph")
async def list_ralph_loops():
    """List all Ralph loops."""
    return list(ralph_loops.values())


@router.post("/ralph", status_code=201)
async def create_ralph_loop(req: RalphCreate):
    """Create a new Ralph loop."""
    import uuid
    loop_id = str(uuid.uuid4())[:8]
    loop = {
        "id": loop_id,
        "prompt": req.prompt,
        "min_iterations": req.min_iterations,
        "max_iterations": req.max_iterations,
        "current_iteration": 0,
        "status": "running",
        "agent_count": req.agent_count,
        "created_at": datetime.now().isoformat()
    }
    ralph_loops[loop_id] = loop
    await broadcast_activity({
        "type": "ralph_created",
        "loop_id": loop_id,
        "prompt": req.prompt,
        "timestamp": datetime.now().isoformat()
    })
    return loop


@router.get("/ralph/{loop_id}")
async def get_ralph_loop(loop_id: str):
    """Get Ralph loop details."""
    if loop_id not in ralph_loops:
        raise HTTPException(404, "Ralph loop not found")
    return ralph_loops[loop_id]


@router.delete("/ralph/{loop_id}")
async def delete_ralph_loop(loop_id: str):
    """Stop and delete a Ralph loop."""
    if loop_id not in ralph_loops:
        raise HTTPException(404, "Ralph loop not found")
    del ralph_loops[loop_id]
    await broadcast_activity({
        "type": "ralph_deleted",
        "loop_id": loop_id,
        "timestamp": datetime.now().isoformat()
    })
    return {"success": True}


# --- Flows API ---
class FlowCreate(BaseModel):
    name: str
    schedule: str
    agent_profile: str
    prompt: str
    provider: str = "q_cli"
    script: Optional[str] = None
    flow_type: str = "agent"  # agent or orchestrator


@router.get("/flows")
async def list_flows():
    """List all flows."""
    flows = flow_service.list_flows()
    return [
        {
            "name": f.name,
            "schedule": f.schedule,
            "agent_profile": f.agent_profile,
            "provider": f.provider,
            "enabled": f.enabled,
            "next_run": f.next_run.isoformat() if f.next_run else None,
            "last_run": f.last_run.isoformat() if f.last_run else None,
            "flow_type": getattr(f, 'flow_type', 'agent'),
        }
        for f in flows
    ]


@router.post("/flows")
async def create_flow(req: FlowCreate):
    """Create a new flow by generating a flow file."""
    try:
        # Create flow file in ~/.aws/cli-agent-orchestrator/flows/
        flows_dir = Path.home() / ".aws" / "cli-agent-orchestrator" / "flows"
        flows_dir.mkdir(parents=True, exist_ok=True)
        
        file_path = flows_dir / f"{req.name}.md"
        if file_path.exists():
            raise HTTPException(400, f"Flow '{req.name}' already exists")
        
        # Generate flow file content
        content = f"""---
name: {req.name}
schedule: "{req.schedule}"
agent_profile: {req.agent_profile}
provider: {req.provider}
flow_type: {req.flow_type}
"""
        if req.script:
            content += f"script: {req.script}\n"
        content += f"""---

{req.prompt}
"""
        file_path.write_text(content)
        
        # Add flow using existing service
        flow = flow_service.add_flow(str(file_path))
        
        await broadcast_activity({
            "type": "flow_created",
            "flow_name": req.name,
            "timestamp": datetime.now().isoformat()
        })
        
        return {
            "name": flow.name,
            "schedule": flow.schedule,
            "agent_profile": flow.agent_profile,
            "provider": flow.provider,
            "flow_type": req.flow_type,
            "enabled": flow.enabled,
            "next_run": flow.next_run.isoformat() if flow.next_run else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/flows/{name}")
async def get_flow(name: str):
    """Get flow details."""
    try:
        f = flow_service.get_flow(name)
        # Read prompt and metadata from file
        prompt = ""
        metadata = {}
        if f.file_path:
            try:
                import frontmatter
                with open(f.file_path) as fp:
                    post = frontmatter.load(fp)
                    prompt = post.content
                    metadata = post.metadata
            except:
                pass
        return {
            "name": f.name,
            "schedule": f.schedule,
            "agent_profile": f.agent_profile,
            "provider": f.provider,
            "enabled": f.enabled,
            "next_run": f.next_run.isoformat() if f.next_run else None,
            "last_run": f.last_run.isoformat() if f.last_run else None,
            "prompt": prompt,
            "script": f.script,
            "flow_type": getattr(f, 'flow_type', metadata.get('flow_type', 'agent')),
            "worker_count": metadata.get('worker_count', 3),
            "supervisor_agent": metadata.get('supervisor_agent', 'code_supervisor'),
            "worker_agents": metadata.get('worker_agents', ['developer', 'reviewer', 'tester']),
        }
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/flows/{name}/run")
async def run_flow(name: str):
    """Manually trigger a flow."""
    try:
        result = flow_service.execute_flow(name)
        await broadcast_activity({
            "type": "flow_executed",
            "flow_name": name,
            "session_id": result.get("session_id"),
            "timestamp": datetime.now().isoformat()
        })
        return {"success": True, **result}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/flows/{name}/executions")
async def get_flow_executions(name: str, limit: int = 20):
    """Get flow execution history."""
    try:
        executions = flow_service.get_execution_history(name, limit)
        return {"executions": executions}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/flows/executions/{execution_id}/log")
async def get_execution_log(execution_id: int):
    """Get execution output log."""
    from cli_agent_orchestrator.clients.database import get_flow_execution_log
    log = get_flow_execution_log(execution_id)
    if log is None:
        raise HTTPException(404, "Execution not found")
    return {"log": log}


@router.post("/flows/executions/{execution_id}/complete")
async def complete_execution(execution_id: int):
    """Manually mark execution as completed and capture logs."""
    from cli_agent_orchestrator.clients.database import get_flow_execution, update_flow_execution
    from cli_agent_orchestrator.clients.tmux import TmuxClient
    
    ex = get_flow_execution(execution_id)
    if not ex:
        raise HTTPException(404, "Execution not found")
    
    if ex['status'] != 'running':
        return {"success": True, "message": "Already completed"}
    
    # Capture logs
    output_log = None
    if ex.get('session_id'):
        try:
            session_data = session_service.get_session(ex['session_id'])
            tmux = TmuxClient()
            logs = []
            for term in session_data.get('terminals', []):
                agent = term.get('agent_profile', 'agent')
                history = tmux.get_history(term.get('tmux_session'), term.get('tmux_window'), tail_lines=2000) or ''
                logs.append(f"=== [{agent}] ===\n{history}")
            output_log = "\n\n".join(logs) if logs else None
        except:
            pass
    
    update_flow_execution(execution_id, 'completed', output_log=output_log)
    return {"success": True, "has_log": bool(output_log)}


@router.get("/sessions/{session_id}/history")
async def get_session_history(session_id: str, lines: int = 500):
    """Get full terminal history for a session (all agents)."""
    try:
        data = session_service.get_session(session_id)
        if not data.get("terminals"):
            return {"history": "", "terminals": []}
        
        # Get history from ALL terminals in the session
        all_history = []
        for term in data["terminals"]:
            agent = term.get("agent_profile", "agent")
            history = tmux_client.get_history(term["tmux_session"], term["tmux_window"], tail_lines=lines)
            all_history.append({
                "agent": agent,
                "terminal_id": term["id"],
                "history": history
            })
        
        # Combine into single view with agent headers
        combined = []
        for h in all_history:
            combined.append(f"\n{'='*60}\n[{h['agent']}] Terminal: {h['terminal_id']}\n{'='*60}\n")
            combined.append(h["history"])
        
        return {
            "history": "\n".join(combined),
            "session_id": session_id,
            "terminals": all_history
        }
    except Exception as e:
        raise HTTPException(404, str(e))


@router.post("/flows/{name}/enable")
async def enable_flow(name: str):
    """Enable a flow."""
    try:
        flow_service.enable_flow(name)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/flows/{name}/disable")
async def disable_flow(name: str):
    """Disable a flow."""
    try:
        flow_service.disable_flow(name)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/flows/{name}")
async def delete_flow(name: str):
    """Delete a flow."""
    try:
        flow_service.remove_flow(name)
        await broadcast_activity({
            "type": "flow_deleted",
            "flow_name": name,
            "timestamp": datetime.now().isoformat()
        })
        return {"success": True}
    except ValueError as e:
        raise HTTPException(404, str(e))
