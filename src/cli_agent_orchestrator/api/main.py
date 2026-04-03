"""Single FastAPI entry point for all HTTP routes."""

import asyncio
import fcntl
import json
import logging
import os
import pty
import signal
import struct
import subprocess
import termios
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator
from watchdog.observers.polling import PollingObserver

from cli_agent_orchestrator.clients.database import (
    create_inbox_message,
    get_inbox_messages,
    get_terminal_metadata,
    init_db,
)
from cli_agent_orchestrator.constants import (
    ALLOWED_HOSTS,
    CAO_HOME_DIR,
    CORS_ORIGINS,
    INBOX_POLLING_INTERVAL,
    SERVER_HOST,
    SERVER_PORT,
    SERVER_VERSION,
    TERMINAL_LOG_DIR,
)
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import Terminal, TerminalId
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import (
    flow_service,
    inbox_service,
    session_service,
    terminal_service,
)
from cli_agent_orchestrator.services.cleanup_service import cleanup_old_data
from cli_agent_orchestrator.services.inbox_service import LogFileHandler
from cli_agent_orchestrator.services.terminal_service import OutputMode
from cli_agent_orchestrator.utils.agent_profiles import resolve_provider
from cli_agent_orchestrator.utils.logging import setup_logging
from cli_agent_orchestrator.utils.terminal import generate_session_name

logger = logging.getLogger(__name__)


async def flow_daemon():
    """Background task to check and execute flows."""
    logger.info("Flow daemon started")
    while True:
        try:
            flows = flow_service.get_flows_to_run()
            for flow in flows:
                try:
                    executed = flow_service.execute_flow(flow.name)
                    if executed:
                        logger.info(f"Flow '{flow.name}' executed successfully")
                    else:
                        logger.info(f"Flow '{flow.name}' skipped (execute=false)")
                except Exception as e:
                    logger.error(f"Flow '{flow.name}' failed: {e}")
        except Exception as e:
            logger.error(f"Flow daemon error: {e}")

        await asyncio.sleep(60)


# Response Models
class TerminalOutputResponse(BaseModel):
    output: str
    mode: str


class WorkingDirectoryResponse(BaseModel):
    """Response model for terminal working directory."""

    working_directory: Optional[str] = Field(
        description="Current working directory of the terminal, or None if unavailable"
    )


