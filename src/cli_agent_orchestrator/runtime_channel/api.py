"""Server-side runtime channel endpoints (#745).

Mounted onto the existing cao-server app — additive, opt-in surface:

- ``WS /runtime/channel``: the persistent outbound connection each execution
  runtime (cao-bridge) dials. Authenticated with a shared runtime token that
  FAILS CLOSED: when ``CAO_RUNTIME_TOKEN`` is not configured on the server,
  every runtime connection is rejected, so a purely local installation
  exposes no anonymous execution channel. (#774 replaces this shared token
  with per-runtime delegated credentials.)
- ``POST /runtimes/{runtime_id}/terminals``: create a terminal on a connected
  runtime. The runtime executes the full local launch sequence beside its own
  tmux; the server persists the authoritative registry row and routes all
  later operations for that terminal over the channel.
- ``GET /runtimes``: operator visibility into connected runtimes.
"""

import asyncio
import base64
import hmac
import logging
import os
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict

from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import delete_terminal as db_delete_terminal
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    AckFrame,
    CommandOutcome,
    CommandResultFrame,
    CommandType,
    EventFrame,
    EventType,
    GapFrame,
    HeartbeatFrame,
    HelloFrame,
    StreamFrame,
    StreamName,
    StreamPosition,
    decode_frame,
    encode_frame,
)
from cli_agent_orchestrator.runtime_channel.registry import (
    EXTRACT_TIMEOUT,
    INPUT_TIMEOUT,
    LAUNCH_TIMEOUT,
    TEARDOWN_TIMEOUT,
    RemoteCommandError,
    RuntimeUnavailableError,
    runtime_registry,
)
from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    get_current_principal,
    require_any_scope,
)
from cli_agent_orchestrator.security.principal import Principal
from cli_agent_orchestrator.services.event_bus import bus

logger = logging.getLogger(__name__)

router = APIRouter()

RUNTIME_TOKEN_HEADER = "x-cao-runtime-token"
RUNTIME_TOKEN_ENV = "CAO_RUNTIME_TOKEN"

# Re-exported from the registry, which owns them now that non-HTTP senders
# share the same deadlines. Kept as module attributes because call sites
# reference them as runtime_channel_api.<NAME>.
__all__ = [
    "router",
    "LAUNCH_TIMEOUT",
    "INPUT_TIMEOUT",
    "EXTRACT_TIMEOUT",
    "TEARDOWN_TIMEOUT",
    "remote_terminal_command",
    "remote_delete_terminal",
    "launch_remote_terminal",
    "relay_remote_attach",
]


def _expected_token() -> Optional[str]:
    token = os.environ.get(RUNTIME_TOKEN_ENV, "").strip()
    return token or None


