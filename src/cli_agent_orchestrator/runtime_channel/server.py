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
from typing import Any, Dict, List, Optional, Set

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field, ValidationError

from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import (
    get_terminal_metadata,
    list_terminal_ids_on_runtime,
    list_terminals_by_session,
)
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import Terminal, TerminalId, TerminalStatus
from cli_agent_orchestrator.plugins import PostCreateSessionEvent, PostCreateTerminalEvent
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
    RuntimeConnection,
    RuntimeUnavailableError,
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
from cli_agent_orchestrator.services.plugin_dispatch import dispatch_plugin_event
from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock

logger = logging.getLogger(__name__)

router = APIRouter()

# Strong references to teardown tasks: the event loop keeps only weak ones.
_teardowns: Set["asyncio.Task[None]"] = set()


def _token_matches(presented: str) -> bool:
    expected = runtime_token()
    if not expected:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())


def _delete_unrecorded(conn: RuntimeConnection, terminal_id: str) -> None:
    """Delete, in its runtime, a terminal the server has no record of.

    That is a launch whose result was lost (or arrived after the server gave
    up), so nothing else would ever stop it.
    """

    async def run() -> None:
        logger.warning(
            "runtime %s runs terminal %s, which this server never recorded; deleting it",
            conn.runtime_id,
            terminal_id,
        )
        try:
            await conn.call(CommandType.DELETE, {}, terminal_id=terminal_id)
        except RemoteRuntimeError as exc:
            logger.warning("could not delete unrecorded terminal %s: %s", terminal_id, exc)

    task = asyncio.create_task(run())
    _teardowns.add(task)
    task.add_done_callback(_teardowns.discard)


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
        recorded = await asyncio.to_thread(list_terminal_ids_on_runtime, runtime_id)
        for terminal_id in recorded:
            runtime_registry.place(terminal_id, runtime_id)
        # A delete may have dropped a row after that read and before its
        # placement: undo the placement of any row that is gone now.
        still_recorded = set(await asyncio.to_thread(list_terminal_ids_on_runtime, runtime_id))
        for terminal_id in recorded:
            if terminal_id not in still_recorded:
                runtime_registry.unplace(terminal_id, runtime_id)
        for terminal_id, reported in hello.statuses.items():
            runtime_registry.set_status(terminal_id, runtime_id, reported, conn=conn)
        await ws.send_text(server_hello)
        runtime_registry.activate(conn)
        # The hello lists every terminal the runtime runs.
        for terminal_id in hello.statuses:
            if not runtime_registry.is_known(terminal_id, runtime_id):
                _delete_unrecorded(conn, terminal_id)

        while True:
            frame = decode(await ws.receive_text())
            if conn.closed:
                # A newer connection for this runtime replaced this one.
                await ws.close(code=status.WS_1000_NORMAL_CLOSURE, reason="replaced")
                break
            if isinstance(frame, Result):
                launched = frame.payload.get("terminal") if frame.ok else None
                launched_id = launched.get("id") if isinstance(launched, dict) else None
                if not isinstance(launched_id, str):
                    launched_id = None
                if launched_id and conn.awaits(frame.op_id):
                    # Reserved before its caller resumes, so a reconnect's hello
                    # in the meantime does not take it for an unrecorded one.
                    runtime_registry.reserve(launched_id, runtime_id)
                if (
                    not conn.resolve(frame)
                    and launched_id
                    and not runtime_registry.is_known(launched_id, runtime_id)
                ):
                    # A launch that finished after the server gave up on it.
                    _delete_unrecorded(conn, launched_id)
            elif isinstance(frame, Status):
                if runtime_registry.set_status(
                    frame.terminal_id, runtime_id, frame.status, conn=conn
                ):
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


class _Launched(BaseModel):
    """The terminal a runtime reports it launched: checked before it is recorded."""

    id: TerminalId
    name: str = Field(min_length=1)
    provider: ProviderType
    session_name: str = Field(min_length=1)
    agent_profile: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    status: Optional[TerminalStatus] = None


async def _undo_launch(runtime_id: str, terminal_id: str) -> bool:
    """Delete a launched terminal the server will not record. True if it is gone."""
    try:
        deleted = await runtime_registry.call(
            runtime_id, CommandType.DELETE, {}, terminal_id=terminal_id
        )
        return bool(deleted.get("deleted"))
    except RemoteRuntimeError as exc:
        logger.warning(
            "could not delete terminal %s on runtime %s: %s", terminal_id, runtime_id, exc
        )
        return False


