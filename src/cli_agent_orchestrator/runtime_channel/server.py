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
import os
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
from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import (
    get_terminal_metadata,
    list_terminal_ids_on_runtime,
    list_terminals_by_session,
)
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
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
from cli_agent_orchestrator.utils.terminal import validate_tmux_name

logger = logging.getLogger(__name__)

router = APIRouter()

# Strong references to teardown tasks: the event loop keeps only weak ones.
_teardowns: Set["asyncio.Task[None]"] = set()
# A runtime may defer deleting a terminal (its cleanup is not done yet), or fail
# it. For one the server never recorded, no row would route a later retry, so
# the delete is retried here, with the delay doubling from this up to that.
UNRECORDED_RETRY_DELAY = 5.0
UNRECORDED_RETRY_MAX_DELAY = 60.0
# The ids each connection's unrecorded cleanups are deleting: one per id.
_unrecorded: Dict[RuntimeConnection, Set[str]] = {}
# Likewise for launch settlements that outlive a cancelled request.
_settlements: Set["asyncio.Task[Dict[str, Any]]"] = set()


#: Seconds a launch may take, from the command to the runtime's result.
LAUNCH_TIMEOUT_ENV = "CAO_RUNTIME_LAUNCH_TIMEOUT"


def _launch_timeout() -> float:
    """``CAO_RUNTIME_LAUNCH_TIMEOUT``, else ``LAUNCH_TIMEOUT`` (240 s). Set it above
    the longest provider start-up a runtime's profiles allow."""
    raw = os.environ.get(LAUNCH_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        logger.warning("ignoring %s=%r: not a positive number of seconds", LAUNCH_TIMEOUT_ENV, raw)
    return LAUNCH_TIMEOUT


_TERMINAL_ID = TypeAdapter(TerminalId)


def _drop_lost_terminals(
    runtime_id: str, terminal_ids: List[str], conn: Optional[RuntimeConnection] = None
) -> List[str]:
    """Drop the records of terminals ``runtime_id`` no longer runs; return those gone.

    Each row goes only while it still names the runtime and its session, so a
    relaunch of the id elsewhere keeps its own. No plugin event is emitted: as
    with the server's own sweeps, nothing here tore a terminal down. A row that
    cannot be dropped now is kept, routing and all, for the next hello. With
    ``conn``, the connection whose hello decided these drops, none starts once
    a newer connection has replaced it: that one's hello decides then.
    """
    from cli_agent_orchestrator.services import terminal_service

    gone: List[str] = []
    dropped: List[str] = []
    for terminal_id in terminal_ids:
        if conn is not None and conn.closed:
            break
        try:
            row = get_terminal_metadata(terminal_id)
            if row is not None and row.get("runtime_id") == runtime_id:
                if terminal_service.delete_terminal_row(
                    terminal_id, row, registry=None, routed=True
                ):
                    dropped.append(terminal_id)
            # Dropped now, or no longer a row of this runtime's: either way the
            # placement, conditional on the runtime, can go.
            gone.append(terminal_id)
        except Exception:  # noqa: BLE001 - kept for the next hello; the channel goes on
            logger.warning(
                "could not drop the record of terminal %s, lost on runtime %s",
                terminal_id,
                runtime_id,
                exc_info=True,
            )
    if dropped:
        logger.warning(
            "runtime %s no longer runs terminal(s) %s: dropped their records",
            runtime_id,
            ", ".join(dropped),
        )
        # Not for an id that is no longer this runtime's row: a relaunch of it
        # elsewhere is another terminal, whose status this is not.
        _publish_unknown(dropped)
    return gone


def _publish_unknown(terminal_ids: List[str]) -> None:
    """Tell the server's status consumers these terminals' status is no longer
    known, as ``GET /terminals/{id}`` now reads.

    They act on ``terminal.{id}.status`` events only: an approval prompt opened
    for a terminal waiting for its user expires on the next event with another
    status, which no runtime sends for a terminal it is no longer connected for,
    or no longer runs. ``bus.publish`` is safe from any thread.
    """
    for terminal_id in terminal_ids:
        bus.publish(f"terminal.{terminal_id}.status", {"status": TerminalStatus.UNKNOWN.value})


def _unreported_terminal(frame: Result) -> Optional[str]:
    """The terminal a failed result names as still running, if any: a launch
    whose agent cao-bridge could neither report nor stop."""
    named = frame.payload.get("terminal_id") if not frame.ok else None
    try:
        return _TERMINAL_ID.validate_python(named) if named is not None else None
    except ValidationError:
        return None


def _token_matches(presented: str) -> bool:
    expected = runtime_token()
    if not expected:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())


