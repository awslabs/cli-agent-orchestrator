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
import uuid
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict

from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import delete_terminal as db_delete_terminal
from cli_agent_orchestrator.clients.database import (
    get_dispatch_record,
    get_terminal_metadata,
    record_dispatch,
    settle_dispatch,
)
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
    RuntimeNotDispatchedError,
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


def _note_heartbeat_watermark(behind: dict, stream_pos: StreamPosition, runtime_id: str) -> None:
    """Say so when a heartbeat shows the server is behind the runtime's stream.

    The heartbeat exists so the loss of a FINAL chunk is detectable when no later
    output ever arrives (#776), but the handler only rebound routing and threw the
    watermarks away, so nothing acted on the one signal that carries them
    (Copilot review on #802).

    What this deliberately does NOT do is adopt the advertised ``end_pos`` as the
    resume position, which was the reviewed suggestion. The recorded position
    means "bytes the server has actually received". Those undelivered bytes are
    still in the runtime's replay buffer -- ``Bridge._send`` swallows a
    ``ConnectionClosed``, which is exactly how a chunk goes missing while the
    buffer keeps it -- so the stale watermark is what makes the next reconnect
    replay them. Moving it forward would mark unreceived output as received and
    throw away the only path that recovers it, turning a recoverable gap into
    silent loss. If the range is evicted before that reconnect, the reconnect's
    bounded ``GapFrame`` reports the loss and advances the watermark; that is the
    path allowed to declare bytes gone, because the runtime is the only party
    that knows they are.

    So the watermark is left alone and the discrepancy is stated. It is only
    reported once it has survived two consecutive heartbeats without progress: a
    reconnect's replay is a stream of chunks, a heartbeat can interleave between
    them, and a warning that fires during normal recovery is a warning operators
    learn to ignore.
    """
    key = (stream_pos.terminal_id, stream_pos.stream.value)
    recorded = runtime_registry.resume_position(*key)
    if stream_pos.end_pos <= recorded:
        behind.pop(key, None)
        return

    previous = behind.get(key)
    if previous == (stream_pos.end_pos, stream_pos.generation, recorded):
        logger.warning(
            "runtime %s reports terminal %s %s at %s but this server has only "
            "received %s; %s bytes never arrived and are awaiting a reconnect "
            "replay (generation %s)",
            runtime_id,
            stream_pos.terminal_id,
            key[1],
            stream_pos.end_pos,
            recorded,
            stream_pos.end_pos - recorded,
            stream_pos.generation,
        )
        behind.pop(key, None)
        return

    behind[key] = (stream_pos.end_pos, stream_pos.generation, recorded)


