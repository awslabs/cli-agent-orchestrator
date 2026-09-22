"""cao-bridge: execution-only runtime for remote workers (#745/#776).

Runs beside the agent's tmux server in a worker pod and holds one persistent
OUTBOUND WebSocket to the central cao-server. It is not a renamed cao-server:
no HTTP API, no scheduler, no MCP, no plugins — it reuses the existing local
execution stack (terminal_service, FIFO reader, StatusMonitor, LogWriter, the
in-process bus) and forwards what used to be co-located hops over the channel:

- commands come DOWN (launch, input, special_key, extract, teardown) with
  op_id correlation; results are retained until the server acks them, so a
  lost response is re-deliverable instead of re-executed;
- terminal output and worker-derived status stream UP with per-stream
  (generation, position) sequencing from a bounded replay buffer, so a server
  that reconnects resumes or sees an explicit gap.

The bridge keeps its own local SQLite registry (throwaway pod storage) as
runtime-internal bookkeeping for the reused service layer; the central
server's row remains the authoritative terminal identity.
"""

import asyncio
import base64
import logging
import os
import re
import signal
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional

import websockets

from cli_agent_orchestrator.clients.database import init_runtime_db
from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    AckFrame,
    CommandFrame,
    CommandOutcome,
    CommandResultFrame,
    CommandType,
    EventFrame,
    EventType,
    Frame,
    GapFrame,
    HeartbeatFrame,
    HelloFrame,
    StreamFrame,
    StreamName,
    StreamPosition,
    decode_frame,
    encode_frame,
)
from cli_agent_orchestrator.runtime_channel.replay_buffer import GapInfo, ReplayBuffer
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.log_writer import log_writer
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.utils.logging import setup_logging

logger = logging.getLogger(__name__)

RUNTIME_TOKEN_HEADER = "X-CAO-Runtime-Token"
RECONNECT_BACKOFF_INITIAL = 1.0
RECONNECT_BACKOFF_MAX = 30.0
HEARTBEAT_INTERVAL = 15.0
REPLAY_BUFFER_BYTES = 1024 * 1024  # per terminal capture stream

_OUTPUT_TOPIC = re.compile(r"^terminal\.([a-f0-9]{8})\.output$")
_STATUS_TOPIC = re.compile(r"^terminal\.([a-f0-9]{8})\.status$")

# A bridge serves no HTTP, so there is nothing to GET for readiness. This file
# exists exactly while the runtime channel is established and the hello has been
# accepted, which is the only condition under which the pod can do any work:
# the orchestrator reaches it over the channel, never inbound. An exec probe on
# this path is therefore a true readiness signal rather than a liveness proxy.
READY_FILE_ENV = "CAO_BRIDGE_READY_FILE"
DEFAULT_READY_FILE = CAO_HOME_DIR / "bridge-connected"


def ready_file_path() -> Path:
    """Where the channel-established marker lives. Env override read per call."""
    override = os.environ.get(READY_FILE_ENV, "").strip()
    return Path(override) if override else DEFAULT_READY_FILE


def mark_channel_ready() -> None:
    """Announce an established channel. Never fatal.

    A read-only or missing state mount must degrade to an unready pod, not to a
    crashed runtime: the channel is up and usable either way, and killing the
    process would discard live terminals to fix a reporting problem.
    """
    path = ready_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{os.getpid()}\n")
    except OSError as e:
        logger.warning("could not write readiness marker %s (%s)", path, e)


def clear_channel_ready() -> None:
    """Withdraw readiness the moment the channel is gone."""
    try:
        ready_file_path().unlink(missing_ok=True)
    except OSError as e:  # pragma: no cover - unlink on a live path
        logger.warning("could not remove readiness marker (%s)", e)


