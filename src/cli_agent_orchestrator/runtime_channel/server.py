"""cao-server's side of the runtime channel (#745).

- ``WS /runtime/channel``: each execution runtime dials in with the shared token
  in the ``x-cao-runtime-token`` header. With no token configured on the server,
  every connection is refused.
- ``POST /runtimes/{runtime_id}/terminals``: launch a terminal in a connected
  runtime. The runtime starts the agent beside its own tmux; the server writes
  the central row with the runtime's id.
- ``GET /runtimes``: the connected runtimes and the terminals placed on them.
"""

import asyncio
import hmac
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ValidationError

from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import list_terminal_ids_on_runtime
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    CommandType,
    Hello,
    Result,
    Status,
    decode,
    encode,
)
from cli_agent_orchestrator.runtime_channel.registry import (
    LAUNCH_TIMEOUT,
    RemoteRuntimeError,
    runtime_registry,
)
from cli_agent_orchestrator.runtime_channel.token import TOKEN_HEADER, runtime_token
from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    require_any_scope,
)
from cli_agent_orchestrator.services.event_bus import bus

logger = logging.getLogger(__name__)

router = APIRouter()


def _token_matches(presented: str) -> bool:
    expected = runtime_token()
    if not expected:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())


@router.websocket("/runtime/channel")
async def runtime_channel(ws: WebSocket) -> None:
    if not _token_matches(ws.headers.get(TOKEN_HEADER, "")):
        # Closing before accept fails the handshake (HTTP 403).
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()

    try:
        hello = decode(await ws.receive_text())
    except (ValidationError, WebSocketDisconnect):
        await ws.close(code=status.WS_1002_PROTOCOL_ERROR)
        return
    server_hello = encode(Hello(protocol_version=PROTOCOL_VERSION, runtime_id="server"))
    if not isinstance(hello, Hello) or hello.protocol_version != PROTOCOL_VERSION:
        # Tell the peer which version this server speaks, then refuse it.
        await ws.send_text(server_hello)
        await ws.close(code=status.WS_1002_PROTOCOL_ERROR)
        return

    runtime_id = hello.runtime_id
    conn = runtime_registry.register(runtime_id, ws.send_text)
    try:
        # Terminals whose central row names this runtime are its to report on;
        # a status for any other terminal is ignored.
        for terminal_id in await asyncio.to_thread(list_terminal_ids_on_runtime, runtime_id):
            runtime_registry.place(terminal_id, runtime_id)
        for terminal_id, reported in hello.statuses.items():
            runtime_registry.set_status(terminal_id, runtime_id, reported)
        await ws.send_text(server_hello)

        while True:
            frame = decode(await ws.receive_text())
            if isinstance(frame, Result):
                conn.resolve(frame)
            elif isinstance(frame, Status):
                if runtime_registry.set_status(frame.terminal_id, runtime_id, frame.status):
                    bus.publish(
                        f"terminal.{frame.terminal_id}.status", {"status": frame.status.value}
                    )
                else:
                    logger.warning(
                        "ignoring status from runtime %s for terminal %s, which it does not run",
                        runtime_id,
                        frame.terminal_id,
                    )
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - one bad channel must not take down the server
        logger.exception("runtime channel error for %s", runtime_id)
    finally:
        runtime_registry.unregister(runtime_id, conn)


class LaunchRequest(BaseModel):
    agent_profile: str
    provider: Optional[str] = None
    working_directory: Optional[str] = None


@router.post(
    "/runtimes/{runtime_id}/terminals",
    response_model=Terminal,
    status_code=status.HTTP_201_CREATED,
)
async def launch_on_runtime(
    runtime_id: str,
    body: LaunchRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Launch a terminal in a connected execution runtime."""
    from cli_agent_orchestrator.services import terminal_service

    try:
        result = await runtime_registry.call(
            runtime_id,
            CommandType.LAUNCH,
            body.model_dump(exclude_none=True),
            timeout=LAUNCH_TIMEOUT,
        )
    except RemoteRuntimeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))

    info = result["terminal"]
    terminal_id = info["id"]
    try:
        await asyncio.to_thread(
            db_create_terminal,
            terminal_id,
            info["session_name"],
            info["name"],
            info["provider"],
            agent_profile=info.get("agent_profile"),
            allowed_tools=info.get("allowed_tools"),
            working_directory=body.working_directory,
            runtime_id=runtime_id,
        )
    except Exception as exc:  # noqa: BLE001
        # The agent is running with no central row: tear it down so nothing is
        # left running that the server cannot see.
        logger.exception("could not record terminal %s from runtime %s", terminal_id, runtime_id)
        cleaned = False
        try:
            deleted = await runtime_registry.call(
                runtime_id, CommandType.DELETE, {}, terminal_id=terminal_id
            )
            cleaned = bool(deleted.get("deleted"))
        except RemoteRuntimeError:
            pass
        detail = f"launched terminal {terminal_id} on runtime {runtime_id} but could not record it"
        if not cleaned:
            detail += "; it may still be running there"
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail
        ) from exc

    runtime_registry.place(terminal_id, runtime_id)
    reported = info.get("status")
    if reported:
        runtime_registry.set_status(terminal_id, runtime_id, TerminalStatus(reported))
    return await asyncio.to_thread(terminal_service.get_terminal, terminal_id)


@router.get("/runtimes")
async def list_runtimes(
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    return {"runtimes": runtime_registry.list_runtimes()}