async def _reconcile_orphaned_result(frame: CommandResultFrame, runtime_id: str) -> bool:
    """Recover a live terminal from a LAUNCH result redelivered after a restart.

    The runtime retains each result until it is acked, so after the server
    restarts it redelivers the ones the old process never acknowledged. Most are
    harmless to drop, but a successful LAUNCH is not: its terminal is running in
    the runtime, yet the restarted server has no row and no binding for it, so
    without this it would ack-and-drop and orphan a live agent nothing can route
    to or tear down (guojing1217 on #802).

    A successful LAUNCH is reconciled only when no central row exists yet (a row
    already present means the old server persisted it before crashing — bind and
    move on). ``owner`` comes from the dispatch journal, which recorded it when
    the command was sent and never told the runtime; a reconciled terminal is
    therefore owned by whoever asked for it, not unowned.

    Returns whether it is safe to ACK. A reconciliation that FAILED must not be
    acked: the ack is what lets the runtime drop its only retained copy of the
    result, so acking a failure would leave an untracked agent running with
    nothing able to route to or tear it down (Copilot review on #802). Leaving it
    unacked costs a re-delivery on the next reconnect — bounded by reconnects,
    not a hot loop — and that retry is the durable recovery path.

    The dispatch journal is the authority for everything here.
    ``RuntimeConnection.resolve`` answers False for any op_id it does not hold in
    memory, which after a restart is every op_id, so "unmatched" alone said
    nothing about whether this server ever asked for the operation. Without the
    journal a shared-token runtime could fabricate a result naming an arbitrary
    terminal id and have it persisted, placed on itself, and — since the row was
    written with no owner, and ``may_start_work(None)`` is allowed — permitted to
    work. The row this function wrote was the only thing authorizing the claim it
    then made (Copilot review on #802).
    """
    info = frame.payload.get("terminal")
    has_terminal = isinstance(info, dict) and bool(info.get("id"))

    record = None
    try:
        record = get_dispatch_record(frame.op_id)
    except Exception:
        # Provenance is MANDATORY only when applying this result would create or
        # destroy state; otherwise the journal is an optimisation and an unreadable
        # one must not withhold the ack.
        #
        # Getting that backwards wedged the channel: a frame carrying no terminal
        # at all — a result whose caller has simply gone away — went unacked
        # because the journal could not be read, and since an unreadable journal
        # stays unreadable, redelivery hit the same wall forever. Nothing was at
        # stake in those frames to justify it.
        logger.exception("could not read the dispatch journal for op %s", frame.op_id)
        return not has_terminal
    if record is None:
        # Not ours. Nothing to recover, and nothing to create: acking discards a
        # result no operation asked for, which is the correct outcome for a forged
        # or foreign frame. Withholding the ack instead would only make a runtime
        # redeliver it forever.
        logger.warning(
            "discarding result for op %s from runtime %s: no dispatch on record "
            "(forged frame, or a result for another server's database)",
            frame.op_id,
            runtime_id,
        )
        return True
    if record["runtime_id"] != runtime_id:
        # The journal says a different runtime was asked. A result for it from
        # this one cannot be trusted to describe our operation.
        logger.warning(
            "discarding result for op %s: dispatched to runtime %s, answered by %s",
            frame.op_id,
            record["runtime_id"],
            runtime_id,
        )
        return True

    command_type = record["command_type"]

    # A LAUNCH that FAILED may still have created a pane before failing, and the
    # runtime is the only party that can confirm either way. Acking such a result
    # unexamined was the hole: it contradicted this function's own contract and
    # left exactly the untracked terminal the contract forbids (Copilot review on
    # #802). Ask for a teardown and ack only once the runtime confirms the id is
    # gone or was never there.
    if frame.outcome != CommandOutcome.OK:
        if command_type == CommandType.LAUNCH.value and has_terminal:
            return await _confirm_failed_launch_left_nothing(frame, info["id"], runtime_id)
        # Any other failed operation created no terminal to orphan. Settle the
        # journal so a restart does not report it as an outcome never seen.
        _settle_quietly(frame.op_id)
        return True

    if command_type == CommandType.LAUNCH.value:
        if not has_terminal:
            _settle_quietly(frame.op_id)
            return True
        return _persist_reconciled_terminal(info, runtime_id, record, frame.op_id)

    # A successful non-LAUNCH result with no in-memory waiter: the caller that
    # would have applied it is gone. RUN_SCRIPT is the one that matters — its
    # background driver owned a durable run record, and dropping the result left
    # that journal RUNNING forever while the script had in fact finished (Copilot
    # review on #802). The outcome cannot be applied to a future nobody holds, so
    # record it as an outcome that arrived without an owner and settle it, rather
    # than acking into silence.
    if command_type == CommandType.RUN_SCRIPT.value:
        _reconcile_orphaned_script_run(frame, record)
    _settle_quietly(frame.op_id)
    return True