def _delete_unrecorded(conn: RuntimeConnection, terminal_id: str) -> None:
    """Delete, in its runtime, a terminal the server has no record of.

    That is a launch whose result was lost (or arrived after the server gave
    up), so nothing else would ever stop it.

    One cleanup per id and connection: a runtime repeating an id cannot start
    more. Distinct ids are not capped: the server cannot tell an id a runtime
    made up from an agent it must stop, and a token holder is trusted anyway
    (see docs/execution-runtimes.md, Security).
    """
    deleting = _unrecorded.setdefault(conn, set())
    if terminal_id in deleting:
        return
    deleting.add(terminal_id)

    async def run() -> None:
        logger.warning(
            "runtime %s runs terminal %s, which this server never recorded; deleting it",
            conn.runtime_id,
            terminal_id,
        )
        try:
            delay = UNRECORDED_RETRY_DELAY
            # Until the runtime confirms, or this connection ends: no row routes a
            # later retry, but the runtime's next hello lists the terminal again.
            while not conn.closed:
                try:
                    result = await conn.call(CommandType.DELETE, {}, terminal_id=terminal_id)
                    if result.get("deleted"):
                        return
                    reason = "deferred by the runtime"
                except RuntimeUnavailableError:
                    return  # the connection is gone
                except RemoteRuntimeError as exc:  # no answer, or the runtime failed it
                    reason = str(exc)
                logger.warning(
                    "unrecorded terminal %s on runtime %s not deleted yet (%s); retrying in %.0fs",
                    terminal_id,
                    conn.runtime_id,
                    reason,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, UNRECORDED_RETRY_MAX_DELAY)

        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a background task: log, never leave it unseen
            logger.exception(
                "cleanup of unrecorded terminal %s on runtime %s failed",
                terminal_id,
                conn.runtime_id,
            )

    task = asyncio.create_task(run())
    _teardowns.add(task)

    def finished(done: "asyncio.Task[None]") -> None:
        _teardowns.discard(done)
        deleting.discard(terminal_id)
        if not deleting and _unrecorded.get(conn) is deleting:
            del _unrecorded[conn]

    task.add_done_callback(finished)


