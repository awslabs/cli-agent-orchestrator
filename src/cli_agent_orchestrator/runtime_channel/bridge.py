"""cao-bridge: the execution-only process in an execution runtime (#745).

Runs beside the agents' tmux server, in a pod that has no cao-server. It dials
the central server's ``/runtime/channel`` (outbound only), runs each command it
receives through the local terminal service, and pushes the status the local
status monitor derives. Nothing here serves HTTP.

Configuration:

- ``CAO_BRIDGE_SERVER_URL``: e.g. ``ws://cao-server:9889/runtime/channel``
- ``CAO_BRIDGE_RUNTIME_ID``: this runtime's id (in Kubernetes, the pod name)
- ``CAO_RUNTIME_TOKEN_FILE`` or ``CAO_RUNTIME_TOKEN``: the shared channel token
- ``CAO_BRIDGE_READY_FILE`` (optional): created while the channel is up, for a
  readiness probe
"""

import asyncio
import contextlib
import logging
import os
import re
import signal
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple

import websockets
from websockets.asyncio.client import ClientConnection, connect

from cli_agent_orchestrator.constants import CAO_HOME_DIR, DEFAULT_PROVIDER
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    Command,
    CommandType,
    Hello,
    Result,
    Status,
    decode,
    encode,
)
from cli_agent_orchestrator.runtime_channel.token import TOKEN_HEADER, runtime_token
from cli_agent_orchestrator.services.event_bus import bus

logger = logging.getLogger(__name__)

BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 30.0
_STATUS_TOPIC = re.compile(r"^terminal\.([^.]+)\.status$")


class ChannelRefused(Exception):
    """The server refused this runtime (token or protocol version). Retrying cannot help."""