def _reconcile_orphaned_script_run(frame: CommandResultFrame, record: dict) -> None:
    """Settle the durable run a redelivered RUN_SCRIPT result belongs to.

    The in-memory driver that owned this run died with the previous server
    process, so there is no future to resolve and nothing applies the outcome.
    Acking and dropping it left the run's journal ``running`` forever even though
    the script had finished — the workflow was stuck on a step that was already
    done (Copilot review on #802).

    The result is recorded as FAILED rather than as the script's own exit status,
    deliberately. Its stdout/stderr and exit code went to a driver that no longer
    exists, so the step's real outcome cannot be reconstructed; claiming
    ``completed`` on the strength of the frame's outcome alone would assert a
    result nobody captured. FAILED with an explicit reason is the honest terminal
    state, and it unblocks the run so it can be retried.

    ``settle_run_state_if_running`` is conditional, so a run the engine already
    settled by some other path is left exactly as it is.
    """
    run_id = record.get("run_id")
    if not run_id:
        logger.warning(
            "orphaned RUN_SCRIPT result for op %s has no run in the journal; " "nothing to settle",
            frame.op_id,
        )
        return
    try:
        from datetime import datetime, timezone

        from cli_agent_orchestrator.models.workflow_runtime import RunState
        from cli_agent_orchestrator.services import workflow_journal

        settled = workflow_journal.settle_run_state_if_running(
            run_id,
            RunState.FAILED.value,
            datetime.now(timezone.utc).isoformat(),
        )
        if settled:
            logger.warning(
                "settled run %s as FAILED from a redelivered RUN_SCRIPT result (op %s): "
                "the script finished with outcome %s but the driver that owned the run "
                "is gone, so its output could not be applied",
                run_id,
                frame.op_id,
                frame.outcome.value,
            )
        else:
            logger.info(
                "run %s was already terminal when its RUN_SCRIPT result was redelivered "
                "(op %s); left as it is",
                run_id,
                frame.op_id,
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "could not settle run %s from redelivered RUN_SCRIPT result", run_id, exc_info=True
        )


def _settle_quietly(op_id: str) -> None:
    """Settle a journal entry, treating a failure as non-fatal.

    A journal that cannot be updated is a bookkeeping problem, not a reason to
    withhold an ack for work that is genuinely finished.
    """
    try:
        settle_dispatch(op_id)
    except Exception:  # noqa: BLE001
        logger.warning("could not settle dispatch journal for op %s", op_id, exc_info=True)


def _persist_reconciled_terminal(info: dict, runtime_id: str, record: dict, op_id: str) -> bool:
    """Write the central row for a terminal recovered from a redelivered LAUNCH."""
    terminal_id = info["id"]
    try:
        if get_terminal_metadata(terminal_id) is None:
            db_create_terminal(
                terminal_id,
                info["session_name"],
                info["name"],
                info["provider"],
                agent_profile=info.get("agent_profile"),
                allowed_tools=info.get("allowed_tools"),
                shell_command=info.get("shell_command"),
                # The bridge's LAUNCH result payload carries no `engine` key, so
                # `info` cannot supply it; the journal recorded what was asked for
                # at dispatch. Without this an engine-pinned terminal recovered
                # after a restart was persisted as engine=None, which is what reuse
                # validation and the input gate read (Copilot review on #802).
                engine=record.get("engine") or info.get("engine"),
                # The owner comes from the journal, which recorded it at dispatch
                # and never told the runtime. Previously this wrote no owner at
                # all, and an unowned row passes the revocation gate.
                owner=record.get("owner"),
                # server_metadata, not metadata: placement is server-owned and
                # caller-supplied copies of these keys are stripped at creation.
                server_metadata={"runtime_id": runtime_id},
            )
            logger.warning(
                "reconciled orphaned terminal %s from a redelivered LAUNCH result "
                "on runtime %s (owner %s, from the dispatch journal)",
                terminal_id,
                runtime_id,
                record.get("owner"),
            )
        runtime_registry.claim_terminal(terminal_id, runtime_id)
    except Exception:
        logger.exception(
            "failed to reconcile orphaned terminal %s; NOT acking so the runtime "
            "keeps its retained result and a later reconnect can retry",
            terminal_id,
        )
        return False
    _settle_quietly(op_id)
    return True


