"""Server-side registry for connected execution runtimes (#745).

Owns three associations the shared cao-server needs to route work:

- runtime_id → live channel connection (one persistent outbound WebSocket per
  runtime; the runtime dials the server, never the reverse),
- terminal_id → runtime_id (which runtime executes a given terminal),
- terminal_id → last worker-reported status and stream positions.

The terminal→runtime binding is rebuilt from each runtime's hello snapshot on
reconnect, so a server restart recovers routing without persisting live
channel state. A terminal whose runtime is disconnected reports UNKNOWN — the
server never guesses, and never falls back to probing local tmux for a remote
terminal.
"""

import asyncio
import logging
import threading
import time
import uuid
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandFrame,
    CommandResultFrame,
    CommandType,
    StreamName,
    encode_frame,
)

logger = logging.getLogger(__name__)

# Generous by design: LAUNCH runs full provider initialization in the runtime
# (tens of seconds for a cold CLI). Terminal-scoped commands pass a tighter
# per-call timeout at the call site.
DEFAULT_COMMAND_TIMEOUT = 60.0

# Per-operation deadlines. They live beside the registry rather than in the HTTP
# layer because non-HTTP senders need them too: terminal_service routes a remote
# send_input from any caller, and importing the endpoint module for a number
# would drag the FastAPI/auth surface into the service layer.
LAUNCH_TIMEOUT = 240.0
INPUT_TIMEOUT = 60.0
EXTRACT_TIMEOUT = 60.0
TEARDOWN_TIMEOUT = 120.0


class RuntimeUnavailableError(Exception):
    """No connected runtime can execute this operation right now.

    On its own this says nothing about whether the work happened: a channel that
    closes while a command is in flight raises it for an operation the runtime
    may have already performed. Callers that retry MUST check for the subclass
    below instead.
    """


class RuntimeNotDispatchedError(RuntimeUnavailableError):
    """The command never reached a runtime, so retrying cannot duplicate work.

    The distinction is the difference between a retry and a double delivery. An
    inbox message for a supervisor in another pod is the case that bites: with
    one exception type for both, "the runtime was gone before we sent" and "the
    channel dropped after the runtime typed the message" were classified the
    same way — transient — and the reconcile sweep re-delivered a message the
    supervisor had already received, as though the worker had answered twice
    (review finding 6 on #802).

    Raised only where nothing was put on the wire. A dispatched command whose
    answer never came back stays ``RuntimeUnavailableError``: unknown, and not
    for this layer to guess about.
    """


class PlacementUnavailableError(Exception):
    """The durable placement row could not be read, so placement is UNKNOWN.

    Distinct from a confirmed-local terminal (``None``): callers must not treat
    a transient read failure as "local" and drive this host's tmux for a
    terminal that may be remote (guojing1217 on #802). Never cached.
    """


class RemoteCommandError(Exception):
    """The runtime executed the command and reported a failure."""

    def __init__(self, outcome: str, message: str):
        self.outcome = outcome
        super().__init__(message)