@router.websocket("/runtime/channel")
async def runtime_channel(ws: WebSocket) -> None:
    expected = _expected_token()
    presented = ws.headers.get(RUNTIME_TOKEN_HEADER, "")
    if expected is None or not hmac.compare_digest(presented, expected):
        # Rejecting before accept() fails the WebSocket handshake with 403,
        # which the bridge treats as a fatal auth error, not a retry case.
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()

    try:
        hello = decode_frame(await ws.receive_text())
    except Exception:
        await ws.close(code=status.WS_1002_PROTOCOL_ERROR)
        return
    if not isinstance(hello, HelloFrame):
        await ws.close(code=status.WS_1002_PROTOCOL_ERROR)
        return
    if hello.protocol_version != PROTOCOL_VERSION:
        # Version mismatch must fail before any work is accepted (#745).
        await ws.send_text(
            encode_frame(HelloFrame(protocol_version=PROTOCOL_VERSION, runtime_id="server"))
        )
        await ws.close(code=status.WS_1002_PROTOCOL_ERROR)
        return

    runtime_id = hello.runtime_id
    conn = runtime_registry.register(runtime_id, ws.send_text)

    # Rebind routing from the hello snapshot: this is how a restarted server
    # (or a reconnecting runtime) recovers terminal→runtime associations
    # without persisting live channel state.
    resume: List[StreamPosition] = []
    for stream_pos in hello.streams:
        runtime_registry.bind_terminal(stream_pos.terminal_id, runtime_id)
        resume.append(
            StreamPosition(
                terminal_id=stream_pos.terminal_id,
                stream=stream_pos.stream,
                generation=stream_pos.generation,
                end_pos=runtime_registry.resume_position(
                    stream_pos.terminal_id, stream_pos.stream.value
                ),
            )
        )
    # Seed the status cache from the same snapshot. Status is pushed on change,
    # so without this a server that restarted while a terminal sat quiescent
    # would answer UNKNOWN until the agent next moved — indefinitely, for an
    # idle agent. Only terminals this hello actually bound are seeded.
    for terminal_id, reported in hello.statuses.items():
        if runtime_registry.runtime_for_terminal(terminal_id) == runtime_id:
            runtime_registry.set_status(terminal_id, reported)

    await ws.send_text(
        encode_frame(
            HelloFrame(protocol_version=PROTOCOL_VERSION, runtime_id="server", resume=resume)
        )
    )

    try:
        while True:
            frame = decode_frame(await ws.receive_text())
            conn.last_seen = time.time()
            if isinstance(frame, CommandResultFrame):
                conn.resolve(frame)
                # Ack unconditionally so the runtime can drop its retained
                # copy — even for results this process never asked for (they
                # were retained for a server that has since restarted).
                await ws.send_text(encode_frame(AckFrame(op_id=frame.op_id)))
            elif isinstance(frame, StreamFrame):
                raw = base64.b64decode(frame.data)
                # A terminal streaming through this channel is de facto
                # executed by this runtime — keep routing bound even if the
                # hello snapshot predated the terminal's creation.
                runtime_registry.bind_terminal(frame.terminal_id, runtime_id)
                if frame.stream == StreamName.ATTACH:
                    # Interactive bytes go to the live attach client, never
                    # the bus; an empty frame is the runtime PTY's EOF.
                    runtime_registry.deliver_attach(frame.terminal_id, raw if raw else None)
                    continue
                runtime_registry.record_position(
                    frame.terminal_id, frame.stream.value, frame.pos + len(raw)
                )
                # Republish onto the existing in-process bus with the exact
                # payload shape the local FIFO reader uses, so bus-contract
                # consumers (LogWriter, AG-UI, inbox) work unchanged.
                bus.publish(
                    f"terminal.{frame.terminal_id}.output",
                    {"data": raw.decode("utf-8", errors="replace")},
                )
            elif isinstance(frame, GapFrame):
                logger.warning(
                    "output gap for remote terminal %s [%s, %s)",
                    frame.terminal_id,
                    frame.from_pos,
                    frame.to_pos,
                )
                bus.publish(
                    f"terminal.{frame.terminal_id}.output",
                    {"data": "", "gap": {"from_pos": frame.from_pos, "to_pos": frame.to_pos}},
                )
            elif isinstance(frame, EventFrame):
                runtime_registry.bind_terminal(frame.terminal_id, runtime_id)
                if frame.type == EventType.STATUS and frame.status is not None:
                    runtime_registry.set_status(frame.terminal_id, frame.status)
                    bus.publish(
                        f"terminal.{frame.terminal_id}.status", {"status": frame.status.value}
                    )
            elif isinstance(frame, HeartbeatFrame):
                for stream_pos in frame.streams:
                    runtime_registry.bind_terminal(stream_pos.terminal_id, runtime_id)
            else:
                logger.warning("unexpected frame kind from runtime %s: %s", runtime_id, frame.kind)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("runtime channel error for %s", runtime_id)
    finally:
        runtime_registry.unregister(runtime_id, conn)


class CreateRemoteTerminalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    agent_profile: str
    session_name: Optional[str] = None
    working_directory: Optional[str] = None
    env_vars: Optional[Dict[str, str]] = None
    model: Optional[str] = None
    initial_message: Optional[str] = None
    # An in-session assign/handoff by an agent that executes in a runtime is
    # forwarded here by POST /sessions/{name}/terminals, so the fields that
    # request carries have to survive the hop: the worker joins the caller's
    # existing session in that runtime (``new_session`` false), remembers which
    # terminal asked for it (``caller_id``, what send_message routes callbacks
    # by), and keeps assign's deferred-init contract.
    new_session: bool = True
    caller_id: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    defer_init: bool = False
    initial_message_orchestration_type: Optional[str] = None
    engine: Optional[str] = None
    # The worktree is provisioned by the runtime, in the filesystem the agent
    # will actually run in — the central container's disk is not that workspace.
    use_worktree: bool = False