@router.websocket("/runtime/channel")
async def runtime_channel(ws: WebSocket) -> None:
    if not _token_matches(ws.headers.get(TOKEN_HEADER, "")):
        # Closing before accept() rejects the handshake itself: the ASGI server
        # answers the upgrade request with HTTP 403 (no WebSocket, so the close
        # code is never sent). The tests and `cao-bridge` rely on that 403.
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

    async def close_replaced() -> None:
        await ws.close(code=status.WS_1000_NORMAL_CLOSURE, reason="replaced")

    conn = runtime_registry.register(runtime_id, ws.send_text, close_socket=close_replaced)
    # What the instance behind this connection runs. Set before anything here
    # awaits, so a launch settling meanwhile can tell whether this one runs it.
    conn.listed = set(hello.statuses)
    try:
        # One reconciliation at a time per runtime: a newer connection's hello
        # waits for an older one's, which drops no further record once it is
        # replaced. So a replaced hello never acts on rows the newer one keeps.
        async with runtime_registry.hello_lock(runtime_id):
            # Terminals whose central row names this runtime are its to report on;
            # a status for any other terminal is ignored.
            recorded = await asyncio.to_thread(list_terminal_ids_on_runtime, runtime_id)
            # Conditionally: a relaunch of an id elsewhere (deleted, then claimed by
            # another runtime) after that read must keep its placement.
            held_elsewhere = {t for t in recorded if not runtime_registry.claim(t, runtime_id)}
            # Revalidate against the rows now. A delete may have dropped a row after
            # that read and before its placement: undo this runtime's placement of
            # any row that is gone. And a row that still names this runtime wins
            # over another runtime's placement of its id, which is then stale.
            still_recorded = set(await asyncio.to_thread(list_terminal_ids_on_runtime, runtime_id))
            for terminal_id in recorded:
                if terminal_id not in still_recorded:
                    runtime_registry.unplace(terminal_id, runtime_id)
                elif terminal_id in held_elsewhere:
                    runtime_registry.place(terminal_id, runtime_id)
            # A terminal recorded on this runtime that its hello does not list is
            # gone, lost with the runtime's previous state: its pod was replaced, or
            # cao-bridge restarted without the terminal's tmux. Its record goes too.
            # A launch still being recorded is left to its own path (see
            # _drop_if_lost_to_a_replacement), which knows where it came from.
            lost = sorted(
                terminal_id
                for terminal_id in still_recorded
                if terminal_id not in conn.listed
                and not runtime_registry.is_reserved(terminal_id, runtime_id)
            )
            if lost:
                for terminal_id in await asyncio.to_thread(
                    _drop_lost_terminals, runtime_id, lost, conn
                ):
                    runtime_registry.unplace(terminal_id, runtime_id)
            for terminal_id, reported in hello.statuses.items():
                runtime_registry.set_status(terminal_id, runtime_id, reported, conn=conn)
        if conn.closed:
            # Replaced during the hello: the newer connection's hello decides.
            return
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
                unreported = _unreported_terminal(frame)
                if unreported and not runtime_registry.is_known(unreported, runtime_id):
                    # A failed launch whose agent the runtime could not stop.
                    _delete_unrecorded(conn, unreported)
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
        # Only while this is the runtime's current connection does its end say
        # anything: a replaced one's terminals are the newer connection's.
        _publish_unknown(runtime_registry.unregister(runtime_id, conn))


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
    # The Kiro engine the runtime resolved for the launch (v2 or kas).
    engine: Optional[KiroEngine] = None
    # The launch model the runtime resolved, and whether its provider applies
    # it (#856). A cao-bridge from before #856 sends neither: unknown.
    model: Optional[str] = None
    model_honored: Optional[bool] = None

    @field_validator("name", "session_name")
    @classmethod
    def _addressable(cls, value: str, info: ValidationInfo) -> str:
        # The allowlist local creation uses: the session APIs refuse any other
        # name, so a row under one could not be addressed through them.
        return validate_tmux_name(value, info.field_name or "name")


async def _undo_launch(
    runtime_id: str,
    terminal_id: str,
    origin: Optional[RuntimeConnection],
    session_name: Optional[str] = None,
) -> bool:
    """Delete a launched terminal the server will not record. True if it is gone.

    Only the instance that ran the launch can confirm that: ``origin``, the
    connection its result came over, or a newer connection whose hello listed
    the terminal (the same runtime, reconnected). Another instance under the
    same runtime id would answer that the terminal is absent while the
    original may still run it; then the outcome is reported unconfirmed, and
    the original's next hello lists the terminal and gets it deleted.
    ``session_name``, the session the launch result reported, fences the
    delete to that terminal, not a later launch reusing its id.

    If the runtime defers or fails it, the delete is retried in the background
    (see ``_delete_unrecorded``): with no central row, nothing else would.
    """
    # Not recorded after all: from here on, a hello that lists the terminal
    # gets it deleted, instead of skipping it as a launch being recorded.
    runtime_registry.release(terminal_id, runtime_id)
    conn = _instance_running(runtime_id, terminal_id, origin)
    if conn is None:
        logger.warning(
            "terminal %s was launched over a connection of runtime %s that has since "
            "been replaced; it is deleted when that instance reconnects",
            terminal_id,
            runtime_id,
        )
        return False
    try:
        deleted = await conn.call(
            CommandType.DELETE, {}, terminal_id=terminal_id, session_name=session_name
        )
        if deleted.get("deleted"):
            return True
    except RemoteRuntimeError as exc:
        logger.warning(
            "could not delete terminal %s on runtime %s: %s", terminal_id, runtime_id, exc
        )
    if not conn.closed:
        _delete_unrecorded(conn, terminal_id)
    # Otherwise the instance's next hello lists the terminal, and it is deleted then.
    return False