async def _confirm_failed_launch_left_nothing(
    frame: CommandResultFrame, terminal_id: str, runtime_id: str
) -> bool:
    """Tear down the pane a failed LAUNCH may have created, before acking it.

    The runtime is the only party that can say whether the id exists there, and
    its TEARDOWN answer distinguishes the two cases: ``absent`` or ``deleted``
    means nothing is running under that id and the result is safe to drop, while
    anything else means a session may still be alive and the result must stay
    retained for another attempt.
    """
    conn = runtime_registry.get_runtime(runtime_id)
    if conn is None:
        logger.warning(
            "failed LAUNCH %s left terminal %s unconfirmed and runtime %s is gone; "
            "keeping the result for a later reconnect",
            frame.op_id,
            terminal_id,
            runtime_id,
        )
        return False
    try:
        td = await conn.send_command(
            CommandType.TEARDOWN, {}, terminal_id=terminal_id, timeout=TEARDOWN_TIMEOUT
        )
    except Exception:
        logger.exception(
            "could not confirm teardown of terminal %s after failed LAUNCH %s; "
            "keeping the result retained",
            terminal_id,
            frame.op_id,
        )
        return False
    settled = td.outcome == CommandOutcome.OK and (
        td.payload.get("deleted") or td.payload.get("absent")
    )
    if not settled:
        logger.error(
            "terminal %s may still be running after failed LAUNCH %s (teardown said "
            "%s); NOT acking, so the result survives for another attempt",
            terminal_id,
            frame.op_id,
            td.payload,
        )
        return False
    # Nothing is running under that id. Drop any central row the failed launch
    # left behind, then let the result go.
    try:
        if get_terminal_metadata(terminal_id) is not None:
            db_delete_terminal(terminal_id)
        runtime_registry.unbind_terminal(terminal_id, deleted=True)
    except Exception:  # noqa: BLE001
        logger.warning(
            "teardown of %s confirmed but central cleanup failed", terminal_id, exc_info=True
        )
    _settle_quietly(frame.op_id)
    return True


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
    bound_this_hello: set = set()
    for stream_pos in hello.streams:
        # A hello names the terminals this runtime says it owns, but the shared
        # token makes that a claim, not a proof: refuse any it does not own and
        # drop the entry from resume, so the server never hands a claiming
        # runtime the stream position of a terminal it never launched.
        if not runtime_registry.claim_terminal(stream_pos.terminal_id, runtime_id):
            continue
        bound_this_hello.add(stream_pos.terminal_id)
        # ESTABLISH the advertised generation, rather than only echoing it back.
        # A restarted stream whose new end position happens to equal the old
        # watermark emits no replay frame, so nothing else would ever carry the
        # new generation to the registry and the server would stay on the old one
        # — then accept a delayed old-generation frame as current (Copilot review
        # on #802). record_position resets the watermark when the generation
        # advances, so resume below reads the NEW generation's position.
        runtime_registry.record_position(
            stream_pos.terminal_id,
            stream_pos.stream.value,
            0,
            generation=stream_pos.generation,
        )
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
    # The hello is this incarnation's complete statement of what it owns, so
    # anything still bound here and absent from it is gone: discard that cached
    # state BEFORE seeding, or a reconnect resurrects a dead terminal's last
    # PROCESSING/COMPLETED and answers it as current (review finding 2 on #802).
    for terminal_id in runtime_registry.reconcile_hello(
        runtime_id, [sp.terminal_id for sp in hello.streams]
    ):
        bus.publish(f"terminal.{terminal_id}.status", {"status": TerminalStatus.UNKNOWN.value})
    # Seed the status cache from the same snapshot. Status is pushed on change,
    # so without this a server that restarted while a terminal sat quiescent
    # would answer UNKNOWN until the agent next moved — indefinitely, for an
    # idle agent. Only terminals THIS hello actually bound are seeded: keying on
    # runtime_for_terminal() would also match terminals reconcile_hello kept
    # bound from a prior hello but that this one omitted, letting a stale
    # ``statuses`` entry resurrect their last status (Copilot review on #802).
    for terminal_id, reported in hello.statuses.items():
        if terminal_id in bound_this_hello:
            runtime_registry.set_status(terminal_id, reported, conn=conn)

    await ws.send_text(
        encode_frame(
            HelloFrame(protocol_version=PROTOCOL_VERSION, runtime_id="server", resume=resume)
        )
    )

    # Per-connection record of a heartbeat watermark this server has not caught
    # up to, keyed by (terminal, stream) -> (advertised end_pos, generation). A
    # reconnect starts the observation over, which is correct: the reconnect's
    # own replay is what resolves the discrepancy.
    behind: dict = {}

    try:
        while True:
            frame = decode_frame(await ws.receive_text())
            if runtime_registry.get_runtime(runtime_id) is not conn:
                # A reconnect under the same runtime id has replaced this
                # channel (``register`` already failed its waiters). A half-open
                # socket can keep delivering for a while, and acting on those
                # frames would let the superseded connection rebind terminals
                # and advance stream positions behind the live one — the
                # protocol's ``generation`` field is meant to fence exactly this,
                # but nothing has ever advanced it, so identity of the
                # registered connection is the check that actually holds today
                # (Copilot review on #802, finding 3).
                logger.warning(
                    "ignoring frames from a superseded channel for runtime %s", runtime_id
                )
                break
            conn.last_seen = time.time()
            if isinstance(frame, CommandResultFrame):
                matched = conn.resolve(frame)
                safe_to_ack = True
                if not matched:
                    # A result the runtime retained for an op sent before this
                    # server restarted. A lost LAUNCH result is the one that
                    # bites: the runtime kept a live terminal, but the restarted
                    # server never persisted or bound it, so acking-and-dropping
                    # would orphan a running agent nothing can route to or tear
                    # down (guojing1217 on #802). Reconcile it before acking.
                    safe_to_ack = await _reconcile_orphaned_result(frame, runtime_id)
                # Ack only once any reconciliation SUCCEEDED: the ack is what lets
                # the runtime drop its retained copy, and dropping it after a
                # failed reconcile would orphan a live agent for good. Worker
                # cleanup must not outrun result delivery either way.
                if safe_to_ack:
                    await ws.send_text(encode_frame(AckFrame(op_id=frame.op_id)))
            elif isinstance(frame, StreamFrame):
                raw = base64.b64decode(frame.data)
                # A terminal streaming through this channel is de facto executed
                # by this runtime — keep routing bound even if the hello snapshot
                # predated the terminal's creation. But a shared-token runtime
                # can just as easily emit a StreamFrame for a terminal it does
                # not own to republish its output and advance its position; a
                # refused claim drops the frame rather than rebinding.
                if not runtime_registry.claim_terminal(frame.terminal_id, runtime_id):
                    continue
                if frame.stream == StreamName.ATTACH:
                    # Interactive bytes go to the live attach client, never
                    # the bus; an empty frame is the runtime PTY's EOF.
                    runtime_registry.deliver_attach(frame.terminal_id, raw if raw else None)
                    continue
                # A frame from a SUPERSEDED generation belongs to a stream this
                # server has already moved past: drop it rather than splice its
                # bytes into the live stream's transcript (Copilot review on
                # #802). record_position then fences the other direction — a
                # higher generation resets the watermark instead of being read as
                # a rewind of the old stream.
                if runtime_registry.is_stale_generation(
                    frame.terminal_id, frame.stream.value, frame.generation
                ):
                    logger.warning(
                        "dropping stream frame for terminal %s from stale generation %s",
                        frame.terminal_id,
                        frame.generation,
                    )
                    continue
                # Republish onto the existing in-process bus with the exact
                # payload shape the local FIFO reader uses, so bus-contract
                # consumers (LogWriter, AG-UI, inbox) work unchanged.
                #
                # Loss is reported PER SUBSCRIBER, and the watermark always
                # advances. Three earlier shapes of this were wrong, each for the
                # same structural reason — the bus is a shared, bounded,
                # fire-and-forget fanout, so no single decision derived from an
                # aggregate is right for every subscriber:
                #
                #  * advancing silently: a dropped chunk was recorded as received
                #    and became unrecoverable;
                #  * holding the watermark back: the reconnect replays the range to
                #    EVERY subscriber, duplicating output for those that took it —
                #    and with two consecutive drops the second call moved the
                #    watermark past the first loss anyway;
                #  * broadcasting a gap marker: it tells subscribers that received
                #    the bytes they lost them, is published fire-and-forget, and can
                #    be dropped by the very queue that is full — so the one
                #    subscriber needing it is the least likely to get it.
                #
                # `deliver_with_loss_markers` owes the marker to the specific queue
                # that refused, and hands it over on the next put that queue
                # accepts. Nothing is replayed, nothing is duplicated, and the loss
                # is visible to exactly the consumer that suffered it (Copilot
                # reviews on #802).
                dropped = bus.deliver_with_loss_markers(
                    f"terminal.{frame.terminal_id}.output",
                    {"data": raw.decode("utf-8", errors="replace")},
                    lost={
                        "from_pos": frame.pos,
                        "to_pos": frame.pos + len(raw),
                        # Positions restart at 0 on a re-armed stream, so a pending
                        # range must not be widened across that boundary.
                        "generation": frame.generation,
                    },
                )
                runtime_registry.record_position(
                    frame.terminal_id,
                    frame.stream.value,
                    frame.pos + len(raw),
                    generation=frame.generation,
                )
                if dropped:
                    logger.warning(
                        "%s subscriber queue(s) dropped output for terminal %s "
                        "[%s, %s); each is owed a gap marker on its next accepted "
                        "event, and the watermark advances so nothing is replayed "
                        "to the subscribers that did receive it",
                        dropped,
                        frame.terminal_id,
                        frame.pos,
                        frame.pos + len(raw),
                    )
            elif isinstance(frame, GapFrame):
                # A GapFrame advances the resume watermark and republishes a loss,
                # so it needs the same ownership fence as the other inbound
                # terminal frames — otherwise a shared-token runtime could forge a
                # gap for a terminal it does not own and make the server discard or
                # report output that was never its (Copilot follow-up on #802).
                if not runtime_registry.claim_terminal(frame.terminal_id, runtime_id):
                    continue
                # Same generation fence as StreamFrame: a gap declared by a
                # superseded stream says nothing about the live one.
                if runtime_registry.is_stale_generation(
                    frame.terminal_id, frame.stream.value, frame.generation
                ):
                    logger.warning(
                        "dropping gap frame for terminal %s from stale generation %s",
                        frame.terminal_id,
                        frame.generation,
                    )
                    continue
                logger.warning(
                    "output gap for remote terminal %s [%s, %s)",
                    frame.terminal_id,
                    frame.from_pos,
                    frame.to_pos,
                )
                if frame.to_pos is not None:
                    # A BOUNDED gap is a definitive answer: those bytes are gone
                    # and no reconnect will ever produce them. Only StreamFrames
                    # advanced the watermark, so a gap that replayed no chunks
                    # alongside it (one evicted chunk larger than the whole
                    # window) left the resume position before ``to_pos`` — and
                    # every reconnect then asked for the same lost range and got
                    # the same gap, forever, never reaching live bytes. Consuming
                    # the gap is what closes that loop.
                    #
                    # ``to_pos is None`` is the runtime saying it cannot bound
                    # the loss (a lost generation). Advancing on that would skip
                    # past bytes that may yet arrive, so it is deliberately left
                    # to the generation path (Copilot review on #802).
                    runtime_registry.record_position(
                        frame.terminal_id,
                        frame.stream.value,
                        frame.to_pos,
                        generation=frame.generation,
                    )
                # Drop-aware, like the StreamFrame arm: the watermark has already
                # advanced over this range, so a marker lost to a full queue is a
                # range nobody will ever hear about. Converting only the
                # StreamFrame arm left exactly the reported defect here (Copilot
                # review on #802).
                bus.deliver_with_loss_markers(
                    f"terminal.{frame.terminal_id}.output",
                    {"data": "", "gap": {"from_pos": frame.from_pos, "to_pos": frame.to_pos}},
                    lost={
                        "from_pos": frame.from_pos,
                        "to_pos": (frame.to_pos if frame.to_pos is not None else frame.from_pos),
                        "generation": frame.generation,
                    },
                )
            elif isinstance(frame, EventFrame):
                if not runtime_registry.claim_terminal(frame.terminal_id, runtime_id):
                    continue
                if frame.type == EventType.STATUS and frame.status is not None:
                    runtime_registry.set_status(
                        frame.terminal_id,
                        frame.status,
                        conn=conn,
                        generation=frame.generation,
                    )
                    bus.publish(
                        f"terminal.{frame.terminal_id}.status", {"status": frame.status.value}
                    )
                    # A status change is the one reliable signal that a stream may
                    # have stopped producing. Hand over any loss marker still owed
                    # for this terminal's output now, while a consumer is still
                    # listening — otherwise a chunk dropped as the LAST output stays
                    # owed forever and its range is never reported.
                    outstanding = bus.flush_owed(f"terminal.{frame.terminal_id}.output")
                    if outstanding:
                        logger.warning(
                            "%s gap marker(s) for terminal %s are still undelivered "
                            "after a status change; their subscriber queues are full",
                            outstanding,
                            frame.terminal_id,
                        )
            elif isinstance(frame, HeartbeatFrame):
                for stream_pos in frame.streams:
                    if not runtime_registry.claim_terminal(stream_pos.terminal_id, runtime_id):
                        continue
                    _note_heartbeat_watermark(behind, stream_pos, runtime_id)
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

    # Optional on purpose (review finding 8 on #802). The profile store that
    # says which CLI an agent runs on lives in the RUNTIME — its image, its
    # `cao install`. A provider resolved from a client's or this server's own
    # profiles is an answer about the wrong machine, and sending it silently
    # overrode the runtime's ``provider:`` field. Omit it and the runtime
    # resolves from its own profile; send it only to express an explicit
    # override the caller actually asked for (``cao launch --provider``).
    provider: Optional[str] = None
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
        # 503, not 404: a disconnected runtime is a retryable availability
        # condition, the same one the terminal command paths return 503 for and
        # the remote-execution contract promises. 404 made a bridge rollout look
        # like a permanent not-found and denied callers consistent retry
        # handling (Copilot review on #802).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"runtime '{runtime_id}' is not connected",
        )
    # Journal the operation with its owner BEFORE dispatching it. Two things
    # depend on this record existing first: a result that arrives for an unknown
    # op_id is refused outright (a runtime cannot invent an operation), and a
    # LAUNCH redelivered after a restart can be reconciled with the owner it was
    # made for instead of with none. The owner is deliberately NOT in the payload
    # below — an identity handed to an executor is one it can re-present.
    op_id = uuid.uuid4().hex
    try:
        record_dispatch(
            op_id,
            CommandType.LAUNCH.value,
            runtime_id,
            owner=owner_id,
            # The runtime's result payload does not echo `engine`, so the journal
            # is the only place a reconcile after a restart can read it from.
            engine=body.engine,
        )
    except Exception:
        # A launch that cannot be journalled must not happen: its result would be
        # unrecognisable on redelivery, which is the orphan this journal exists to
        # prevent. Refuse before anything is running.
        logger.exception("could not journal LAUNCH dispatch for runtime %s", runtime_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="cannot record the dispatch journal; launch refused",
        )
    try:
        result = await conn.send_command(
            CommandType.LAUNCH,
            body.model_dump(exclude_none=True),
            timeout=LAUNCH_TIMEOUT,
            op_id=op_id,
        )
    except RuntimeNotDispatchedError as e:
        # PROVABLY nothing on the wire, so a retry cannot duplicate the launch:
        # 503, the retryable signal.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except RuntimeUnavailableError as e:
        # The base class means the frame's fate is UNKNOWN — the channel can close
        # after the bytes are written. Answering 503 invited a retry that starts a
        # SECOND agent while the first is already running, which is the same
        # duplicate-work hazard the timeout arm below exists to avoid (Copilot
        # review on #802). Report unknown instead.
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"launch on runtime '{runtime_id}' has an unknown outcome: {e}",
        )
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
    # The engine the caller pinned is forwarded in the LAUNCH payload and
    # honored by the bridge, so it must be recorded centrally too: reuse
    # validation and the KAS input gate read the persisted value, and without
    # it a remote assign/handoff that pinned an engine stored engine=None
    # (guojing1217 on #802). Prefer the runtime's echo, fall back to the
    # request.
    engine = info.get("engine") or body.engine
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
    try:
        db_create_terminal(
            info["id"],
            info["session_name"],
            info["name"],
            info["provider"],
            agent_profile=info.get("agent_profile"),
            allowed_tools=info.get("allowed_tools"),
            shell_command=info.get("shell_command"),
            caller_id=body.caller_id,
            engine=engine,
            working_directory=body.working_directory,
            # server_metadata, not metadata: placement is server-owned and
            # caller-supplied copies of these keys are stripped at creation.
            server_metadata={"runtime_id": runtime_id},
            owner=owner_id,
        )
    except Exception:
        # The provider is already running in the runtime, but there is no row and
        # no binding — nothing can route to it or tear it down, so it is a leaked
        # pod holding a live model session (guojing1217 on #802). Compensate with
        # a best-effort TEARDOWN on the same connection; both ids are in hand.
        # The launch failure is what the caller must see, so re-raise after.
        logger.exception(
            "persisting terminal %s failed after launch on %s; tearing it back down",
            info.get("id"),
            runtime_id,
        )
        try:
            td = await conn.send_command(
                CommandType.TEARDOWN, {}, timeout=TEARDOWN_TIMEOUT, terminal_id=info["id"]
            )
            # send_command does NOT raise on a runtime-side teardown FAILURE — it
            # returns a result frame with a non-OK outcome. Treating that as
            # success would report the leak as cleaned up when the agent is still
            # running (Copilot follow-up on #802), so check the outcome and the
            # deleted/absent confirmation the teardown path uses.
            cleaned = td.outcome == CommandOutcome.OK and (
                td.payload.get("deleted") or td.payload.get("absent")
            )
            if not cleaned:
                logger.error(
                    "compensating teardown of leaked terminal %s did not confirm cleanup: "
                    "outcome=%s payload=%s — the agent may still be running on %s",
                    info.get("id"),
                    td.outcome,
                    td.payload,
                    runtime_id,
                )
        except Exception:
            logger.exception("compensating teardown of leaked terminal %s failed", info.get("id"))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"launched terminal on '{runtime_id}' but failed to persist it; tore it down",
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
        engine=engine,
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
    except RuntimeNotDispatchedError as e:
        # Nothing was sent: safe to retry, so keep the retryable 503.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except RuntimeUnavailableError as e:
        # Frame may already have been delivered — unknown, not retryable.
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(f"remote operation on terminal '{terminal_id}' has an unknown outcome: {e}"),
        )
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
        # Only the relay that still owns the attach closes the runtime PTY.
        # A second client on the same terminal displaces this one; closing on
        # the way out would then tear down the PTY the replacement is using
        # (Copilot review on #802, finding 4).
        if runtime_registry.unbind_attach(terminal_id, sink):
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
        # deleted=True tombstones the id so a frame queued before this TEARDOWN
        # cannot rebind routing for a terminal that no longer exists.
        runtime_registry.unbind_terminal(terminal_id, deleted=True)
    return deleted or absent