class RuntimeConnection:
    """One live channel to a runtime, with op_id-correlated command futures."""

    def __init__(self, runtime_id: str, send_text: Callable[[str], Awaitable[None]]):
        self.runtime_id = runtime_id
        self._send_text = send_text
        self._pending: Dict[str, asyncio.Future] = {}
        self.connected_at = time.time()
        self.last_seen = time.time()
        # Set by the registry at register(); 0 means "never registered", which is
        # what an unregistered connection built directly in a test has.
        self.incarnation = 0
        # Set the moment the registry knows this channel is gone (disconnect, or
        # superseded by a reconnect). It is the ONLY basis on which a send
        # failure may be reported as "never dispatched" — see send_command.
        self.closed = False

    async def send_command(
        self,
        command_type: CommandType,
        payload: dict,
        terminal_id: Optional[str] = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        op_id: Optional[str] = None,
    ) -> CommandResultFrame:
        """Send one correlated command and wait for its retained result.

        A timeout here means the RESPONSE is missing, not that the work did
        not happen — callers must treat it as unknown, never resubmit the same
        work blindly (#745).

        ``op_id`` lets a caller that must later *refer* to this operation mint the
        identity itself. The remote script driver is the case that needs it: it
        records ``(runtime_id, op_id)`` on the run so ``cancel_script_run`` can
        relay ``CANCEL_SCRIPT(target_op_id=...)``, and the bridge indexes the
        subprocess under the op_id of the command it received. When this method
        minted its own, those were two different UUIDs and the cancel named an
        operation the runtime had never seen: the signal reached nothing and the
        script ran to completion while the record journalled CANCELLED (review
        finding 1 on #802). Everything else passes nothing and gets a fresh id.
        """
        op_id = op_id or uuid.uuid4().hex
        frame = CommandFrame(
            op_id=op_id, terminal_id=terminal_id, type=command_type, payload=payload
        )
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[op_id] = future
        try:
            if self.closed:
                # Provably nothing on the wire: the registry had already declared
                # this channel gone before the attempt. This is the ONLY case that
                # may claim non-dispatch, because it is the only one that can.
                raise RuntimeNotDispatchedError(
                    f"channel to runtime {self.runtime_id} was already closed; "
                    f"{command_type.value} was not sent"
                )
            try:
                await self._send_text(encode_frame(frame))
            except Exception as exc:  # noqa: BLE001 — transport failure, any kind
                # A send raising does NOT prove the frame never went out: the
                # connection can close after the bytes are written and before the
                # await returns. Classifying that as non-dispatch let inbox
                # delivery retry an input the runtime may already have typed,
                # duplicating a delegated result — the exact failure
                # RuntimeNotDispatchedError exists to prevent (Copilot review on
                # #802). Unknown is the honest answer, and the retryable subclass
                # is reserved for the provable case above.
                raise RuntimeUnavailableError(
                    f"send of {command_type.value} to runtime {self.runtime_id} failed "
                    f"with the frame's fate unknown: {exc}"
                ) from exc
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(op_id, None)

    def resolve(self, result: CommandResultFrame) -> bool:
        """Complete the waiting future for ``result.op_id``.

        Returns whether the op was one this process was waiting on. ``False``
        means the result was retained by the runtime for an op sent before a
        server restart — the caller may need to reconcile it (a lost LAUNCH
        leaves a live terminal the restarted server never persisted) before
        acking, rather than discard it.
        """
        future = self._pending.get(result.op_id)
        if future is not None:
            if not future.done():
                future.set_result(result)
            return True
        # A result for an op this process never sent (e.g. retained from before
        # a server restart). Not raised: the runtime needs its ack regardless.
        logger.info(
            "runtime %s delivered result for unknown op %s (server restart?)",
            self.runtime_id,
            result.op_id,
        )
        return False

    def fail_all_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeUnavailableError(reason))
        self._pending.clear()


