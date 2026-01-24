"""Web dashboard API routes for Beads, Ralph, and real-time updates."""
from pathlib import Path
import asyncio
import json
from typing import List, Optional, Set
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from cli_agent_orchestrator.clients.beads import BeadsClient, Task
from cli_agent_orchestrator.clients.ralph import RalphRunner
from cli_agent_orchestrator.api.v2 import clear_bead_position

router = APIRouter()
beads = BeadsClient(working_dir=str(Path.home() / ".beads-planning"))
ralph = RalphRunner()

# WebSocket connections
connections: Set[WebSocket] = set()

# Pydantic models
class TaskCreate(BaseModel):
    title: str
    description: str = ""
    priority: int = 2
    tags: str = "[]"

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[int] = None
    status: Optional[str] = None
    assignee: Optional[str] = None

class RalphStart(BaseModel):
    prompt: str
    min_iterations: int = 3
    max_iterations: int = 10
    completion_promise: str = "COMPLETE"
    task_id: Optional[str] = None
    work_dir: Optional[str] = None

class RalphFeedback(BaseModel):
    score: int
    summary: str
    improvements: List[str] = []
    next_steps: List[str] = []
    ideas: List[str] = []
    blockers: List[str] = []

# Broadcast helper
async def broadcast(event_type: str, data: dict):
    msg = json.dumps({"type": event_type, "data": data})
    for ws in list(connections):
        try:
            await ws.send_text(msg)
        except:
            connections.discard(ws)

# Tasks (Beads) endpoints
@router.get("/tasks")
async def list_tasks(status: Optional[str] = None, priority: Optional[int] = None):
    tasks = beads.list(status=status, priority=priority)
    return [t.__dict__ for t in tasks]

@router.get("/tasks/next")
async def next_task(priority: Optional[int] = None):
    task = beads.next(priority=priority)
    if not task:
        raise HTTPException(404, "No open tasks")
    return task.__dict__

@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    task = beads.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task.__dict__

@router.post("/tasks", status_code=201)
async def create_task(req: TaskCreate):
    task = beads.add(req.title, req.description, req.priority, req.tags)
    await broadcast("task_created", task.__dict__)
    return task.__dict__

@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, req: TaskUpdate):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    task = beads.update(task_id, **updates)
    if not task:
        raise HTTPException(404, "Task not found")
    await broadcast("task_updated", task.__dict__)
    return task.__dict__

@router.post("/tasks/{task_id}/wip")
async def mark_wip(task_id: str, assignee: Optional[str] = None):
    task = beads.wip(task_id, assignee)
    if not task:
        raise HTTPException(404, "Task not found")
    await broadcast("task_updated", task.__dict__)
    return task.__dict__

@router.post("/tasks/{task_id}/close")
async def close_task(task_id: str):
    task = beads.close(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await broadcast("task_updated", task.__dict__)
    return task.__dict__

@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    if not beads.delete(task_id):
        raise HTTPException(404, "Task not found")
    clear_bead_position(task_id)
    await broadcast("task_deleted", {"id": task_id})
    return {"success": True}

@router.post("/tasks/unassign-session/{session_id}")
async def unassign_session_beads(session_id: str):
    """Unassign all beads from a session (keeps status unchanged)."""
    tasks_list = beads.list()
    unassigned = []
    for task in tasks_list:
        if task.assignee == session_id:
            beads.update(task.id, assignee=None)
            unassigned.append(task.id)
    await broadcast("tasks_unassigned", {"session_id": session_id, "task_ids": unassigned})
    return {"unassigned": unassigned}

# Ralph endpoints
@router.get("/ralph")
async def ralph_status():
    state = ralph.status()
    if not state:
        return {"active": False}
    return state.__dict__

@router.post("/ralph", status_code=201)
async def ralph_start(req: RalphStart):
    state = ralph.start(
        req.prompt, req.min_iterations, req.max_iterations,
        req.completion_promise, req.task_id, req.work_dir
    )
    await broadcast("ralph_started", state.__dict__)
    return state.__dict__

@router.post("/ralph/feedback")
async def ralph_feedback(req: RalphFeedback):
    state = ralph.feedback(
        req.score, req.summary, req.improvements,
        req.next_steps, req.ideas, req.blockers
    )
    if not state:
        raise HTTPException(404, "No active Ralph loop")
    await broadcast("ralph_updated", state.__dict__)
    return state.__dict__

@router.post("/ralph/stop")
async def ralph_stop():
    if not ralph.stop():
        raise HTTPException(404, "No active Ralph loop")
    await broadcast("ralph_stopped", {})
    return {"success": True}

@router.post("/ralph/complete")
async def ralph_complete():
    state = ralph.complete()
    if not state:
        raise HTTPException(404, "No active Ralph loop")
    await broadcast("ralph_completed", state.__dict__)
    return state.__dict__

# Token usage tracking
from cli_agent_orchestrator.utils.token_tracker import get_usage, track_input, track_output, reset_usage

@router.get("/tokens/{session_id}")
async def get_token_usage(session_id: str):
    """Get estimated token usage for a session."""
    return get_usage(session_id).to_dict()

@router.post("/tokens/{session_id}/track")
async def track_tokens(session_id: str, data: dict):
    """Track input/output text for token estimation."""
    if "input" in data:
        track_input(session_id, data["input"])
    if "output" in data:
        track_output(session_id, data["output"])
    usage = get_usage(session_id)
    await broadcast("token_update", {"session_id": session_id, **usage.to_dict()})
    return usage.to_dict()

@router.post("/tokens/{session_id}/reset")
async def reset_token_usage(session_id: str):
    """Reset token usage for a session."""
    reset_usage(session_id)
    return {"success": True}

# WebSocket for real-time updates
@router.websocket("/ws/updates")
async def websocket_updates(ws: WebSocket):
    await ws.accept()
    connections.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        connections.discard(ws)