class Bridge:
    def __init__(self, server_url: str, runtime_id: str, token: str):
        self._server_url = server_url
        self._runtime_id = runtime_id
        self._token = token
        self._buffers: Dict[str, ReplayBuffer] = {}
        # Results not yet acked by the server, re-sent after every reconnect.
        # Bounded by the number of in-flight ops, which the server bounds.
        self._unacked: Dict[str, CommandResultFrame] = {}
        self._ws: Optional[websockets.ClientConnection] = None
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        # In-flight script subprocesses by op_id, so CANCEL_SCRIPT can terminate
        # the exact run without touching any other (#745, script relocation).
        self._script_procs: Dict[str, asyncio.subprocess.Process] = {}
        # Interactive attach PTYs by terminal_id (#776): the PTY subprocess
        # lives HERE, beside the tmux socket; only bytes cross the channel.
        self._attach: Dict[str, dict] = {}
        # Whether the current attempt got past the hello exchange. Read by the
        # reconnect loop to decide if the backoff earned a reset.
        self._established = False

    # --- outbound plumbing ---

    async def _send(self, frame) -> None:
        """Send if connected; silently skip otherwise (the replay buffer and
        unacked-result map are what survive the disconnection, not the send)."""
        ws = self._ws
        if ws is None:
            return
        async with self._send_lock:
            try:
                await ws.send(encode_frame(frame))
            except websockets.exceptions.ConnectionClosed:
                pass

    def _buffer_for(self, terminal_id: str) -> ReplayBuffer:
        buf = self._buffers.get(terminal_id)
        if buf is None:
            buf = ReplayBuffer(max_bytes=REPLAY_BUFFER_BYTES)
            self._buffers[terminal_id] = buf
        return buf

    # --- bus forwarding (runtime → server) ---

    async def _forward_output(self) -> None:
        """Stream captured output up, with the loss the bus caused made visible.

        The subscription queue is bounded and the bus drops on full — that is
        deliberate back-pressure for a TUI that can emit faster than anything
        downstream reads. What was NOT deliberate: numbering the chunks here,
        after that drop. Positions came out contiguous whatever the bus had
        thrown away, so the watermark advertised at every hello and heartbeat
        described a complete stream that the server had only part of, and no
        GapFrame was ever possible (review finding 3 on #802). The producer now
        stamps each event with its offset in the terminal's byte stream, so a
        missing event is arithmetic here rather than an invisible hole: the gap
        is reported first, and only then the bytes that follow it.
        """
        queue = bus.subscribe("terminal.*.output")
        try:
            while True:
                event = await queue.get()
                match = _OUTPUT_TOPIC.match(event["topic"])
                if not match:
                    continue
                terminal_id = match.group(1)
                data = event["data"].get("data", "")
                if not data:
                    continue
                raw = data.encode("utf-8", errors="replace")
                buf = self._buffer_for(terminal_id)
                offset = event["data"].get("offset")
                if offset is None:
                    # A publisher that carries no offset (a non-FIFO producer)
                    # keeps the old arrival-order numbering; nothing claims its
                    # stream is gap-checked.
                    gap, pos = None, buf.append(raw)
                else:
                    start = int(offset)
                    if start < buf.end_pos:
                        # The producer's offset counter is monotonic per stream,
                        # so a start BEHIND the watermark means it restarted: the
                        # FIFO reader was re-armed, or the pane re-created, and
                        # these bytes are a NEW stream reusing the terminal id.
                        # Appending them contiguously would splice the two into a
                        # transcript that never existed, so advance the generation
                        # — the protocol's fence for precisely this — and number
                        # the new stream from 0 (Copilot review on #802).
                        logger.warning(
                            "terminal %s restarted its output stream at offset %s "
                            "(watermark was %s); starting generation %s",
                            terminal_id,
                            start,
                            buf.end_pos,
                            buf.generation + 1,
                        )
                        buf.begin_generation()
                    gap, pos = buf.append_at(start, raw)
                if gap is not None:
                    logger.warning(
                        "dropped output for terminal %s: [%s, %s) never reached the channel",
                        terminal_id,
                        gap.from_pos,
                        gap.to_pos,
                    )
                    await self._send(
                        GapFrame(
                            terminal_id=terminal_id,
                            stream=StreamName.CAPTURE,
                            generation=buf.generation,
                            from_pos=gap.from_pos,
                            to_pos=gap.to_pos,
                        )
                    )
                await self._send(
                    StreamFrame(
                        terminal_id=terminal_id,
                        stream=StreamName.CAPTURE,
                        generation=buf.generation,
                        pos=pos,
                        data=base64.b64encode(raw).decode(),
                    )
                )
        finally:
            bus.unsubscribe("terminal.*.output", queue)

    async def _forward_status(self) -> None:
        queue = bus.subscribe("terminal.*.status")
        try:
            while True:
                event = await queue.get()
                match = _STATUS_TOPIC.match(event["topic"])
                if not match:
                    continue
                terminal_id = match.group(1)
                try:
                    status = TerminalStatus(event["data"]["status"])
                except (KeyError, ValueError):
                    continue
                await self._send(
                    EventFrame(
                        terminal_id=terminal_id,
                        generation=self._buffer_for(terminal_id).generation,
                        type=EventType.STATUS,
                        status=status,
                    )
                )
        finally:
            bus.unsubscribe("terminal.*.status", queue)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await self._send(HeartbeatFrame(streams=self._stream_positions()))

    def _stream_positions(self) -> list:
        return [
            StreamPosition(
                terminal_id=tid,
                stream=StreamName.CAPTURE,
                generation=buf.generation,
                end_pos=buf.end_pos,
            )
            for tid, buf in self._buffers.items()
        ]

    def _terminal_statuses(self) -> dict:
        """Current status per live terminal, for the hello snapshot.

        A terminal the runtime cannot read a verdict for is left out rather
        than reported as UNKNOWN: absent means "no claim", and the server's
        own UNKNOWN default already covers that.
        """
        statuses = {}
        for tid in self._buffers:
            try:
                status = status_monitor.get_status(tid)
            except Exception as e:  # a status read must never fail the hello
                logger.warning("could not read status for %s at hello: %s", tid, e)
                continue
            if status != TerminalStatus.UNKNOWN:
                statuses[tid] = status
        return statuses

    # --- command execution (server → runtime) ---

    async def _handle_command(self, frame: CommandFrame) -> None:
        try:
            outcome, payload, terminal_id = await self._execute(frame)
        except Exception as e:
            logger.exception("command %s (%s) failed", frame.op_id, frame.type.value)
            outcome, payload, terminal_id = (
                CommandOutcome.FAILED,
                {"error": str(e)},
                frame.terminal_id,
            )
        result = CommandResultFrame(
            op_id=frame.op_id, terminal_id=terminal_id, outcome=outcome, payload=payload
        )
        # Retain BEFORE sending: an ack that never arrives (channel died with
        # the result in flight) must leave the result re-deliverable.
        self._unacked[frame.op_id] = result
        await self._send(result)

    async def _execute(self, frame: CommandFrame):
        # Imported here, not at module top: importing terminal_service pulls in
        # the provider stack, which is only needed once a command arrives.
        from cli_agent_orchestrator.constants import DEFAULT_PROVIDER
        from cli_agent_orchestrator.services import terminal_service
        from cli_agent_orchestrator.utils.agent_profiles import resolve_provider

        payload = frame.payload

        # Runtime-scoped commands (no terminal_id): script execution (#745).
        if frame.type == CommandType.RUN_SCRIPT:
            result = await self._run_script(
                frame.op_id,
                payload["script"],
                payload.get("env", {}),
                float(payload.get("timeout", 300.0)),
                float(payload.get("term_grace", 10.0)),
                str(payload.get("mode", "python")),
            )
            return CommandOutcome.OK, result, None
        if frame.type == CommandType.CANCEL_SCRIPT:
            target_op = payload["target_op_id"]
            proc = self._script_procs.get(target_op)
            if proc is not None:
                await self._terminate_process(proc, float(payload.get("term_grace", 10.0)))
                return CommandOutcome.CANCEL_REQUESTED, {"cancelled": True}, None
            return CommandOutcome.OK, {"cancelled": False, "reason": "not running"}, None

        if frame.type == CommandType.LAUNCH:
            # ``new_session`` false adds a window to a session this runtime
            # already owns: an in-session assign/handoff by an agent executing
            # here places its worker BESIDE it, in the same tmux session, since
            # that session exists in this pod and not in the server container.
            new_session = bool(payload.get("new_session", True))
            defer_init = bool(payload.get("defer_init", False))
            orch_type = payload.get("initial_message_orchestration_type")
            # An absent provider means "whatever this installation says", and
            # this installation is the only one that can answer: the profile
            # store, the installed CLIs and the engine all live here, not in the
            # client that typed the command or the server that relayed it. A
            # present provider is an explicit override the caller asked for, so
            # it is honoured as given (review finding 8 on #802).
            provider = payload.get("provider") or resolve_provider(
                payload["agent_profile"], DEFAULT_PROVIDER
            )
            terminal = await terminal_service.create_terminal(
                provider=provider,
                agent_profile=payload["agent_profile"],
                session_name=payload.get("session_name"),
                new_session=new_session,
                working_directory=payload.get("working_directory"),
                env_vars=payload.get("env_vars"),
                model=payload.get("model"),
                caller_id=payload.get("caller_id"),
                allowed_tools=payload.get("allowed_tools"),
                # Deferred init keeps the caller's tool call short: the window
                # and its registry row exist on return, provider startup and
                # the first message run in a local background task exactly as
                # they do for a co-located assign.
                defer_init=defer_init,
                initial_message=payload.get("initial_message") if defer_init else None,
                initial_message_orchestration_type=(
                    OrchestrationType(orch_type) if defer_init and orch_type else None
                ),
                engine=payload.get("engine"),
                use_worktree=bool(payload.get("use_worktree", False)),
            )
            # Open the terminal's buffer now, not at its first byte. `_buffers`
            # is this runtime's record of which panes it owns: the hello snapshot
            # (streams + statuses) is built from it, so a pane that has not
            # emitted anything yet — a provider still starting, an agent idle at
            # its prompt — was left out of the snapshot entirely, and a server
            # that restarted in that window neither rebound its routing nor
            # learned its status (Copilot review on #802, finding 11). An empty
            # buffer reports end_pos 0, which is exactly true.
            self._buffer_for(terminal.id)
            if payload.get("initial_message") and not defer_init:
                await asyncio.to_thread(
                    terminal_service.send_input, terminal.id, payload["initial_message"]
                )
            return (
                CommandOutcome.OK,
                {
                    "terminal": {
                        "id": terminal.id,
                        "name": terminal.name,
                        # Enum fields may arrive as enum or plain str depending
                        # on the model's coercion settings — normalize both.
                        "provider": getattr(terminal.provider, "value", terminal.provider),
                        "session_name": terminal.session_name,
                        "agent_profile": terminal.agent_profile,
                        "allowed_tools": terminal.allowed_tools,
                        "shell_command": terminal.shell_command,
                        "status": getattr(terminal.status, "value", terminal.status),
                    }
                },
                terminal.id,
            )

        terminal_id = frame.terminal_id
        if terminal_id is None:
            raise ValueError(f"command {frame.type.value} requires a terminal_id")

        if frame.type == CommandType.INPUT:
            success = await asyncio.to_thread(
                terminal_service.send_input,
                terminal_id,
                payload["message"],
                sender_id=payload.get("sender_id"),
                orchestration_type=payload.get("orchestration_type"),
            )
            return CommandOutcome.OK, {"success": success}, terminal_id
        if frame.type == CommandType.SPECIAL_KEY:
            success = await asyncio.to_thread(
                terminal_service.send_special_key, terminal_id, payload["key"]
            )
            return CommandOutcome.OK, {"success": success}, terminal_id
        if frame.type == CommandType.EXTRACT:
            mode = terminal_service.OutputMode(payload.get("mode", "full"))
            output = await asyncio.to_thread(terminal_service.get_output, terminal_id, mode)
            return CommandOutcome.OK, {"output": output}, terminal_id
        if frame.type == CommandType.TEARDOWN:
            await self._attach_close(terminal_id)
            deleted = await asyncio.to_thread(terminal_service.delete_terminal, terminal_id)
            self._buffers.pop(terminal_id, None)
            if not deleted:
                # "Not deleted" has two meanings that must not be conflated. A
                # runtime with no row for this terminal has nothing to tear down:
                # its pod was replaced, so the tmux server and the local row went
                # with it, and the caller's goal state — no session here — already
                # holds. Reporting that as a failure wedges every caller that
                # recycles before launching, which is how a scheduled flow whose
                # executor restarted would defer forever. A row that IS still here
                # is a genuinely deferred cleanup and stays a failure, because a
                # session that may be alive must never be declared gone.
                absent = (
                    await asyncio.to_thread(terminal_service.get_terminal_metadata, terminal_id)
                ) is None
                if absent:
                    return CommandOutcome.OK, {"deleted": False, "absent": True}, terminal_id
            return (
                CommandOutcome.OK if deleted else CommandOutcome.FAILED,
                {"deleted": deleted},
                terminal_id,
            )
        if frame.type == CommandType.ATTACH_OPEN:
            opened = await self._attach_open(
                terminal_id, int(payload.get("rows", 24)), int(payload.get("cols", 80))
            )
            return (
                CommandOutcome.OK if opened else CommandOutcome.FAILED,
                {"opened": opened},
                terminal_id,
            )
        if frame.type == CommandType.ATTACH_DATA:
            written = self._attach_write(terminal_id, base64.b64decode(payload["data"]))
            return CommandOutcome.OK, {"written": written}, terminal_id
        if frame.type == CommandType.RESIZE:
            resized = self._attach_resize(
                terminal_id, int(payload.get("rows", 24)), int(payload.get("cols", 80))
            )
            return CommandOutcome.OK, {"resized": resized}, terminal_id
        if frame.type == CommandType.ATTACH_CLOSE:
            await self._attach_close(terminal_id)
            return CommandOutcome.OK, {"closed": True}, terminal_id
        raise ValueError(f"unsupported command type: {frame.type.value}")

    # --- interactive attach (#776): PTY lives beside the tmux socket ---

    async def _attach_open(self, terminal_id: str, rows: int, cols: int) -> bool:
        """Spawn the backend's interactive attach client in a local PTY and
        pump its output up the channel as the ``attach`` stream. One attach
        per terminal; a second open replaces the first (last caller wins,
        mirroring tmux's own attach semantics)."""
        import fcntl
        import pty
        import struct
        import subprocess
        import termios

        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import get_terminal_metadata

        await self._attach_close(terminal_id)

        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            return False
        try:
            attach_command = await asyncio.to_thread(
                get_backend().prepare_web_attach,
                metadata["tmux_session"],
                metadata["tmux_window"],
            )
        except Exception as exc:  # noqa: BLE001 — reported as a failed open
            logger.warning("attach open failed for %s: %s", terminal_id, exc)
            return False

        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        env = dict(os.environ)
        if env.get("TERM", "dumb") == "dumb":
            env["TERM"] = "xterm-256color"
        proc = subprocess.Popen(
            attach_command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=os.setsid,
            env=env,
        )
        os.close(slave_fd)
        flag = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flag | os.O_NONBLOCK)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _on_pty_data():
            try:
                data = os.read(master_fd, 65536)
                queue.put_nowait(data if data else None)
            except BlockingIOError:
                pass
            except OSError:
                queue.put_nowait(None)

        loop.add_reader(master_fd, _on_pty_data)
        # Positions restart per attach: the attach stream is live-interaction
        # bytes, not replayable history — a reconnect renders a fresh screen
        # (tmux redraws), so no replay buffer is kept (#776 allows an explicit
        # fresh generation here).
        state = {
            "proc": proc,
            "master_fd": master_fd,
            "queue": queue,
            "pos": 0,
            "task": None,
        }
        state["task"] = asyncio.create_task(self._attach_pump(terminal_id, state))
        self._attach[terminal_id] = state
        return True

    async def _attach_pump(self, terminal_id: str, state: dict) -> None:
        while True:
            data = await state["queue"].get()
            if data is None:
                break
            pos = state["pos"]
            state["pos"] += len(data)
            await self._send(
                StreamFrame(
                    terminal_id=terminal_id,
                    stream=StreamName.ATTACH,
                    generation=0,
                    pos=pos,
                    data=base64.b64encode(data).decode(),
                )
            )
        # PTY hit EOF (client exited / detached): tell the server so it can
        # close the client-facing socket instead of leaving it silent. Sent only
        # while this state is still the terminal's live attach — a second
        # ATTACH_OPEN replaces the PTY, and the outgoing pump's EOF would
        # otherwise reach the server after the new client bound its sink and
        # close a session that had just opened.
        if self._attach.get(terminal_id) is state:
            await self._send(
                StreamFrame(
                    terminal_id=terminal_id,
                    stream=StreamName.ATTACH,
                    generation=0,
                    pos=state["pos"],
                    data="",
                )
            )

    def _attach_write(self, terminal_id: str, data: bytes) -> bool:
        state = self._attach.get(terminal_id)
        if state is None:
            return False
        try:
            os.write(state["master_fd"], data)
            return True
        except OSError:
            return False

    def _attach_resize(self, terminal_id: str, rows: int, cols: int) -> bool:
        import fcntl
        import struct
        import termios

        state = self._attach.get(terminal_id)
        if state is None:
            return False
        try:
            fcntl.ioctl(
                state["master_fd"], termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
            )
            return True
        except OSError:
            return False

    async def _attach_close(self, terminal_id: str) -> None:
        state = self._attach.pop(terminal_id, None)
        if state is None:
            return
        loop = asyncio.get_running_loop()
        try:
            loop.remove_reader(state["master_fd"])
        except (ValueError, OSError):
            pass
        task = state.get("task")
        if task is not None:
            task.cancel()
        try:
            state["proc"].terminate()
        except OSError:
            pass
        try:
            os.close(state["master_fd"])
        except OSError:
            pass

    # --- script execution (#745) ---

    async def _run_script(
        self,
        op_id: str,
        script_body: str,
        env: dict,
        timeout: float,
        term_grace: float,
        mode: str = "python",
    ) -> dict:
        """Run a workflow/flow script here, beside the agent, not on the server.

        Returns the RAW outcome (returncode, stdout, stderr, timed_out); the
        central server owns interpretation, journaling, generation fencing and
        the run record. The constructed env is used verbatim — the server built
        it through the ``build_env`` allowlist and rewrote ``CAO_API_BASE_URL``
        to a callback address the script can reach, so its ``workflow_return``
        calls route back to the central server.

        ``mode`` distinguishes the two kinds of user code that reach this seam.
        A workflow script is a Python file the server runs under its own
        interpreter, so ``python`` execs ``sys.executable``. A flow pre-script is
        documented as an executable file (`docs/flows.md`'s example is
        `#!/bin/bash`) and is run directly on the server host today, so
        ``executable`` reproduces that: mode `0700`, then exec the file itself
        and let its shebang choose the interpreter. Running a bash pre-script
        through ``sys.executable`` would fail as a Python SyntaxError blamed on
        the user's script, which is why the distinction is in the protocol rather
        than guessed from the body.
        """
        from cli_agent_orchestrator.constants import WORKFLOW_SCRIPT_LOG_CAP

        if mode not in ("python", "executable"):
            return {
                "returncode": None,
                "stdout": "",
                "stderr": f"unsupported script mode: {mode!r}",
                "timed_out": False,
            }

        with tempfile.TemporaryDirectory(prefix="cao-bridge-script-") as tmp:
            executable_mode = mode == "executable"
            script_path = os.path.join(tmp, "prescript" if executable_mode else "workflow.py")
            with open(script_path, "w") as f:
                f.write(script_body)
            # Owner-only: the file carries the caller's code into a pod that may
            # host other work, and it never needs to be readable by anyone else.
            os.chmod(script_path, 0o700 if executable_mode else 0o600)
            argv = [script_path] if executable_mode else [sys.executable, script_path]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    env=dict(env),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as exc:
                return {
                    "returncode": None,
                    "stdout": "",
                    "stderr": f"spawn failed: {exc}",
                    "timed_out": False,
                }
            self._script_procs[op_id] = proc
            timed_out = False
            try:
                try:
                    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                    await self._terminate_process(proc, term_grace)
                    out, err = b"", b""
                    try:
                        out, err = await asyncio.wait_for(proc.communicate(), timeout=term_grace)
                    except asyncio.TimeoutError:
                        pass
            finally:
                self._script_procs.pop(op_id, None)
            return {
                "returncode": proc.returncode,
                # Cap both streams so a runaway script cannot blow the frame size.
                "stdout": out.decode("utf-8", errors="replace")[-WORKFLOW_SCRIPT_LOG_CAP:],
                "stderr": err.decode("utf-8", errors="replace")[-WORKFLOW_SCRIPT_LOG_CAP:],
                "timed_out": timed_out,
            }

    async def _terminate_process(self, proc: asyncio.subprocess.Process, grace: float) -> None:
        """SIGTERM → grace → SIGKILL, mirroring the server-side reaper escalation."""
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    # --- connection lifecycle ---

    async def _serve(self, ws) -> None:
        self._ws = ws
        await ws.send(
            encode_frame(
                HelloFrame(
                    protocol_version=PROTOCOL_VERSION,
                    runtime_id=self._runtime_id,
                    streams=self._stream_positions(),
                    statuses=self._terminal_statuses(),
                )
            )
        )
        server_hello = decode_frame(await ws.recv())
        if not isinstance(server_hello, HelloFrame):
            raise ValueError("server did not answer hello with hello")
        if server_hello.protocol_version != PROTOCOL_VERSION:
            raise ValueError(
                f"protocol version mismatch: server {server_hello.protocol_version}, "
                f"bridge {PROTOCOL_VERSION}"
            )

        # Re-deliver results the server never acked, then replay stream bytes
        # from the server's resume positions (or emit an explicit gap).
        for result in list(self._unacked.values()):
            await self._send(result)
        for resume in server_hello.resume:
            buf = self._buffers.get(resume.terminal_id)
            if buf is None:
                continue
            start = resume.end_pos
            if start > buf.end_pos:
                # The server claims bytes past this buffer's watermark. It can
                # happen legitimately — a pane recovered under an id whose
                # earlier stream this server had consumed, so its position
                # belongs to a buffer that no longer exists — but it is never
                # ordinary, and `replay_from` would raise on it. Clamp so the
                # reconnect proceeds, and SAY SO: silently substituting a
                # different position leaves the server believing it is current
                # on a stream it is not (Copilot review on #802, finding 12).
                logger.warning(
                    "server resume position %s for terminal %s is past this "
                    "runtime's watermark %s; replaying from the watermark "
                    "(stream restarted under a reused id?)",
                    resume.end_pos,
                    resume.terminal_id,
                    buf.end_pos,
                )
                start = buf.end_pos
            # Gaps and bytes come back interleaved in stream order and are sent
            # that way: a hole the live path could not report (the channel was
            # already down when the bus dropped the chunk) is re-reported here,
            # ahead of the bytes that follow it, so the server never advances
            # past a range it has not received.
            for item in buf.replay_from(start):
                if isinstance(item, GapInfo):
                    await self._send(
                        GapFrame(
                            terminal_id=resume.terminal_id,
                            stream=StreamName.CAPTURE,
                            generation=buf.generation,
                            from_pos=item.from_pos,
                            to_pos=item.to_pos,
                        )
                    )
                    continue
                pos, chunk = item
                await self._send(
                    StreamFrame(
                        terminal_id=resume.terminal_id,
                        stream=StreamName.CAPTURE,
                        generation=buf.generation,
                        pos=pos,
                        data=base64.b64encode(chunk).decode(),
                    )
                )

        logger.info("runtime channel established to %s", self._server_url)
        # After the hello exchange and the replay, not at connect: a socket that
        # opened but failed version negotiation is not a working runtime. The
        # reconnect backoff reads the same flag for the same reason.
        self._established = True
        mark_channel_ready()
        async for raw in ws:
            frame: Frame = decode_frame(raw)
            if isinstance(frame, CommandFrame):
                # Concurrent dispatch: a slow LAUNCH must not block an input
                # or teardown for another terminal.
                asyncio.create_task(self._handle_command(frame))
            elif isinstance(frame, AckFrame):
                self._unacked.pop(frame.op_id, None)
            elif isinstance(frame, HeartbeatFrame):
                pass
            else:
                logger.warning("unexpected frame kind from server: %s", frame.kind)

    async def run(self) -> None:
        backoff = RECONNECT_BACKOFF_INITIAL
        # A marker left behind by a killed predecessor sharing this mount would
        # report a channel that does not exist, so start from unready.
        clear_channel_ready()
        while not self._stop.is_set():
            self._established = False
            # Held rather than logged in place, so the delay in the message is
            # the delay actually waited — it is decided below, not here.
            reason = None
            try:
                async with websockets.connect(
                    self._server_url,
                    additional_headers={RUNTIME_TOKEN_HEADER: self._token},
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    await self._serve(ws)
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.InvalidStatus as e:
                status_code = e.response.status_code
                if status_code in (401, 403):
                    # Auth failure is surfaced and fatal — never retried as if
                    # it were a transient network error (#776).
                    logger.error("runtime channel authentication rejected (%s)", status_code)
                    raise
                reason = f"rejected ({e})"
            except Exception as e:
                reason = f"lost ({e})"
            finally:
                self._ws = None
                clear_channel_ready()
            if self._established:
                # A channel that actually worked starts the next backoff over;
                # one that only opened does not. Resetting at connect instead
                # pins a permanently incompatible runtime — wrong PROTOCOL_VERSION,
                # say — to a 1s retry forever, because its failure always comes
                # after the socket is up. It never becomes Ready either way, so
                # the only thing the tight loop produces is log volume.
                backoff = RECONNECT_BACKOFF_INITIAL
            if reason is not None:
                logger.warning("runtime channel %s; retrying in %.0fs", reason, backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)

    def stop(self) -> None:
        """SIGTERM/SIGINT: stop reconnecting, and close a live channel now.

        Setting the event alone is not enough while a channel is up: `_serve` is
        parked on the socket and would notice nothing until the kubelet's SIGKILL
        at the end of the pod's termination grace period. Closing here runs the
        reconnect loop's `finally`, so readiness is withdrawn while the pod is
        still shutting down and the server sees a close rather than a connection
        that died with the process.
        """
        self._stop.set()
        ws = self._ws
        if ws is None:
            return
        try:
            asyncio.get_running_loop().create_task(ws.close())
        except RuntimeError:
            # No running loop, so there is no `_serve` parked on this socket
            # either; the event on its own ends the reconnect loop.
            pass


async def _amain() -> None:
    server_url = os.environ.get("CAO_BRIDGE_SERVER_URL")
    runtime_id = os.environ.get("CAO_BRIDGE_RUNTIME_ID")
    token = os.environ.get("CAO_RUNTIME_TOKEN")
    if not server_url or not runtime_id or not token:
        raise SystemExit(
            "cao-bridge requires CAO_BRIDGE_SERVER_URL, CAO_BRIDGE_RUNTIME_ID "
            "and CAO_RUNTIME_TOKEN"
        )

    setup_logging()
    # The RUNTIME initializer, not ``init_db``: this process executes panes and
    # owns no orchestration state, so it creates the pane-local tables
    # ``terminal_service`` writes and leaves the control plane's schemas to the
    # server (Copilot review on #802, finding 5).
    init_runtime_db()
    loop = asyncio.get_running_loop()
    bus.set_loop(loop)

    bridge = Bridge(server_url, runtime_id, token)
    tasks = [
        asyncio.create_task(status_monitor.run()),
        asyncio.create_task(log_writer.run()),
        asyncio.create_task(bridge._forward_output()),
        asyncio.create_task(bridge._forward_status()),
        asyncio.create_task(bridge._heartbeat()),
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
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