@router.get("/runtimes")
async def list_runtimes(
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    return {"runtimes": runtime_registry.list_runtimes()}


@router.post("/runtimes/{runtime_id}/terminals", response_model=Terminal)
async def create_remote_terminal(
    runtime_id: str,
    body: CreateRemoteTerminalBody,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
    principal: Principal = Depends(get_current_principal),
) -> Terminal:
    return await launch_remote_terminal(runtime_id, body, owner_id=principal.id)


async def launch_remote_terminal(
    runtime_id: str,
    body: CreateRemoteTerminalBody,
    owner_id: Optional[str],
) -> Terminal:
    """Launch one terminal in a runtime and record it centrally (#745).

    Extracted from the route so the scheduler can reach it: a flow firing in the
    server container must place its agent in an execution runtime too, and it has
    no request to carry a principal — it passes the flow's stored owner instead.
    Everything the route did (registry row, runtime binding, reported status)
    happens here, in one place, because a second launch path that forgot one of
    them would leave a terminal nobody can route to.
    """
    conn = runtime_registry.get_runtime(runtime_id)
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"runtime '{runtime_id}' is not connected",
        )
    try:
        result = await conn.send_command(
            CommandType.LAUNCH, body.model_dump(exclude_none=True), timeout=LAUNCH_TIMEOUT
        )
    except RuntimeUnavailableError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except TimeoutError:
        # The response is missing; the launch may or may not have happened.
        # Report unknown rather than retrying into a duplicate worker (#745).
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"launch on runtime '{runtime_id}' timed out; outcome unknown",
        )
    if result.outcome != CommandOutcome.OK:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"launch failed on runtime '{runtime_id}': "
            f"{result.payload.get('error', result.outcome.value)}",
        )

    info = result.payload["terminal"]
    # Persist the authoritative registry row centrally. runtime_id is recorded
    # in metadata so the association is inspectable and survives restarts
    # alongside the hello-snapshot rebinding.
    #
    # ``owner`` records who this remote terminal's work is for (#745). It is
    # written here, on the server, and deliberately NOT sent to the runtime in
    # the LAUNCH payload: the executor pod is the least trusted party in this
    # topology, and an identity handed to it is an identity it could re-present.
    # Every later question about this terminal ("may this still start work?",
    # "whose callback is this?") is answered by reading this row, which is the
    # concrete form of #745's rule that agent-supplied IDs alone are not
    # authorization.
    db_create_terminal(
        info["id"],
        info["session_name"],
        info["name"],
        info["provider"],
        agent_profile=info.get("agent_profile"),
        allowed_tools=info.get("allowed_tools"),
        shell_command=info.get("shell_command"),
        caller_id=body.caller_id,
        working_directory=body.working_directory,
        metadata={"runtime_id": runtime_id},
        owner=owner_id,
    )
    runtime_registry.bind_terminal(info["id"], runtime_id)
    try:
        reported = TerminalStatus(info.get("status", "unknown"))
    except ValueError:
        reported = TerminalStatus.UNKNOWN
    runtime_registry.set_status(info["id"], reported)

    from datetime import datetime

    return Terminal(
        id=info["id"],
        name=info["name"],
        provider=info["provider"],
        session_name=info["session_name"],
        agent_profile=info.get("agent_profile"),
        caller_id=body.caller_id,
        allowed_tools=info.get("allowed_tools"),
        engine=None,
        shell_command=info.get("shell_command"),
        group=None,
        metadata={"runtime_id": runtime_id},
        owner=owner_id,
        status=reported,
        last_active=datetime.now(),
    )