class Bridge:
    def __init__(
        self, server_url: str, runtime_id: str, token: str, ready_file: Optional[Path] = None
    ):
        self.server_url = server_url
        self.runtime_id = runtime_id
        self._token = token
        self._ready_file = ready_file
        self._ws: Optional[ClientConnection] = None
        self._send_lock = asyncio.Lock()
        # terminal id -> (lock, number of commands holding or awaiting it)
        self._terminal_locks: Dict[str, Tuple[asyncio.Lock, int]] = {}
        # launch op id -> (set once it has settled, ids of the local terminals
        # that existed when it started: none of them can be its own).
        self._launches: Dict[str, Tuple[asyncio.Event, Set[str]]] = {}
        # Whether the current connection completed its hello (see run()).
        self._helloed = False
        # Results that could not be sent (no channel at the time), delivered
        # after the next hello: the server acts on results it no longer awaits.
        self._unsent: List[Result] = []
        # Strong references: the event loop holds only weak ones to tasks.
        self._tasks: Set["asyncio.Task[None]"] = set()
        self._stop = asyncio.Event()

    # --- outbound ---

    async def _send(self, frame) -> None:
        ws = self._ws
        if ws is not None:
            async with self._send_lock:
                try:
                    await ws.send(encode(frame))
                    return
                except websockets.exceptions.ConnectionClosed:
                    pass
        if isinstance(frame, Result):
            self._unsent.append(frame)

    def _local_terminal_ids(self) -> Set[str]:
        from cli_agent_orchestrator.clients.database import list_all_terminals

        return {row["id"] for row in list_all_terminals()}

    def _existing_terminal_ids(self) -> Set[str]:
        """The local terminals now, or none if they cannot be read (then every
        delete waits for the launch, which is merely slower)."""
        try:
            return self._local_terminal_ids()
        except Exception:  # noqa: BLE001 - see the docstring
            logger.debug("could not list local terminals", exc_info=True)
            return set()

    def _status_of(self, terminal_id: str) -> TerminalStatus:
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        return status_monitor.get_status(terminal_id)

    async def _push_status(self, terminal_id: str) -> None:
        """Send the terminal's current status. It is read under the send lock, so
        the last frame the server gets for a terminal carries its newest status."""
        ws = self._ws
        if ws is None:
            return
        async with self._send_lock:
            # Off the loop (it may capture the pane), still under the send lock
            # so status frames cannot be reordered.
            status = await asyncio.to_thread(self._status_of, terminal_id)
            frame = Status(terminal_id=terminal_id, status=status)
            try:
                await ws.send(encode(frame))
            except websockets.exceptions.ConnectionClosed:
                pass

    async def forward_status(self) -> None:
        """Push every status change the local status monitor publishes."""
        queue = bus.subscribe("terminal.*.status")
        try:
            while True:
                event = await queue.get()
                match = _STATUS_TOPIC.match(event["topic"])
                if match:
                    try:
                        await self._push_status(match.group(1))
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - one bad status must not end forwarding
                        logger.warning(
                            "could not push the status of terminal %s",
                            match.group(1),
                            exc_info=True,
                        )
        finally:
            bus.unsubscribe("terminal.*.status", queue)

    # --- commands ---

    @contextlib.asynccontextmanager
    async def _terminal_turn(self, terminal_id: str) -> AsyncIterator[None]:
        """Wait for this terminal's earlier commands. The lock is dropped only
        once nothing holds or awaits it."""
        lock, users = self._terminal_locks.get(terminal_id, (asyncio.Lock(), 0))
        self._terminal_locks[terminal_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._terminal_locks[terminal_id]
            if users == 1:
                del self._terminal_locks[terminal_id]
            else:
                self._terminal_locks[terminal_id] = (lock, users - 1)

    async def handle(self, command: Command) -> None:
        # One terminal's commands run in arrival order; different terminals and
        # launches run concurrently.
        try:
            if command.terminal_id is None:
                existed = await asyncio.to_thread(self._existing_terminal_ids)
                launching = (asyncio.Event(), existed)
                self._launches[command.op_id] = launching
                try:
                    payload = await self.execute(command)
                finally:
                    launching[0].set()
                    self._launches.pop(command.op_id, None)
            else:
                if command.type == CommandType.DELETE:
                    # The terminal may be one a launch still in progress is
                    # starting (its row exists before its result is sent): let
                    # any launch that could have made it settle first, so a
                    # delete sent after a reconnect never tears a pane down
                    # under its initialization. Older terminals do not wait.
                    for settled, existed in list(self._launches.values()):
                        if command.terminal_id not in existed:
                            await settled.wait()
                async with self._terminal_turn(command.terminal_id):
                    payload = await self.execute(command)
            result = Result(op_id=command.op_id, ok=True, payload=payload)
        except Exception as exc:  # noqa: BLE001 - reported to the server as a failed result
            logger.warning("command %s (%s) failed: %s", command.op_id, command.type.value, exc)
            result = Result(op_id=command.op_id, ok=False, error=str(exc) or type(exc).__name__)
        await self._send(result)

    async def execute(self, command: Command) -> Dict[str, Any]:
        # Imported here: the provider stack is only needed once work arrives.
        from cli_agent_orchestrator.clients.database import get_terminal_metadata
        from cli_agent_orchestrator.models.inbox import OrchestrationType
        from cli_agent_orchestrator.services import terminal_service
        from cli_agent_orchestrator.utils.agent_profiles import resolve_provider

        payload = command.payload
        if command.type == CommandType.LAUNCH:
            profile = payload["agent_profile"]
            provider = payload.get("provider") or resolve_provider(profile, DEFAULT_PROVIDER)
            terminal = await terminal_service.create_terminal(
                provider=provider,
                agent_profile=profile,
                new_session=True,
                working_directory=payload.get("working_directory"),
            )
            try:
                local = await asyncio.to_thread(terminal_service.get_terminal, terminal.id)
                return {"terminal": _jsonable(local)}
            except Exception as exc:
                # The agent runs but the server would never learn of it: stop it.
                stopped = False
                try:
                    stopped = bool(
                        await asyncio.to_thread(terminal_service.delete_terminal, terminal.id)
                    )
                except Exception:  # noqa: BLE001 - reported in the error below
                    logger.exception("could not stop unreported terminal %s", terminal.id)
                detail = f"launched terminal {terminal.id} but could not report it ({exc})"
                if not stopped:
                    detail += "; it may still be running"
                raise RuntimeError(detail) from exc

        terminal_id = command.terminal_id
        if not terminal_id:
            raise ValueError(f"{command.type.value} requires a terminal_id")

        if command.type == CommandType.INPUT:
            orchestration = payload.get("orchestration_type")
            success = await asyncio.to_thread(
                terminal_service.send_input,
                terminal_id,
                payload["message"],
                sender_id=payload.get("sender_id"),
                orchestration_type=OrchestrationType(orchestration) if orchestration else None,
                frozen_memory=payload.get("frozen_memory"),
            )
            return {"success": success}
        if command.type == CommandType.KEY:
            success = await asyncio.to_thread(
                terminal_service.send_special_key, terminal_id, payload["key"]
            )
            return {"success": success}
        if command.type == CommandType.OUTPUT:
            mode = terminal_service.OutputMode(payload.get("mode", "full"))
            output = await asyncio.to_thread(terminal_service.get_output, terminal_id, mode)
            return {"output": output}
        if command.type == CommandType.WORKING_DIRECTORY:
            directory = await asyncio.to_thread(terminal_service.get_working_directory, terminal_id)
            return {"working_directory": directory}
        if command.type == CommandType.EXIT:
            await asyncio.to_thread(terminal_service.exit_terminal_cli, terminal_id)
            return {}
        if command.type == CommandType.DELETE:
            if await asyncio.to_thread(get_terminal_metadata, terminal_id) is None:
                # Nothing here to tear down (e.g. the pod was replaced): the
                # goal, no such pane on this runtime, already holds.
                return {"deleted": True, "absent": True}
            deleted = await asyncio.to_thread(terminal_service.delete_terminal, terminal_id)
            return {"deleted": bool(deleted)}
        raise ValueError(f"unsupported command: {command.type.value}")

    # --- connection ---

    def _current_statuses(self) -> Dict[str, TerminalStatus]:
        """Every terminal this runtime runs, with its status: the server deletes
        any it has no record of."""
        from cli_agent_orchestrator.clients.database import list_all_terminals

        return {row["id"]: self._status_of(row["id"]) for row in list_all_terminals()}

    async def serve(self, ws: ClientConnection) -> None:
        """Run one connection: hello exchange, then commands until it closes."""
        statuses = await asyncio.to_thread(self._current_statuses)
        await ws.send(
            encode(
                Hello(
                    protocol_version=PROTOCOL_VERSION,
                    runtime_id=self.runtime_id,
                    statuses=statuses,
                )
            )
        )
        reply = decode(await ws.recv())
        if not isinstance(reply, Hello) or reply.protocol_version != PROTOCOL_VERSION:
            raise ChannelRefused(
                f"protocol mismatch: server speaks {getattr(reply, 'protocol_version', '?')}, "
                f"this runtime {PROTOCOL_VERSION}"
            )
        self._ws = ws
        self._helloed = True
        self._mark_ready(True)
        logger.info("runtime %s connected to %s", self.runtime_id, self.server_url)
        # Everything from here on runs inside the guard: however the connected
        # state ends, the runtime stops reporting Ready.
        try:
            # Changes during the hello exchange were not forwarded (nothing was
            # connected yet), so send each terminal's status as it is now.
            for terminal_id in statuses:
                await self._push_status(terminal_id)
            unsent, self._unsent = self._unsent, []
            for result in unsent:
                await self._send(result)
            async for raw in ws:
                frame = decode(raw)
                if isinstance(frame, Command):
                    task = asyncio.create_task(self.handle(frame))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                else:
                    logger.warning("ignoring unexpected %s frame from the server", frame.kind)
        finally:
            self._ws = None
            self._mark_ready(False)

    async def run(self) -> None:
        """Keep a channel open until stopped, reconnecting with backoff."""
        backoff = BACKOFF_INITIAL
        self._mark_ready(False)
        while not self._stop.is_set():
            self._helloed = False
            try:
                async with connect(
                    self.server_url,
                    additional_headers={TOKEN_HEADER: self._token},
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    await self.serve(ws)
            except ChannelRefused:
                raise
            except websockets.exceptions.InvalidStatus as exc:
                if exc.response.status_code in (401, 403):
                    raise ChannelRefused(
                        f"server refused the runtime token ({exc.response.status_code})"
                    ) from exc
                logger.warning("runtime channel rejected: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - network errors are retried
                logger.warning("runtime channel lost: %s", exc)
            if self._helloed:
                # Only a completed hello: a server that accepts the upgrade and
                # then drops the channel still backs off exponentially.
                backoff = BACKOFF_INITIAL
            if self._stop.is_set():
                break
            logger.info("reconnecting in %.0fs", backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, BACKOFF_MAX)

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            asyncio.get_running_loop().create_task(ws.close())

    def _mark_ready(self, ready: bool) -> None:
        if self._ready_file is None:
            return
        try:
            if ready:
                self._ready_file.parent.mkdir(parents=True, exist_ok=True)
                self._ready_file.write_text(f"{os.getpid()}\n")
            else:
                self._ready_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("could not update readiness file %s: %s", self._ready_file, exc)


def _jsonable(terminal: Dict[str, Any]) -> Dict[str, Any]:
    """The terminal fields the server records, as plain JSON values."""
    keys = (
        "id",
        "name",
        "provider",
        "session_name",
        "agent_profile",
        "allowed_tools",
        "status",
        "engine",
    )
    out = {key: terminal.get(key) for key in keys}
    for key in ("provider", "status", "engine"):
        out[key] = getattr(out[key], "value", out[key])
    return out


def _drop_stale_rows() -> int:
    """Forget local terminals whose tmux session is confirmed gone.

    The runtime's state (its SQLite rows) can outlive the container, while its
    tmux server does not: a row whose session is gone is a terminal lost with
    the old container, and must not be reported as running in the hello. A
    session that cannot be checked is kept. Returns the number of rows dropped.
    """
    from cli_agent_orchestrator.backends.registry import get_backend
    from cli_agent_orchestrator.clients.database import (
        delete_terminals_by_session,
        list_all_terminals,
    )

    backend = get_backend()
    dropped = 0
    for session in sorted({row["tmux_session"] for row in list_all_terminals()}):
        try:
            alive = backend.session_exists_strict(session)
        except Exception as exc:  # noqa: BLE001 - cannot tell: keep the rows
            logger.warning("could not check tmux session %s at startup: %s", session, exc)
            continue
        if not alive:
            count = delete_terminals_by_session(session)
            logger.info("dropped %d terminal(s) of lost tmux session %s", count, session)
            dropped += count
    return dropped


async def _amain() -> None:
    from cli_agent_orchestrator.clients.database import init_runtime_db
    from cli_agent_orchestrator.services.log_writer import log_writer
    from cli_agent_orchestrator.services.status_monitor import status_monitor

    server_url = os.environ.get("CAO_BRIDGE_SERVER_URL", "").strip()
    runtime_id = os.environ.get("CAO_BRIDGE_RUNTIME_ID", "").strip()
    token = runtime_token()
    if not server_url or not runtime_id or not token:
        raise SystemExit(
            "cao-bridge requires CAO_BRIDGE_SERVER_URL, CAO_BRIDGE_RUNTIME_ID and "
            "CAO_RUNTIME_TOKEN_FILE (or CAO_RUNTIME_TOKEN)"
        )
    ready = os.environ.get("CAO_BRIDGE_READY_FILE", "").strip()
    ready_file = Path(ready) if ready else CAO_HOME_DIR / "bridge-ready"

    init_runtime_db()
    # Before the first hello, which lists every row as a running terminal.
    await asyncio.to_thread(_drop_stale_rows)
    loop = asyncio.get_running_loop()
    bus.set_loop(loop)
    bridge = Bridge(server_url, runtime_id, token, ready_file=ready_file)
    tasks = [
        asyncio.create_task(status_monitor.run()),
        asyncio.create_task(log_writer.run()),
        asyncio.create_task(bridge.forward_status()),
    ]
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bridge.stop)
    try:
        await bridge.run()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    from cli_agent_orchestrator.utils.logging import setup_logging

    setup_logging()
    try:
        asyncio.run(_amain())
    except ChannelRefused as exc:
        raise SystemExit(f"cao-bridge: {exc}")


if __name__ == "__main__":
    main()
