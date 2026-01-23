"""V2 API routes for CAO Web UI - Sessions, Agents, Activity, Learning."""
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set
from datetime import datetime

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.clients.beads import BeadsClient
from cli_agent_orchestrator.constants import KIRO_AGENTS_DIR, SESSION_PREFIX
from cli_agent_orchestrator.services import terminal_service, session_service
from cli_agent_orchestrator.providers.manager import provider_manager

router = APIRouter(prefix="/v2")
beads = BeadsClient()

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


class AutoModeToggle(BaseModel):
    enabled: bool


class ContextProposal(BaseModel):
    agent_name: str
    changes: str
    reason: str


class ChatDecompose(BaseModel):
    text: str


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
    lines = output.strip().split("\n")[-50:]
    text = "\n".join(lines).lower()
    
    # Check for waiting input patterns
    if re.search(r"(waiting for|enter your|type your|what would you|how can i help)", text):
        return "WAITING_INPUT"
    if re.search(r"(error|exception|failed|traceback)", text):
        return "ERROR"
    if re.search(r"(processing|working|analyzing|reading|writing)", text):
        return "PROCESSING"
    return "IDLE"


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
    # Try .json first (standard format), then .md
    json_path = KIRO_AGENTS_DIR / f"{name}.json"
    md_path = KIRO_AGENTS_DIR / f"{name}.md"
    
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text())
            # Also try to load steering file
            steering_path = Path.home() / ".kiro" / "steering" / f"{name}.md"
            steering = steering_path.read_text() if steering_path.exists() else None
            return {
                "name": data.get("name", name),
                "description": data.get("description", ""),
                "path": str(json_path),
                "config": data,
                "steering": steering
            }
        except Exception as e:
            raise HTTPException(500, f"Failed to read agent: {e}")
    elif md_path.exists():
        return {
            "name": name,
            "description": "",
            "path": str(md_path),
            "steering": md_path.read_text()
        }
    else:
        raise HTTPException(404, "Agent not found")


@router.put("/agents/{name}")
async def update_agent(name: str, req: AgentCreate):
    """Update agent profile."""
    path = KIRO_AGENTS_DIR / f"{name}.md"
    if not path.exists():
        raise HTTPException(404, "Agent not found")
    content = f"# {req.name}\n\n{req.description}\n\n{req.steering}"
    path.write_text(content)
    return {"name": name, "path": str(path)}


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
            if data.get("terminals"):
                term = data["terminals"][0]
                try:
                    output = terminal_service.get_output(term["id"])
                    status = detect_session_status(output)
                except:
                    pass
            result.append({**s, "status": status, "terminals": data.get("terminals", [])})
        except:
            result.append({**s, "status": "ERROR", "terminals": []})
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


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Terminate session."""
    try:
        session_service.delete_session(session_id)
        await broadcast_activity({
            "type": "session_ended",
            "session_id": session_id,
            "timestamp": datetime.now().isoformat()
        })
        return {"success": True}
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


# --- WebSocket Streaming ---
@router.websocket("/sessions/{session_id}/stream")
async def stream_terminal(ws: WebSocket, session_id: str):
    """WebSocket for live terminal output using tmux pipe-pane."""
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
        
        # Use asyncio subprocess to run tmux capture-pane in a loop
        import subprocess
        last_output = ""
        
        while True:
            try:
                # Capture current pane content
                result = subprocess.run(
                    ["/usr/local/bin/tmux", "capture-pane", "-t", target, "-p", "-S", "-500"],
                    capture_output=True, text=True, timeout=1
                )
                output = result.stdout.rstrip('\n') + '\n' if result.stdout else ''
                
                if output != last_output:
                    # Send only new content if possible
                    if last_output and output.startswith(last_output):
                        new_content = output[len(last_output):]
                    else:
                        new_content = output
                        
                    if new_content.strip():
                        await ws.send_json({
                            "type": "output", 
                            "data": new_content,
                            "status": detect_session_status(output)
                        })
                    last_output = output
                    
                await asyncio.sleep(0.1)  # 100ms polling
            except Exception as e:
                logger.error(f"Stream error: {e}")
                await asyncio.sleep(0.5)
                
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")


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


# --- Beads with Assignment ---
@router.post("/beads/{bead_id}/assign")
async def assign_bead(bead_id: str, req: BeadAssign):
    """Assign bead to session."""
    task = beads.get(bead_id)
    if not task:
        raise HTTPException(404, "Bead not found")
    
    # Update assignee
    task = beads.wip(bead_id, req.session_id)
    
    # Send task to session
    try:
        data = session_service.get_session(req.session_id)
        if data.get("terminals"):
            term_id = data["terminals"][0]["id"]
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


@router.post("/learn/sessions/{session_id}")
async def trigger_learning(session_id: str, outcome: str = "neutral"):
    """Trigger context learning for completed session."""
    try:
        data = session_service.get_session(session_id)
        if not data.get("terminals"):
            raise HTTPException(404, "No terminal in session")
        
        term_id = data["terminals"][0]["id"]
        output = terminal_service.get_output(term_id)
        agent_name = data["terminals"][0].get("agent_profile", "unknown")
        
        # Use new learning system
        proposal = learning_system.create_proposal(session_id, output, outcome)
        proposal["agent_name"] = agent_name
        
        return proposal
    except ValueError as e:
        raise HTTPException(404, str(e))


# --- Chat Bar / Task Decomposition ---
@router.post("/beads/decompose")
async def decompose_tasks(req: ChatDecompose):
    """Decompose text into multiple beads (simplified)."""
    # Simple decomposition - split by newlines or numbered items
    lines = [l.strip() for l in req.text.split("\n") if l.strip()]
    tasks = []
    
    for line in lines:
        # Remove numbering
        clean = re.sub(r"^\d+[\.\)]\s*", "", line)
        if clean and len(clean) > 3:
            task = beads.add(clean, "", 2)
            tasks.append(task.__dict__)
    
    return {"tasks": tasks, "count": len(tasks)}


# --- Position Persistence ---
class PositionUpdate(BaseModel):
    x: float
    y: float


# In-memory position storage (would be DB in production)
session_positions: Dict[str, Dict[str, float]] = {}
bead_positions: Dict[str, Dict[str, float]] = {}


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