def _record_launch(
    runtime_id: str, launched: _Launched, working_directory: Optional[str]
) -> Optional[str]:
    """Write the central row for a launch. Returns the conflict instead, if its id or
    its session name is already in use: on another runtime, or on this server."""
    # The lock local creation and teardown take for a session name, so the
    # check and the row write are one step for every creator of that name.
    with session_lifecycle_lock(launched.session_name):
        if get_terminal_metadata(launched.id) is not None:
            return f"terminal id {launched.id} is already in use"
        if list_terminals_by_session(launched.session_name):
            return f"session {launched.session_name} already exists"
        # Placed before the row is written, so a reconnect in between does not
        # take the terminal for an unrecorded one.
        runtime_registry.place(launched.id, runtime_id)
        try:
            db_create_terminal(
                launched.id,
                launched.session_name,
                launched.name,
                launched.provider.value,
                agent_profile=launched.agent_profile,
                allowed_tools=launched.allowed_tools,
                working_directory=working_directory,
                runtime_id=runtime_id,
            )
        except Exception:
            runtime_registry.forget(launched.id)
            raise
    return None


async def _finish_launch(
    request: Request,
    runtime_id: str,
    body: LaunchRequest,
    conn: Optional[RuntimeConnection],
    raw: Any,
) -> Dict[str, Any]:
    """Validate and record a launch result; undo the launch if it cannot be recorded."""
    from cli_agent_orchestrator.services import terminal_service

    try:
        launched = _Launched.model_validate(raw)
    except ValidationError as exc:
        # Nothing here may be trusted except, perhaps, the id: use it to stop
        # the agent the runtime may have started, and record nothing.
        named = raw.get("id") if isinstance(raw, dict) else None
        detail = f"runtime {runtime_id} returned an invalid launch result"
        if isinstance(named, str) and named:
            cleaned = await _undo_launch(runtime_id, named)
            detail += f" for terminal {named}; " + (
                "it was deleted" if cleaned else "it may still be running there"
            )
        else:
            detail += "; an agent may still be running there"
        logger.warning("%s: %s", detail, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc

    terminal_id = launched.id
    try:
        conflict = await asyncio.to_thread(
            _record_launch, runtime_id, launched, body.working_directory
        )
    except Exception as exc:  # noqa: BLE001
        # The agent is running with no central row: tear it down so nothing is
        # left running that the server cannot see.
        logger.exception("could not record terminal %s from runtime %s", terminal_id, runtime_id)
        cleaned = await _undo_launch(runtime_id, terminal_id)
        detail = f"launched terminal {terminal_id} on runtime {runtime_id} but could not record it"
        if not cleaned:
            detail += "; it may still be running there"
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail
        ) from exc
    if conflict:
        cleaned = await _undo_launch(runtime_id, terminal_id)
        detail = f"runtime {runtime_id} launched terminal {terminal_id}, but {conflict}; " + (
            "it was deleted" if cleaned else "it may still be running there"
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    if launched.status:
        # Ignored if the runtime reconnected meanwhile: its new hello is newer.
        runtime_registry.set_status(terminal_id, runtime_id, launched.status, conn=conn)
    # The central lifecycle events, as a local launch in a new session emits
    # them: the runtime starts its agent with no plugin registry of its own.
    plugins = getattr(request.app.state, "plugin_registry", None)
    dispatch_plugin_event(
        plugins,
        "post_create_terminal",
        PostCreateTerminalEvent(
            session_id=launched.session_name,
            terminal_id=terminal_id,
            agent_name=launched.agent_profile,
            provider=launched.provider.value,
        ),
    )
    dispatch_plugin_event(
        plugins,
        "post_create_session",
        PostCreateSessionEvent(
            session_id=launched.session_name, session_name=launched.session_name
        ),
    )
    return await asyncio.to_thread(terminal_service.get_terminal, terminal_id)


@router.post(
    "/runtimes/{runtime_id}/terminals",
    response_model=Terminal,
    status_code=status.HTTP_201_CREATED,
)
async def launch_on_runtime(
    runtime_id: str,
    body: LaunchRequest,
    request: Request,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Launch a terminal in a connected execution runtime."""
    conn = runtime_registry.connection(runtime_id)
    try:
        if conn is None:
            raise RuntimeUnavailableError(f"runtime {runtime_id} is not connected")
        result = await conn.call(
            CommandType.LAUNCH, body.model_dump(exclude_none=True), timeout=LAUNCH_TIMEOUT
        )
    except RemoteRuntimeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))

    raw = result.get("terminal")
    reserved = raw.get("id") if isinstance(raw, dict) else None
    try:
        return await _finish_launch(request, runtime_id, body, conn, raw)
    finally:
        # Recorded (and so placed) or undone: either way no longer in flight.
        if isinstance(reserved, str):
            runtime_registry.release(reserved, runtime_id)


@router.get("/runtimes")
async def list_runtimes(
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    return {"runtimes": runtime_registry.list_runtimes()}