def _instance_running(
    runtime_id: str, terminal_id: str, origin: Optional[RuntimeConnection]
) -> Optional[RuntimeConnection]:
    """The open connection to the instance that ran a launch, if there is one."""
    if origin is not None and not origin.closed:
        return origin
    current = runtime_registry.connection(runtime_id)
    if current is not None and (origin is None or terminal_id in current.listed):
        return current
    return None


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
        # Claimed before the row is written, so a reconnect in between does not
        # take the terminal for an unrecorded one. Atomic with the check that
        # no other runtime holds the id: a concurrent launch of the same id in
        # another session (another lock) cannot overwrite, or later drop, it.
        if not runtime_registry.claim(launched.id, runtime_id):
            return f"terminal id {launched.id} is already in use"
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
                engine=launched.engine.value if launched.engine else None,
                model=launched.model,
                model_honored=launched.model_honored,
            )
        except Exception:
            # The placement follows the row. Keep it if a row places this id on
            # this runtime anyway: a concurrent launch of the same id there, in
            # another session (so under another lock), wrote it first.
            if not _recorded_on(launched.id, runtime_id):
                runtime_registry.unplace(launched.id, runtime_id)
            raise
    return None


def _recorded_on(terminal_id: str, runtime_id: str) -> bool:
    """Whether the central row of ``terminal_id`` places it on ``runtime_id``."""
    try:
        row = get_terminal_metadata(terminal_id)
    except Exception:  # noqa: BLE001 - unreadable: the write that just failed decides
        return False
    return row is not None and row.get("runtime_id") == runtime_id


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
            cleaned = await _undo_launch(runtime_id, named, conn)
            detail += f" for terminal {named}; " + (
                "it was deleted" if cleaned else "it may still be running there"
            )
        else:
            detail += "; an agent may still be running there"
        logger.warning("%s: %s", detail, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc

    terminal_id = launched.id
    if launched.status:
        # Only if the runtime has not already sent a newer status after its
        # result, and not if it reconnected meanwhile (its new hello is newer).
        runtime_registry.seed_status(terminal_id, runtime_id, launched.status, conn=conn)
    try:
        conflict = await asyncio.to_thread(
            _record_launch, runtime_id, launched, body.working_directory
        )
    except Exception as exc:  # noqa: BLE001
        # The agent is running with no central row: tear it down so nothing is
        # left running that the server cannot see.
        logger.exception("could not record terminal %s from runtime %s", terminal_id, runtime_id)
        cleaned = await _undo_launch(runtime_id, terminal_id, conn, launched.session_name)
        detail = f"launched terminal {terminal_id} on runtime {runtime_id} but could not record it"
        if not cleaned:
            detail += "; it may still be running there"
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail
        ) from exc
    if conflict:
        cleaned = await _undo_launch(runtime_id, terminal_id, conn, launched.session_name)
        detail = f"runtime {runtime_id} launched terminal {terminal_id}, but {conflict}; " + (
            "it was deleted" if cleaned else "it may still be running there"
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

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
    try:
        return await asyncio.to_thread(terminal_service.get_terminal, terminal_id)
    except Exception:  # noqa: BLE001 - the launch is recorded: never report it failed
        # Committed and running: a failed read-back must not turn it into an
        # error the caller might retry (a second agent). Answer from the record.
        logger.warning("could not read back launched terminal %s", terminal_id, exc_info=True)
        return Terminal(
            id=terminal_id,
            name=launched.name,
            provider=launched.provider,
            session_name=launched.session_name,
            agent_profile=launched.agent_profile,
            model=launched.model,
            model_honored=launched.model_honored,
            caller_id=None,
            allowed_tools=launched.allowed_tools,
            engine=launched.engine,
            shell_command=None,
            group=None,
            metadata=None,
            status=runtime_registry.get_status(terminal_id, runtime_id),
            last_active=None,
            deferred_init_failure=None,
            session_incarnation_id=None,
            # A remote launch carries no initial message (#566), and nothing
            # registers it as ephemeral (#866's registry is read-only here).
            initial_delivery=None,
            ephemeral=False,
        ).model_dump(mode="json")


async def _drop_if_lost_to_a_replacement(
    runtime_id: str, terminal_id: str, origin: RuntimeConnection
) -> None:
    """Undo a recorded launch whose runtime was replaced while it was recorded.

    A newer connection's hello skips a launch still in flight. If that newer
    instance does not run the terminal (its hello did not list it), the one
    that ran it is gone, and so is the terminal: its record is dropped, and the
    launch fails with ``502``. The same runtime reconnecting lists it, and
    keeps it.
    """
    latest = runtime_registry.latest(runtime_id)
    if latest is None or latest is origin or terminal_id in latest.listed:
        return
    # Under the hello lock, as a hello's own drops: no hello reconciles
    # meanwhile, and one that starts waits for this drop.
    async with runtime_registry.hello_lock(runtime_id):
        latest = runtime_registry.latest(runtime_id)
        if latest is None or latest is origin or terminal_id in latest.listed:
            return
        gone = await asyncio.to_thread(_drop_lost_terminals, runtime_id, [terminal_id], latest)
        for dropped in gone:
            runtime_registry.unplace(dropped, runtime_id)
    if not gone and latest.closed:
        # Replaced again before the drop: the newer connection's hello, which
        # waited for this, reconciles the terminal like any recorded one.
        return
    outcome = (
        "its record was dropped"
        if gone
        else "its record could not be dropped and is kept for the runtime's next hello"
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=(
            f"runtime {runtime_id} was replaced while terminal {terminal_id} was being "
            f"recorded, and the replacement does not run it; {outcome}"
        ),
    )


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
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"runtime {runtime_id} is not connected",
        )

    sent = asyncio.Event()

    async def launch_and_settle() -> Dict[str, Any]:
        try:
            result = await conn.call(
                CommandType.LAUNCH,
                body.model_dump(exclude_none=True),
                timeout=_launch_timeout(),
                on_sent=sent.set,
            )
        except RemoteRuntimeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        raw = result.get("terminal")
        reserved = raw.get("id") if isinstance(raw, dict) else None
        try:
            launched = await _finish_launch(request, runtime_id, body, conn, raw)
        finally:
            # Recorded (and so placed) or undone: either way no longer in flight.
            if isinstance(reserved, str):
                runtime_registry.release(reserved, runtime_id)
        # Checked once released: from here on a newer connection's hello
        # reconciles the terminal like any other recorded one.
        await _drop_if_lost_to_a_replacement(runtime_id, launched["id"], conn)
        return launched

    # From the moment the command is sent, the runtime may start an agent: the
    # launch and its settlement (record it, or undo it) are one task, shielded
    # from this request's cancellation once the command is written, so a client
    # that goes away can never leave an agent running unrecorded, or a
    # reservation behind. Before that, cancelling the request cancels the launch.
    operation = asyncio.ensure_future(launch_and_settle())
    _settlements.add(operation)
    operation.add_done_callback(_settlements.discard)
    operation.add_done_callback(lambda done: done.cancelled() or done.exception())
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        if not sent.is_set():
            # Not written yet: nothing can have started, so the launch is
            # cancelled outright. (Cancelled mid-send, the frame may still
            # arrive; its late result is then deleted as an unrecorded launch.)
            operation.cancel()
        raise


@router.get("/runtimes")
async def list_runtimes(
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    return {"runtimes": runtime_registry.list_runtimes()}