class RuntimeChannelRegistry:
    """Process-wide routing state for remote execution runtimes."""

    def __init__(self) -> None:
        self._runtimes: Dict[str, RuntimeConnection] = {}
        self._terminal_runtime: Dict[str, str] = {}
        self._status: Dict[str, TerminalStatus] = {}
        # Last stream position the server has consumed, per (terminal, stream).
        # Returned to the runtime on hello so it can replay what this server
        # missed while disconnected (bounded by the runtime's replay window).
        self._positions: Dict[Tuple[str, str], int] = {}
        # Last stream generation seen per (terminal, stream). Positions are only
        # comparable within one generation, so this is what lets record_position
        # tell a new stream numbered from 0 from a rewind of the old one.
        self._generations: Dict[Tuple[str, str], int] = {}
        # Live interactive-attach clients by terminal (#776).
        self._attach_sinks: Dict[str, "asyncio.Queue"] = {}
        # The loop the channel connections belong to, captured at register().
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # How many times each runtime id has been claimed by a channel in this
        # process. Stamped onto the connection so a report can be checked against
        # the incarnation that is current now, not the one that sent it.
        self._incarnations: Dict[str, int] = {}
        # Placement as recovered from the central row, for terminals this process
        # has not (yet) seen a hello for. ``None`` means "the row says local", so
        # a local terminal is asked about the database once, not on every poll.
        self._recovered_placement: Dict[str, Optional[str]] = {}
        # The channel loop mutates the routing/status dicts on the event-loop
        # thread; effective_status reads them from a worker thread (via
        # asyncio.to_thread), so a disconnect could land between get_status's
        # liveness check and its status read and return a stale COMPLETED that
        # satisfies a wait after the runtime is already gone (Copilot follow-up
        # on #802). This guards the in-memory (_runtimes, _terminal_runtime,
        # _status, _incarnations, _positions) reads/writes across that thread
        # boundary. Reentrant so a guarded method can call another; DB I/O is
        # kept OUT of the lock so a worker thread's placement read never blocks
        # the event loop.
        self._lock = threading.RLock()

    # --- runtime lifecycle ---

    def register(
        self, runtime_id: str, send_text: Callable[[str], Awaitable[None]]
    ) -> RuntimeConnection:
        existing = self._runtimes.get(runtime_id)
        if existing is not None:
            # A reconnect superseding a half-open connection: fail the old
            # channel's waiters rather than leaving them to time out.
            existing.closed = True
            existing.fail_all_pending(f"runtime {runtime_id} reconnected on a new channel")
        conn = RuntimeConnection(runtime_id, send_text)
        # The incarnation of a runtime id: one more executor process (or one more
        # channel from the same one) claiming this identity. Reported state is
        # accepted only from the current incarnation -- see ``set_status``.
        with self._lock:
            self._incarnations[runtime_id] = self._incarnations.get(runtime_id, 0) + 1
            conn.incarnation = self._incarnations[runtime_id]
            self._runtimes[runtime_id] = conn
        # Capture the loop the channels live on. Command futures are created on
        # it (see RuntimeConnection.send_command), so a caller on a worker thread
        # has no way to dispatch without it — see send_terminal_command_blocking.
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:  # registered off-loop (unit tests) — nothing to capture
            pass
        logger.info("runtime channel registered: %s", runtime_id)
        return conn

    def unregister(self, runtime_id: str, conn: RuntimeConnection) -> None:
        # Guard against a stale disconnect handler unregistering the NEW
        # connection after a fast reconnect replaced it.
        with self._lock:
            if self._runtimes.get(runtime_id) is conn:
                del self._runtimes[runtime_id]
                logger.info("runtime channel disconnected: %s", runtime_id)
        conn.closed = True
        conn.fail_all_pending(f"runtime {runtime_id} channel closed")

    def get_runtime(self, runtime_id: str) -> Optional[RuntimeConnection]:
        return self._runtimes.get(runtime_id)

    def list_runtimes(self) -> Dict[str, dict]:
        with self._lock:
            return {
                rid: {
                    "connected_at": conn.connected_at,
                    "last_seen": conn.last_seen,
                    "terminals": sorted(t for t, r in self._terminal_runtime.items() if r == rid),
                }
                for rid, conn in self._runtimes.items()
            }

    # --- terminal routing ---

    def bind_terminal(self, terminal_id: str, runtime_id: str) -> None:
        """Unconditional bind. The server owns this call — it is made once, at
        creation, by ``POST /runtimes/{id}/terminals`` right after it has
        written the durable placement row. Inbound channel frames must go
        through :meth:`claim_terminal` instead, which enforces ownership."""
        with self._lock:
            self._terminal_runtime[terminal_id] = runtime_id
            self._recovered_placement.pop(terminal_id, None)

    def claim_terminal(self, terminal_id: str, runtime_id: str) -> bool:
        """A runtime asserts, over the channel, that it owns ``terminal_id``.

        ``CAO_RUNTIME_TOKEN`` is one secret shared by the whole fleet, so a
        connected runtime is only ever proven to be *some* authorized executor —
        never proven to be the one that launched this terminal. Binding on that
        assertion alone let any runtime claim any terminal id: routing, replay
        position and the next input then followed the claimant, and on the next
        real owner's heartbeat the binding flapped back, so an operator saw
        routing change by itself (guojing1217 + Copilot reviews on #802,
        reproduced on EKS). ``generation`` was meant to fence a takeover but is
        never advanced, so it cannot tell a takeover from a continuation.

        The bind is therefore allowed only when the claim is consistent with an
        authority this runtime cannot forge:

        * the terminal is already bound here to this same runtime — a
          continuation, the common case for every frame after the first;
        * the durable central row names this runtime — the restarted-server
          recovery path, where the launching runtime's own hello re-establishes
          the binding;
        * no central row exists at all — a phantom id with no pane and no output
          to hijack, and the window a tracked launch/reconcile binds through
          before its row is committed.

        A row that exists but names NO runtime is a confirmed-LOCAL terminal, and
        a runtime claiming it would redirect a real local pane's routing/status;
        that is refused, distinct from the no-row case (Copilot follow-up on
        #802). So is a row naming a different runtime, and an unreadable row. A
        refused claim leaves the existing binding untouched and is logged.
        Returns ``True`` when bound to ``runtime_id`` on return, else ``False``.
        """
        current = self._terminal_runtime.get(terminal_id)
        if current == runtime_id:
            return True
        if current is not None:
            logger.warning(
                "runtime %s tried to claim terminal %s already bound to %s; refusing",
                runtime_id,
                terminal_id,
                current,
            )
            return False
        try:
            state, placement = self._placement_state(terminal_id)
        except PlacementUnavailableError:
            # Cannot confirm ownership: refuse rather than bind on a claim the
            # durable row would have adjudicated. The runtime retries its hello.
            logger.warning(
                "runtime %s claim of terminal %s refused: placement unreadable",
                runtime_id,
                terminal_id,
            )
            return False
        if state == "named" and placement != runtime_id:
            logger.warning(
                "runtime %s tried to claim terminal %s placed on %s; refusing",
                runtime_id,
                terminal_id,
                placement,
            )
            return False
        if state == "local":
            logger.warning(
                "runtime %s tried to claim terminal %s, which is a local terminal; refusing",
                runtime_id,
                terminal_id,
            )
            return False
        self.bind_terminal(terminal_id, runtime_id)
        return True

    def unbind_terminal(self, terminal_id: str) -> None:
        with self._lock:
            self._terminal_runtime.pop(terminal_id, None)
            self._recovered_placement.pop(terminal_id, None)
            self._status.pop(terminal_id, None)
            for stream in StreamName:
                self._positions.pop((terminal_id, stream.value), None)
                self._generations.pop((terminal_id, stream.value), None)

    def _placement_from_the_central_row(self, terminal_id: str) -> Optional[str]:
        """The runtime this terminal was launched on, per the persisted row.

        Bindings live in this process, but the server they belong to is
        replaceable: a rollout, a crash loop or a scale-out gives a new pod an
        empty ``_terminal_runtime`` while every remote terminal it now serves is
        still running in its executor. Until that executor's hello arrives --
        seconds at best, indefinitely if the executor is gone -- a placement
        question answered from memory alone says "local", and the caller drives
        the CONTROLLER's tmux: on a pure controller that fails against a tmux
        that was never there, and on a hybrid host it can address an unrelated
        local pane (review finding 5 on #802).

        ``POST /runtimes/{id}/terminals`` writes ``metadata.runtime_id`` for
        exactly this reason, so the answer is durable; it just was not being
        read. Recovered placement is cached (including the "this row is local"
        answer, as ``None``) because a terminal's placement is fixed when it is
        created, and ``is_remote`` is on the status-poll path.

        Returns ``None`` for a confirmed-local terminal (a row with no
        ``runtime_id``) AND for an absent row, and the runtime id for a
        confirmed-remote one — the distinction absent-vs-local is exposed by
        :meth:`_placement_state`, which ``claim_terminal`` needs and this
        (``is_remote``/``runtime_for_terminal``) does not. Raises
        :class:`PlacementUnavailableError` when the row cannot be read: that is
        an UNKNOWN placement, distinct from a confirmed-local one, and callers
        must not collapse the two — answering "local" on a transient read
        failure drove status polling and command routing into the controller's
        own tmux for a terminal that may be remote (guojing1217 on #802). The
        failure is never cached, so a later read can still learn the truth.
        """
        _state, runtime_id = self._placement_state(terminal_id)
        return runtime_id

    def _placement_state(self, terminal_id: str) -> Tuple[str, Optional[str]]:
        """Placement per the durable row, distinguishing absent from local.

        ``("named", runtime_id)`` — the row places it on that runtime;
        ``("local", None)`` — the row exists but names no runtime (a local pane);
        ``("absent", None)`` — no row at all.

        Only a successful read is cached (a terminal's placement is fixed at
        creation); an absent row is not cached, so a row written moments later —
        a tracked launch, a reconcile — is seen. Raises
        :class:`PlacementUnavailableError` on a read failure.
        """
        if terminal_id in self._recovered_placement:
            runtime_id = self._recovered_placement[terminal_id]
            return ("named", runtime_id) if runtime_id is not None else ("local", None)
        try:
            from cli_agent_orchestrator.clients.database import get_terminal_metadata

            row = get_terminal_metadata(terminal_id)
        except Exception as exc:  # noqa: BLE001 — surfaced as an unknown placement
            logger.warning("placement lookup for terminal %s failed: %s", terminal_id, exc)
            raise PlacementUnavailableError(str(exc)) from exc
        if row is None:
            return ("absent", None)
        metadata = row.get("metadata") or {}
        value = metadata.get("runtime_id")
        runtime_id = str(value) if value else None
        self._recovered_placement[terminal_id] = runtime_id
        return ("named", runtime_id) if runtime_id is not None else ("local", None)

    def is_remote(self, terminal_id: str) -> bool:
        """Whether this terminal executes in a runtime rather than on this host.

        Deliberately NOT a liveness question: a terminal whose executor is
        disconnected is still remote, and the command path says "runtime X is not
        connected" rather than silently running the work here.

        Fails closed toward remote/unknown when the placement row cannot be
        read: during a database outage the row is unreadable anyway, so the
        caller gets a retryable "runtime not connected" / UNKNOWN status rather
        than the local arm quietly driving this host's tmux for a terminal that
        may live in a runtime (guojing1217 on #802).
        """
        if terminal_id in self._terminal_runtime:
            return True
        try:
            return self._placement_from_the_central_row(terminal_id) is not None
        except PlacementUnavailableError:
            return True

    def runtime_for_terminal(self, terminal_id: str) -> Optional[str]:
        bound = self._terminal_runtime.get(terminal_id)
        if bound is not None:
            return bound
        try:
            return self._placement_from_the_central_row(terminal_id)
        except PlacementUnavailableError:
            # Which runtime is genuinely unknown — not "definitely local". The
            # command path pairs this with ``is_remote`` (True on the same
            # error), so the operation routes remote and fails with "runtime not
            # connected" rather than running here.
            return None

    def remote_terminal_ids(self) -> List[str]:
        """Every terminal bound to a runtime that is currently CONNECTED.

        A snapshot (new list), not a view: callers iterate it while awaiting DB
        reads, and a channel disconnecting mid-iteration must not raise
        "dictionary changed size during iteration" at them.

        ``list_runtimes`` answers the same question per connected runtime; this
        is for callers that need the whole set without caring which runtime owns
        which -- ``session_service.list_sessions``, which turns it into the set
        of sessions executing off-box.

        A binding OUTLIVES its channel on purpose: ``unregister`` leaves
        ``_terminal_runtime`` intact so a command for a terminal whose executor
        vanished fails with "runtime X is not connected" rather than being
        misread as a terminal that never existed. That makes the raw dict the
        wrong answer for a liveness question -- ``GET /sessions`` listed a dead
        pod's sessions as active indefinitely (Copilot review on #802, finding
        13). Routing keeps the full map; enumeration gets only the live part.

        Built under the lock: a channel disconnecting on the event-loop thread
        mid-iteration would otherwise raise "dictionary changed size during
        iteration" at this worker-thread caller.
        """
        with self._lock:
            return [tid for tid, rid in self._terminal_runtime.items() if rid in self._runtimes]

    async def send_terminal_command(
        self,
        terminal_id: str,
        command_type: CommandType,
        payload: dict,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> CommandResultFrame:
        # Recovered placement counts: after a server replacement the row is the
        # only record of where this terminal runs, and "not connected" is the
        # honest answer to give until its executor says hello again.
        runtime_id = self.runtime_for_terminal(terminal_id)
        if runtime_id is None:
            raise RuntimeNotDispatchedError(f"terminal {terminal_id} is not bound to a runtime")
        conn = self._runtimes.get(runtime_id)
        if conn is None:
            raise RuntimeNotDispatchedError(
                f"runtime {runtime_id} for terminal {terminal_id} is not connected"
            )
        return await conn.send_command(
            command_type, payload, terminal_id=terminal_id, timeout=timeout
        )

    def send_terminal_command_blocking(
        self,
        terminal_id: str,
        command_type: CommandType,
        payload: dict,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> CommandResultFrame:
        """Route one terminal-scoped operation from a worker thread.

        Some senders are synchronous by construction and run off the loop: inbox
        delivery is the one that matters (a worker's completion callback for a
        supervisor whose session is in another pod), and it is already dispatched
        via ``asyncio.to_thread``. Without this, such a caller would reach past
        the channel into local tmux and fail with "session not found" for a
        terminal that is alive in its own runtime.

        Refuses to run on the loop thread: ``Future.result()`` there would block
        the very loop that has to deliver the frame, deadlocking until timeout.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeNotDispatchedError(
                f"no runtime channel loop is available to reach terminal {terminal_id}"
            )
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            raise RuntimeError(
                "send_terminal_command_blocking called on the channel loop; "
                "await send_terminal_command instead"
            )
        future = asyncio.run_coroutine_threadsafe(
            self.send_terminal_command(terminal_id, command_type, payload, timeout=timeout),
            loop,
        )
        # The coroutine already enforces `timeout`; this one only bounds the
        # handoff itself, so it is the same deadline plus a small margin rather
        # than a second, independent one.
        return future.result(timeout + 5.0)

    # --- worker-reported state ---

    def set_status(
        self,
        terminal_id: str,
        status: TerminalStatus,
        conn: Optional[RuntimeConnection] = None,
    ) -> None:
        """Record what a runtime says this terminal is doing.

        ``conn`` is the channel the report arrived on. A report from a connection
        that is no longer the registered one for its runtime id is DROPPED: it
        describes a process that has already been replaced, and applying it lets
        an older incarnation's ``COMPLETED`` overwrite the live incarnation's
        ``PROCESSING`` -- a supervisor then reads a finished worker that is still
        mid-task, or the reverse (review finding 2 on #802). The protocol carries
        a ``generation`` field intended for this, but nothing in the codebase ever
        advances it, so the identity and incarnation of the registered connection
        is the fence that actually holds.

        Callers with no connection in hand (the local status monitor's own
        bookkeeping) pass nothing and are unaffected.
        """
        with self._lock:
            if conn is not None:
                current = self._runtimes.get(conn.runtime_id)
                if current is not conn or conn.incarnation != self._incarnations.get(
                    conn.runtime_id, 0
                ):
                    logger.warning(
                        "dropping %s report for terminal %s from superseded incarnation %s of "
                        "runtime %s (current: %s)",
                        status.value,
                        terminal_id,
                        conn.incarnation,
                        conn.runtime_id,
                        self._incarnations.get(conn.runtime_id, 0),
                    )
                    return
            self._status[terminal_id] = status

    def reconcile_hello(self, runtime_id: str, advertised: Iterable[str]) -> List[str]:
        """Forget state for terminals the runtime's new hello does not claim.

        A hello is the incoming incarnation's complete statement of what it owns.
        Anything this process still has bound to that runtime and absent from the
        statement no longer exists: the pod was replaced, or restarted, and its
        panes died with it. Leaving the cached status behind meant a reconnect
        RESURRECTED a dead terminal's last ``PROCESSING`` or ``COMPLETED`` --
        answered as current, because the runtime is connected again (review
        finding 2 on #802). Stream positions go too; they describe a byte stream
        that no longer has a producer.

        The binding is deliberately KEPT. The central row still says this terminal
        was placed on this runtime, and "bound, status unknown" is the truthful
        state; dropping the binding would make the terminal look local, which is
        the failure mode finding 5 is about. Returns the ids it invalidated so the
        caller can log or publish them.
        """
        claimed = set(advertised)
        with self._lock:
            stale = [
                tid
                for tid, rid in self._terminal_runtime.items()
                if rid == runtime_id and tid not in claimed
            ]
            for tid in stale:
                self._status.pop(tid, None)
                for stream in StreamName:
                    self._positions.pop((tid, stream.value), None)
                    self._generations.pop((tid, stream.value), None)
        if stale:
            logger.warning(
                "runtime %s reconnected without terminals %s; their cached state is discarded",
                runtime_id,
                sorted(stale),
            )
        return stale

    def get_status(self, terminal_id: str) -> TerminalStatus:
        # Compound read held under the lock so a concurrent unregister/reconcile
        # on the event-loop thread cannot land between the liveness check and the
        # status read and leave a stale value looking current.
        with self._lock:
            runtime_id = self._terminal_runtime.get(terminal_id)
            if runtime_id is None or runtime_id not in self._runtimes:
                # No live channel: the server cannot know. Explicit UNKNOWN beats
                # a stale last report presented as current (#745 failure posture).
                return TerminalStatus.UNKNOWN
            return self._status.get(terminal_id, TerminalStatus.UNKNOWN)

    def is_stale_generation(self, terminal_id: str, stream: str, generation: int) -> bool:
        """Whether a frame's generation is BEHIND the stream's current one.

        Positions are only comparable within one generation. When the runtime
        re-arms a reader (or a terminal id is reused) it begins a new generation
        numbered from 0; frames still arriving from the superseded stream carry
        the old generation and must be dropped rather than mixed into the new
        stream's transcript (Copilot review on #802).
        """
        with self._lock:
            known = self._generations.get((terminal_id, stream))
            return known is not None and generation < known

    def record_position(
        self, terminal_id: str, stream: str, end_pos: int, generation: Optional[int] = None
    ) -> None:
        """Advance the consumed watermark for one (terminal, stream).

        ``generation`` fences a stream RESTART:

        - a HIGHER generation is a new stream, so the watermark is SET to its
          position rather than max'd against the old stream's — a new stream
          numbered from 0 would otherwise read as a rewind and be ignored,
          leaving the server resuming the new stream at a dead stream's offset;
        - a LOWER generation is a stale frame and changes nothing (callers should
          drop such frames outright via :meth:`is_stale_generation`);
        - the SAME generation keeps the monotonic-max behaviour.

        ``generation=None`` (local bookkeeping, tests) skips the fence entirely.
        """
        with self._lock:
            key = (terminal_id, stream)
            if generation is not None:
                known = self._generations.get(key)
                if known is not None and generation < known:
                    return
                if known is None:
                    self._generations[key] = generation
                elif generation > known:
                    self._generations[key] = generation
                    logger.info(
                        "terminal %s %s advanced to generation %s; watermark reset to %s",
                        terminal_id,
                        stream,
                        generation,
                        end_pos,
                    )
                    self._positions[key] = end_pos
                    return
            if end_pos > self._positions.get(key, 0):
                self._positions[key] = end_pos

    def resume_position(self, terminal_id: str, stream: str) -> int:
        return self._positions.get((terminal_id, stream), 0)

    # --- interactive attach relay (#776) ---
    #
    # One live attach client per terminal: attach bytes arriving on the
    # channel are handed to that client's queue instead of the bus. `None`
    # on the queue means the runtime-side PTY ended (EOF/detach).

    def bind_attach(self, terminal_id: str, sink: "asyncio.Queue") -> bool:
        """Make *sink* the live attach client, ending any client it displaces.

        Returns True when this call displaced an earlier client. That client is
        sent ``None`` — the same EOF the runtime PTY's own end produces — so its
        relay unwinds immediately instead of parking forever on a queue nothing
        will ever feed again (Copilot review on #802, finding 6). Ownership is
        what the ``None`` conveys, not a PTY that ended: the PTY is still alive
        and now belongs to *sink*, which is why the displaced relay must not go
        on to close it (see :meth:`unbind_attach`).
        """
        previous = self._attach_sinks.get(terminal_id)
        self._attach_sinks[terminal_id] = sink
        if previous is not None and previous is not sink:
            previous.put_nowait(None)
            return True
        return False

    def unbind_attach(self, terminal_id: str, sink: "asyncio.Queue") -> bool:
        """Release *sink*'s claim; True only if it was still the live client.

        The return value is the caller's authority to send ``ATTACH_CLOSE``. A
        relay that was displaced by a later client must not: the runtime-side
        PTY it would close is now the replacement's, so an unconditional close
        in the old relay's ``finally`` killed a live attach (finding 4).
        """
        if self._attach_sinks.get(terminal_id) is sink:
            del self._attach_sinks[terminal_id]
            return True
        return False

    def deliver_attach(self, terminal_id: str, data: Optional[bytes]) -> bool:
        sink = self._attach_sinks.get(terminal_id)
        if sink is None:
            return False
        sink.put_nowait(data)
        return True


runtime_registry = RuntimeChannelRegistry()