class CreateFlowRequest(BaseModel):
    """Request model for creating a flow."""

    name: str
    schedule: str
    agent_profile: str
    provider: str = "kiro_cli"
    prompt_template: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Prevent path traversal — flow name becomes a filename."""
        if "/" in v or "\\" in v or ".." in v:
            raise ValueError("Flow name must not contain '/', '\\', or '..'")
        return v


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    logger.info("Starting CLI Agent Orchestrator server...")
    setup_logging()
    init_db()

    # Run cleanup in background
    asyncio.create_task(asyncio.to_thread(cleanup_old_data))

    # Start flow daemon as background task
    daemon_task = asyncio.create_task(flow_daemon())

    # Start inbox watcher
    inbox_observer = PollingObserver(timeout=INBOX_POLLING_INTERVAL)
    inbox_observer.schedule(LogFileHandler(), str(TERMINAL_LOG_DIR), recursive=False)
    inbox_observer.start()
    logger.info("Inbox watcher started (PollingObserver)")

    yield

    # Stop inbox observer
    inbox_observer.stop()
    inbox_observer.join()
    logger.info("Inbox watcher stopped")

    # Cancel daemon on shutdown
    daemon_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass

    logger.info("Shutting down CLI Agent Orchestrator server...")


app = FastAPI(
    title="CLI Agent Orchestrator",
    description="Simplified CLI Agent Orchestrator API",
    version=SERVER_VERSION,
    lifespan=lifespan,
)

# Security: DNS Rebinding Protection
# Validate Host header to prevent DNS rebinding attacks (CVE mitigation)
# Only allow requests with localhost Host headers
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=ALLOWED_HOSTS,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "cli-agent-orchestrator"}


@app.get("/agents/profiles")
async def list_agent_profiles_endpoint() -> List[Dict]:
    """List all available agent profiles from all configured directories."""
    try:
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        return list_agent_profiles()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list agent profiles: {str(e)}",
        )


@app.get("/agents/providers")
async def list_providers_endpoint() -> List[Dict]:
    """List available providers with installation status."""
    import shutil

    provider_binaries = {
        "kiro_cli": "kiro-cli",
        "claude_code": "claude",
        "q_cli": "q",
        "codex": "codex",
    }
    result = []
    for provider, binary in provider_binaries.items():
        installed = shutil.which(binary) is not None
        result.append({"name": provider, "binary": binary, "installed": installed})
    return result


@app.get("/settings/agent-dirs")
async def get_agent_dirs_endpoint() -> Dict:
    """Get configured agent directories per provider."""
    from cli_agent_orchestrator.services.settings_service import (
        get_agent_dirs,
        get_extra_agent_dirs,
    )

    return {"agent_dirs": get_agent_dirs(), "extra_dirs": get_extra_agent_dirs()}


class AgentDirsUpdate(BaseModel):
    agent_dirs: Optional[Dict[str, str]] = None
    extra_dirs: Optional[List[str]] = None


@app.post("/settings/agent-dirs")
async def set_agent_dirs_endpoint(body: AgentDirsUpdate) -> Dict:
    """Update agent directories per provider."""
    from cli_agent_orchestrator.services.settings_service import (
        get_extra_agent_dirs,
        set_agent_dirs,
        set_extra_agent_dirs,
    )

    result_dirs = {}
    result_extra = []
    if body.agent_dirs:
        result_dirs = set_agent_dirs(body.agent_dirs)
    if body.extra_dirs is not None:
        result_extra = set_extra_agent_dirs(body.extra_dirs)
    return {
        "agent_dirs": result_dirs or {},
        "extra_dirs": result_extra or get_extra_agent_dirs(),
    }


@app.post("/sessions", response_model=Terminal, status_code=status.HTTP_201_CREATED)
async def create_session(
    provider: str,
    agent_profile: str,
    session_name: Optional[str] = None,
    working_directory: Optional[str] = None,
) -> Terminal:
    """Create a new session with exactly one terminal."""
    try:
        result = terminal_service.create_terminal(
            provider=provider,
            agent_profile=agent_profile,
            session_name=session_name,
            new_session=True,
            working_directory=working_directory,
        )
        return result

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create session: {str(e)}",
        )


@app.get("/sessions")
async def list_sessions() -> List[Dict]:
    try:
        return session_service.list_sessions()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list sessions: {str(e)}",
        )


@app.get("/sessions/{session_name}")
async def get_session(session_name: str) -> Dict:
    try:
        return session_service.get_session(session_name)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get session: {str(e)}",
        )


@app.delete("/sessions/{session_name}")
async def delete_session(session_name: str) -> Dict:
    try:
        result = session_service.delete_session(session_name)
        return {"success": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete session: {str(e)}",
        )


@app.post(
    "/sessions/{session_name}/terminals",
    response_model=Terminal,
    status_code=status.HTTP_201_CREATED,
)
async def create_terminal_in_session(
    session_name: str,
    provider: str,
    agent_profile: str,
    working_directory: Optional[str] = None,
) -> Terminal:
    """Create additional terminal in existing session."""
    try:
        resolved_provider = resolve_provider(agent_profile, fallback_provider=provider)

        result = terminal_service.create_terminal(
            provider=resolved_provider,
            agent_profile=agent_profile,
            session_name=session_name,
            new_session=False,
            working_directory=working_directory,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create terminal: {str(e)}",
        )


@app.get("/sessions/{session_name}/terminals")
async def list_terminals_in_session(session_name: str) -> List[Dict]:
    """List all terminals in a session."""
    try:
        from cli_agent_orchestrator.clients.database import list_terminals_by_session

        return list_terminals_by_session(session_name)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list terminals: {str(e)}",
        )


@app.get("/terminals/{terminal_id}", response_model=Terminal)
async def get_terminal(terminal_id: TerminalId) -> Terminal:
    try:
        terminal = terminal_service.get_terminal(terminal_id)
        return Terminal(**terminal)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get terminal: {str(e)}",
        )


@app.get("/terminals/{terminal_id}/working-directory", response_model=WorkingDirectoryResponse)
async def get_terminal_working_directory(terminal_id: TerminalId) -> WorkingDirectoryResponse:
    """Get the current working directory of a terminal's pane."""
    try:
        working_directory = terminal_service.get_working_directory(terminal_id)
        return WorkingDirectoryResponse(working_directory=working_directory)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get working directory: {str(e)}",
        )