async def remote_terminal_command(
    terminal_id: str,
    command_type: CommandType,
    payload: dict,
    timeout: float,
) -> CommandResultFrame:
    """Route one terminal-scoped operation to its runtime, mapping transport
    and execution failures onto the HTTP semantics the existing endpoints use."""
    try:
        result = await runtime_registry.send_terminal_command(
            terminal_id, command_type, payload, timeout=timeout
        )
    except RuntimeUnavailableError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"remote operation on terminal '{terminal_id}' timed out; outcome unknown",
        )
    if result.outcome != CommandOutcome.OK:
        raise RemoteCommandError(
            result.outcome.value,
            result.payload.get("error", f"remote operation failed: {result.outcome.value}"),
        )
    return result


async def relay_remote_attach(websocket, terminal_id: str) -> None:
    """Relay the browser/native WS attach protocol to a remote runtime (#776).

    Client-facing contract is IDENTICAL to the local PTY path in
    ``api/main.py``: binary frames carry terminal bytes down; JSON text frames
    ``{"type": "input"|"resize", ...}`` come up. Runtime-facing: ATTACH_OPEN /
    ATTACH_DATA / RESIZE / ATTACH_CLOSE commands, and attach-stream frames
    routed to this socket via the registry sink. The PTY subprocess lives in
    the runtime pod, beside the tmux socket.

    Caller has already ACCEPTED the websocket and enforced IP/Origin/auth.
    """
    import base64 as _b64
    import json as _json

    from starlette.websockets import WebSocketDisconnect as _WSDisconnect

    sink: asyncio.Queue = asyncio.Queue()
    runtime_registry.bind_attach(terminal_id, sink)
    try:
        try:
            result = await runtime_registry.send_terminal_command(
                terminal_id, CommandType.ATTACH_OPEN, {"rows": 24, "cols": 80}, timeout=30.0
            )
        except (RuntimeUnavailableError, TimeoutError) as exc:
            await websocket.close(code=4010, reason=f"remote attach failed: {exc}")
            return
        if result.outcome != CommandOutcome.OK or not result.payload.get("opened", False):
            await websocket.close(code=4010, reason="remote attach failed to open")
            return

        async def _downstream():
            while True:
                data = await sink.get()
                if data is None:
                    break
                await websocket.send_bytes(data)

        async def _upstream():
            while True:
                msg = await websocket.receive_text()
                payload = _json.loads(msg)
                if payload.get("type") == "input":
                    await runtime_registry.send_terminal_command(
                        terminal_id,
                        CommandType.ATTACH_DATA,
                        {"data": _b64.b64encode(payload["data"].encode()).decode()},
                        timeout=INPUT_TIMEOUT,
                    )
                elif payload.get("type") == "resize":
                    await runtime_registry.send_terminal_command(
                        terminal_id,
                        CommandType.RESIZE,
                        {"rows": payload.get("rows", 24), "cols": payload.get("cols", 80)},
                        timeout=INPUT_TIMEOUT,
                    )

        down = asyncio.create_task(_downstream())
        up = asyncio.create_task(_upstream())
        try:
            done, pending = await asyncio.wait({down, up}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, (_WSDisconnect, RuntimeError)):
                    logger.warning("remote attach relay error for %s: %s", terminal_id, exc)
        finally:
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001 — already closed is fine
                pass
    finally:
        runtime_registry.unbind_attach(terminal_id, sink)
        try:
            await runtime_registry.send_terminal_command(
                terminal_id, CommandType.ATTACH_CLOSE, {}, timeout=10.0
            )
        except Exception:  # noqa: BLE001 — best-effort close on a dead runtime
            pass


async def remote_delete_terminal(terminal_id: str) -> bool:
    """TEARDOWN on the runtime, then drop the central row and routing.

    A runtime that reports the terminal ``absent`` settles this too: the pod that
    held the session was replaced, so there is nothing left to kill and the
    central row points at a session its own runtime says does not exist. Keeping
    that row would leave the id permanently untearable and, for a caller that
    recycles before launching, permanently blocked. A teardown whose outcome is
    merely unknown never reaches here — ``remote_terminal_command`` raises.
    """
    result = await remote_terminal_command(
        terminal_id, CommandType.TEARDOWN, {}, timeout=TEARDOWN_TIMEOUT
    )
    deleted = bool(result.payload.get("deleted", False))
    absent = bool(result.payload.get("absent", False))
    if deleted or absent:
        db_delete_terminal(terminal_id)
        runtime_registry.unbind_terminal(terminal_id)
    return deleted or absent