@app.post("/terminals/{terminal_id}/input")
async def send_terminal_input(terminal_id: TerminalId, message: str) -> Dict:
    try:
        success = terminal_service.send_input(terminal_id, message)
        return {"success": success}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send input: {str(e)}",
        )


@app.get("/terminals/{terminal_id}/output", response_model=TerminalOutputResponse)
async def get_terminal_output(
    terminal_id: TerminalId, mode: OutputMode = OutputMode.FULL
) -> TerminalOutputResponse:
    try:
        output = terminal_service.get_output(terminal_id, mode)
        return TerminalOutputResponse(output=output, mode=mode)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get output: {str(e)}",
        )


@app.post("/terminals/{terminal_id}/exit")
async def exit_terminal(terminal_id: TerminalId) -> Dict:
    """Send provider-specific exit command to terminal."""
    try:
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            raise ValueError(f"Provider not found for terminal {terminal_id}")
        exit_command = provider.exit_cli()
        # Some providers use tmux key sequences (e.g., "C-d" for Ctrl+D) instead
        # of text commands (e.g., "/exit"). Key sequences must be sent via
        # send_special_key() to be interpreted by tmux, not as literal text.
        if exit_command.startswith(("C-", "M-")):
            terminal_service.send_special_key(terminal_id, exit_command)
        else:
            terminal_service.send_input(terminal_id, exit_command)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to exit terminal: {str(e)}",
        )


@app.delete("/terminals/{terminal_id}")
async def delete_terminal(terminal_id: TerminalId) -> Dict:
    """Delete a terminal."""
    try:
        success = terminal_service.delete_terminal(terminal_id)
        return {"success": success}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete terminal: {str(e)}",
        )


@app.post("/terminals/{receiver_id}/inbox/messages")
async def create_inbox_message_endpoint(
    receiver_id: TerminalId, sender_id: str, message: str
) -> Dict:
    """Create inbox message and attempt immediate delivery."""
    try:
        inbox_msg = create_inbox_message(sender_id, receiver_id, message)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create inbox message: {str(e)}",
        )

    # Best-effort immediate delivery. If the receiver terminal is idle, the
    # message is delivered now; otherwise the watchdog will deliver it when
    # the terminal becomes idle. Delivery failures must not cause the API
    # to report an error — the message was already persisted above.
    try:
        inbox_service.check_and_send_pending_messages(receiver_id)
    except Exception as e:
        logger.warning(f"Immediate delivery attempt failed for {receiver_id}: {e}")

    return {
        "success": True,
        "message_id": inbox_msg.id,
        "sender_id": inbox_msg.sender_id,
        "receiver_id": inbox_msg.receiver_id,
        "created_at": inbox_msg.created_at.isoformat(),
    }


@app.get("/terminals/{terminal_id}/inbox/messages")
async def get_inbox_messages_endpoint(
    terminal_id: TerminalId,
    limit: int = Query(default=10, le=100, description="Maximum number of messages to retrieve"),
    status_param: Optional[str] = Query(
        default=None, alias="status", description="Filter by message status"
    ),
) -> List[Dict]:
    """Get inbox messages for a terminal.

    Args:
        terminal_id: Terminal ID to get messages for
        limit: Maximum number of messages to return (default: 10, max: 100)
        status_param: Optional filter by message status ('pending', 'delivered', 'failed')

    Returns:
        List of inbox messages with sender_id, message, created_at, status
    """
    try:
        # Convert status filter if provided
        status_filter = None
        if status_param:
            try:
                status_filter = MessageStatus(status_param)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status: {status_param}. Valid values: pending, delivered, failed",
                )

        # Get messages using existing database function
        messages = get_inbox_messages(terminal_id, limit=limit, status=status_filter)

        # Convert to response format
        result = []
        for msg in messages:
            result.append(
                {
                    "id": msg.id,
                    "sender_id": msg.sender_id,
                    "receiver_id": msg.receiver_id,
                    "message": msg.message,
                    "status": msg.status.value,
                    "created_at": msg.created_at.isoformat() if msg.created_at else None,
                }
            )

        return result

    except HTTPException:
        # Re-raise HTTPException (validation errors)
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve inbox messages: {str(e)}",
        )


@app.websocket("/terminals/{terminal_id}/ws")
async def terminal_ws(websocket: WebSocket, terminal_id: str):
    """WebSocket endpoint for live terminal streaming via tmux attach.

    Security: This endpoint provides full PTY access with no authentication.
    It is intended for localhost-only use. Do NOT expose the server to
    untrusted networks (e.g. --host 0.0.0.0) without adding authentication.
    """
    # Reject connections from non-loopback clients
    client_host = websocket.client.host if websocket.client else None
    if client_host not in (None, "127.0.0.1", "::1", "localhost"):
        await websocket.close(code=4003, reason="WebSocket access is restricted to localhost")
        return

    await websocket.accept()

    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        await websocket.close(code=4004, reason="Terminal not found")
        return

    session_name = metadata["tmux_session"]
    window_name = metadata["tmux_window"]

    # Create PTY pair for tmux attach
    master_fd, slave_fd = pty.openpty()

    # Set initial terminal size
    winsize = struct.pack("HHHH", 24, 80, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    # Start tmux attach inside the PTY
    proc = subprocess.Popen(
        ["tmux", "attach-session", "-t", f"{session_name}:{window_name}"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        preexec_fn=os.setsid,
    )
    os.close(slave_fd)

    # Make master_fd non-blocking for event-driven reads
    flag = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flag | os.O_NONBLOCK)

    loop = asyncio.get_event_loop()
    output_queue: asyncio.Queue[bytes] = asyncio.Queue()
    done = asyncio.Event()

    def _on_pty_data():
        """Callback when PTY has data available."""
        try:
            data = os.read(master_fd, 65536)
            if data:
                output_queue.put_nowait(data)
            else:
                done.set()
        except BlockingIOError:
            pass
        except OSError:
            done.set()

    loop.add_reader(master_fd, _on_pty_data)

    async def _forward_output():
        """Read from PTY queue and send to WebSocket."""
        while not done.is_set():
            try:
                data = await asyncio.wait_for(output_queue.get(), timeout=1.0)
                # Drain any additional pending data for batching
                while not output_queue.empty():
                    try:
                        data += output_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                await websocket.send_bytes(data)
            except asyncio.TimeoutError:
                if proc.poll() is not None:
                    break
            except Exception:
                break

    async def _forward_input():
        """Receive from WebSocket and write to PTY."""
        try:
            while not done.is_set():
                msg = await websocket.receive_text()
                payload = json.loads(msg)
                if payload.get("type") == "input":
                    os.write(master_fd, payload["data"].encode())
                elif payload.get("type") == "resize":
                    rows = payload.get("rows", 24)
                    cols = payload.get("cols", 80)
                    winsize_data = struct.pack("HHHH", rows, cols, 0, 0)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize_data)
                    # Explicitly notify tmux of the size change —
                    # TIOCSWINSZ on the master doesn't always deliver
                    # SIGWINCH to the child process group.
                    try:
                        os.kill(proc.pid, signal.SIGWINCH)
                    except OSError:
                        pass
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            done.set()

    try:
        await asyncio.gather(_forward_output(), _forward_input())
    finally:
        done.set()
        try:
            loop.remove_reader(master_fd)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        # Terminate tmux attach (just detaches, doesn't kill the session)
        proc.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await asyncio.to_thread(proc.wait)


# ── Flow management endpoints ────────────────────────────────────────


@app.get("/flows", response_model=List[Flow])
async def list_flows() -> List[Flow]:
    """List all flows."""
    try:
        return flow_service.list_flows()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list flows: {str(e)}",
        )


@app.get("/flows/{name}", response_model=Flow)
async def get_flow(name: str) -> Flow:
    """Get a specific flow by name."""
    try:
        return flow_service.get_flow(name)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get flow: {str(e)}",
        )


@app.post("/flows", response_model=Flow, status_code=status.HTTP_201_CREATED)
async def create_flow(body: CreateFlowRequest) -> Flow:
    """Create a new flow.

    Writes a .flow.md file with YAML frontmatter and prompt body, then
    registers it via flow_service.add_flow().
    """
    try:
        flows_dir = CAO_HOME_DIR / "flows"
        flows_dir.mkdir(parents=True, exist_ok=True)

        file_path = flows_dir / f"{body.name}.flow.md"

        # Build YAML frontmatter content
        frontmatter_lines = [
            "---",
            f"name: {body.name}",
            f'schedule: "{body.schedule}"',
            f"agent_profile: {body.agent_profile}",
            f"provider: {body.provider}",
            "---",
        ]
        file_content = "\n".join(frontmatter_lines) + "\n" + body.prompt_template

        file_path.write_text(file_content)

        return flow_service.add_flow(str(file_path))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create flow: {str(e)}",
        )


@app.delete("/flows/{name}")
async def remove_flow(name: str) -> Dict:
    """Remove a flow."""
    try:
        flow_service.remove_flow(name)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove flow: {str(e)}",
        )


@app.post("/flows/{name}/enable")
async def enable_flow(name: str) -> Dict:
    """Enable a flow."""
    try:
        flow_service.enable_flow(name)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to enable flow: {str(e)}",
        )


@app.post("/flows/{name}/disable")
async def disable_flow(name: str) -> Dict:
    """Disable a flow."""
    try:
        flow_service.disable_flow(name)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to disable flow: {str(e)}",
        )


@app.post("/flows/{name}/run")
async def run_flow(name: str) -> Dict:
    """Manually execute a flow."""
    try:
        executed = flow_service.execute_flow(name)
        return {"executed": executed}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to execute flow: {str(e)}",
        )


# =============================================================================
# Beads (Tasks) & Epics
# =============================================================================

from cli_agent_orchestrator.clients.beads_real import BeadsClient, resolve_context_files
from cli_agent_orchestrator.clients.database import set_terminal_bead, get_terminal_by_bead
import asyncio

beads = BeadsClient(working_dir=str(Path.home() / ".beads-planning"))
_assign_locks: Dict[str, asyncio.Lock] = {}


def _get_assign_lock(bead_id: str) -> asyncio.Lock:
    if bead_id not in _assign_locks:
        _assign_locks[bead_id] = asyncio.Lock()
    return _assign_locks[bead_id]


# --- Task CRUD ---

@app.get("/tasks")
async def list_tasks(status_filter: Optional[str] = None, priority: Optional[int] = None) -> List[Dict]:
    tasks = beads.list(status=status_filter, priority=priority)
    return [t.__dict__ for t in tasks]


@app.get("/tasks/next")
async def next_task(priority: Optional[int] = None) -> Dict:
    task = beads.next(priority=priority)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No open tasks")
    return task.__dict__


@app.get("/tasks/{task_id}")
async def get_task(task_id: str) -> Dict:
    task = beads.get(task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task.__dict__


@app.post("/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(title: str, description: str = "", priority: int = 2) -> Dict:
    task = beads.add(title, description, priority)
    return task.__dict__


@app.patch("/tasks/{task_id}")
async def update_task(task_id: str, title: Optional[str] = None, description: Optional[str] = None,
                      priority: Optional[int] = None, assignee: Optional[str] = None) -> Dict:
    updates = {}
    if title is not None: updates["title"] = title
    if description is not None: updates["description"] = description
    if priority is not None: updates["priority"] = priority
    if assignee is not None: updates["assignee"] = assignee
    task = beads.update(task_id, **updates)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task.__dict__


@app.post("/tasks/{task_id}/wip")
async def mark_wip(task_id: str, assignee: Optional[str] = None) -> Dict:
    task = beads.wip(task_id, assignee)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task.__dict__


@app.post("/tasks/{task_id}/close")
async def close_task(task_id: str) -> Dict:
    task = beads.close(task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task.__dict__


@app.delete("/tasks/{task_id}")
async def delete_task(task_id: str) -> Dict:
    success = beads.delete(task_id)
    return {"success": success}


# --- Epics ---

@app.post("/epics", status_code=status.HTTP_201_CREATED)
async def create_epic(title: str, steps: str, description: str = "",
                      priority: int = 2, sequential: bool = True,
                      max_concurrent: int = 3, labels: Optional[str] = None) -> Dict:
    """Create an epic with child beads. Steps is a comma-separated list."""
    step_list = [s.strip() for s in steps.split(",") if s.strip()]
    if not step_list:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one step required")
    label_list = [l.strip() for l in labels.split(",") if l.strip()] if labels else None
    epic = beads.create_epic(title=title, steps=step_list, description=description,
                             priority=priority, sequential=sequential,
                             max_concurrent=max_concurrent, labels=label_list)
    children = beads.get_children(epic.id)
    return {"epic": epic.__dict__, "children": [c.__dict__ for c in children]}


@app.get("/epics/{epic_id}")
async def get_epic(epic_id: str) -> Dict:
    epic = beads.get(epic_id)
    if not epic:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Epic not found")
    children = beads.get_children(epic_id)
    completed = sum(1 for c in children if c.status == "closed")
    wip = sum(1 for c in children if c.status == "wip")
    return {
        "epic": epic.__dict__,
        "children": [c.__dict__ for c in children],
        "progress": {"total": len(children), "completed": completed, "wip": wip,
                     "open": len(children) - completed - wip},
    }


@app.get("/epics/{epic_id}/ready")
async def get_epic_ready(epic_id: str) -> List[Dict]:
    epic = beads.get(epic_id)
    if not epic:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Epic not found")
    return [t.__dict__ for t in beads.ready(parent_id=epic_id)]


# --- Dependencies ---

@app.post("/beads/{bead_id}/dep", status_code=status.HTTP_201_CREATED)
async def add_dependency(bead_id: str, depends_on: str) -> Dict:
    if not beads.add_dependency(bead_id, depends_on):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to add dependency")
    return {"success": True, "bead_id": bead_id, "depends_on": depends_on}


@app.delete("/beads/{bead_id}/dep/{dep_id}")
async def remove_dependency(bead_id: str, dep_id: str) -> Dict:
    if not beads.remove_dependency(bead_id, dep_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to remove dependency")
    return {"success": True}


# --- Children ---

@app.post("/beads/{bead_id}/children", status_code=status.HTTP_201_CREATED)
async def create_child_bead(bead_id: str, title: str, description: str = "", priority: int = 2) -> Dict:
    parent = beads.get(bead_id)
    if not parent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent not found")
    child = beads.create_child(bead_id, title, description, priority)
    return child.__dict__


@app.get("/beads/{bead_id}/children")
async def get_child_beads(bead_id: str) -> List[Dict]:
    return [c.__dict__ for c in beads.get_children(bead_id)]


# --- Bead-Session Wiring ---

@app.get("/beads/{bead_id}/session")
async def get_bead_session(bead_id: str) -> Dict:
    terminal = get_terminal_by_bead(bead_id)
    if not terminal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No session for this bead")
    return terminal


@app.post("/beads/{bead_id}/assign")
async def assign_bead(bead_id: str, session_id: str) -> Dict:
    task = beads.get(bead_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bead not found")
    task = beads.wip(bead_id, session_id)
    try:
        detail = session_service.get_session(session_id)
        if detail.get("terminals"):
            tid = detail["terminals"][0]["id"]
            set_terminal_bead(tid, bead_id)
            terminal_service.send_input(tid, f"Work on this task: {task.title}\n\n{task.description}")
    except Exception:
        pass
    return task.__dict__


@app.post("/beads/{bead_id}/assign-agent")
async def assign_bead_to_agent(bead_id: str, agent_name: str, provider: str = "claude_code") -> Dict:
    async with _get_assign_lock(bead_id):
        task = beads.get(bead_id)
        if not task:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bead not found")
        if task.assignee:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"Bead already assigned to {task.assignee}")
        try:
            terminal = terminal_service.create_terminal(
                provider=provider, agent_profile=agent_name, new_session=True, bead_id=bead_id,
            )
            session_id = terminal.session_name
            task = beads.wip(bead_id, session_id)
            prompt = f"Work on this task: {task.title}\n\n{task.description}" if task.description else f"Work on this task: {task.title}"
            terminal_service.send_input(terminal.id, prompt)
            return {"task": task.__dict__, "session_id": session_id, "terminal_id": terminal.id}
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=f"Failed to assign: {str(e)}")


# =============================================================================
# Master Orchestrator
# =============================================================================

# Track the active orchestrator session (in-memory, one per server)
_orchestrator_session_id: Optional[str] = None


def _find_existing_orchestrator() -> Optional[str]:
    """Scan all live sessions for an existing master_orchestrator.

    This handles the case where the server restarts but an orchestrator
    session is still running in tmux from before. Returns the session_id
    if found, None otherwise.
    """
    try:
        sessions = session_service.list_sessions()
        for session in sessions:
            try:
                detail = session_service.get_session(session["id"])
                for terminal in detail.get("terminals", []):
                    if terminal.get("agent_profile") == "master_orchestrator":
                        return session["id"]
            except Exception:
                continue
    except Exception:
        pass
    return None


@app.post("/orchestrator/launch", status_code=status.HTTP_201_CREATED)
async def launch_orchestrator(
    provider: str = "claude_code",
    agent_profile: str = "master_orchestrator",
) -> Dict:
    """Launch the master orchestrator as a persistent session.

    The orchestrator is a CLI agent with full CAO MCP tools that can manage
    sessions, flows, agents, and coordinate work. Only one can run at a time.

    If an orchestrator session already exists (even from before a server restart),
    it will be detected and reconnected automatically.
    """
    global _orchestrator_session_id

    # Check our in-memory tracker first
    if _orchestrator_session_id:
        try:
            session_service.get_session(_orchestrator_session_id)
            return {
                "session_id": _orchestrator_session_id,
                "status": "already_running",
                "message": "Orchestrator is already running",
            }
        except (ValueError, Exception):
            _orchestrator_session_id = None  # stale reference

    # Scan live sessions for an orphaned orchestrator (survives server restart)
    existing = _find_existing_orchestrator()
    if existing:
        _orchestrator_session_id = existing
        return {
            "session_id": existing,
            "status": "reconnected",
            "message": "Reconnected to existing orchestrator session",
        }

    # No existing orchestrator — launch a new one
    try:
        result = terminal_service.create_terminal(
            provider=provider,
            agent_profile=agent_profile,
            new_session=True,
        )
        _orchestrator_session_id = result.session_name
        return {
            "session_id": result.session_name,
            "terminal_id": result.id,
            "agent_profile": agent_profile,
            "provider": provider,
            "status": "launched",
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to launch orchestrator: {str(e)}",
        )


@app.get("/orchestrator/status")
async def orchestrator_status() -> Dict:
    """Check if the master orchestrator is running."""
    global _orchestrator_session_id

    # Check in-memory tracker
    if _orchestrator_session_id:
        try:
            session_service.get_session(_orchestrator_session_id)
            return {"running": True, "session_id": _orchestrator_session_id}
        except (ValueError, Exception):
            _orchestrator_session_id = None

    # Fallback: scan for orphaned orchestrator
    existing = _find_existing_orchestrator()
    if existing:
        _orchestrator_session_id = existing
        return {"running": True, "session_id": existing}

    return {"running": False, "session_id": None}


@app.delete("/orchestrator/stop")
async def stop_orchestrator() -> Dict:
    """Stop the master orchestrator session.

    Also scans for orphaned orchestrator sessions and cleans them up.
    """
    global _orchestrator_session_id

    stopped = []

    # Stop tracked session
    if _orchestrator_session_id:
        try:
            session_service.delete_session(_orchestrator_session_id)
            stopped.append(_orchestrator_session_id)
        except Exception:
            pass
        _orchestrator_session_id = None

    # Also clean up any orphaned orchestrator sessions
    existing = _find_existing_orchestrator()
    while existing:
        try:
            session_service.delete_session(existing)
            stopped.append(existing)
        except Exception:
            break
        existing = _find_existing_orchestrator()

    if stopped:
        return {"success": True, "stopped": stopped, "message": f"Stopped {len(stopped)} orchestrator session(s)"}
    return {"success": True, "message": "No orchestrator running"}


# Static file serving for built web UI
WEB_DIST = Path(__file__).parent.parent.parent.parent / "web" / "dist"
if WEB_DIST.exists():
    from starlette.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")


def main():
    """Entry point for cao-server command."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="CLI Agent Orchestrator Server")
    parser.add_argument(
        "--agents-dir",
        type=str,
        default=None,
        help="Path to agents directory (overrides CAO_AGENTS_DIR env var)",
    )
    parser.add_argument("--host", type=str, default=None, help="Server host")
    parser.add_argument("--port", type=int, default=None, help="Server port")
    args = parser.parse_args()

    if args.agents_dir:
        os.environ["CAO_AGENTS_DIR"] = args.agents_dir
        import cli_agent_orchestrator.constants as constants

        constants.KIRO_AGENTS_DIR = Path(args.agents_dir)
        logger.info(f"Using agents directory: {args.agents_dir}")

    host = args.host or SERVER_HOST
    port = args.port or SERVER_PORT
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
