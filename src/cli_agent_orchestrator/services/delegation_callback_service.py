"""Server-side callback guarantee for assign and send_message delegations.

An edge records that a caller is owed a callback by a worker. Edges are opened
by assign (terminal create) and by send_message (inbox POST), armed once their
task has been delivered, and closed by a reply, an auto-forward, or the worker
ending. Evaluation runs on terminal status events and on a periodic sweep, so
it still applies on backends that do not publish status events.
"""

import asyncio
import functools
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional, Set

from cli_agent_orchestrator.clients.database import (
    close_delegation_edge,
    close_delegation_edge_with_message,
    close_open_edges_for_terminal,
    create_delegation_edge,
    find_open_edge,
    get_pending_messages,
    get_terminal_metadata,
    list_armed_worker_ids,
    list_open_edges_into,
    list_open_edges_out_of,
    list_open_message_edges,
    list_unarmed_assign_edges,
)
from cli_agent_orchestrator.constants import DELEGATION_PICKUP_TIMEOUT_SECONDS
from cli_agent_orchestrator.models.delegation import (
    DelegationEdge,
    EdgeReason,
    EdgeSource,
    EdgeState,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.inbox_service import inbox_service
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.terminal_service import OutputMode
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

_REASON_TEXT = {
    EdgeReason.NO_CALLBACK: "did not call send_message",
    EdgeReason.NO_SEND_MESSAGE_TOOL: (
        "has no send_message tool (cao-mcp-server is not in its profile)"
    ),
}
_UNREADABLE = "(last response could not be read)"
_DONE_STATES = (  # subset-ok: only IDLE/COMPLETED end a turn; ERROR is handled apart
    TerminalStatus.IDLE,
    TerminalStatus.COMPLETED,
)
_DONE_STATE_VALUES = frozenset(s.value for s in _DONE_STATES)


def build_auto_forward_message(
    worker_id: str, profile: Optional[str], reason: EdgeReason, last_response: Optional[str]
) -> str:
    return (
        f"[CAO auto-forward] Worker {worker_id} (profile {profile or 'unknown'}) "
        "went idle without calling send_message.\n"
        f"Reason: {_REASON_TEXT[reason]}\n"
        "Below is its last response, read from the terminal. It may be partial "
        "or not the final result.\n"
        f'The worker is still running. To continue: send_message(receiver_id="{worker_id}", ...)\n'
        f'When done with it: delete_terminal("{worker_id}")\n'
        "---\n"
        f"{last_response if last_response else _UNREADABLE}"
    )


def build_pickup_timeout_message(
    worker_id: str, profile: Optional[str], waited_seconds: float, last_response: Optional[str]
) -> str:
    return (
        f"[CAO auto-forward] Worker {worker_id} (profile {profile or 'unknown'}) "
        "showed no sign of work on the last message delivered to it.\n"
        f"Reason: no work was seen {int(waited_seconds)}s after delivery. The message may not "
        "have been picked up (its paste or Enter can be lost), or the worker may have "
        "answered too fast for its status to show it.\n"
        "Below is its last response, read from the terminal. It can be from an earlier turn.\n"
        "The worker is still running. Check its terminal before you send the message again: "
        f'send_message(receiver_id="{worker_id}", ...)\n'
        f'When done with it: delete_terminal("{worker_id}")\n'
        "---\n"
        f"{last_response if last_response else _UNREADABLE}"
    )


def build_worker_ended_message(
    worker_id: str,
    profile: Optional[str],
    how: str,
    last_response: Optional[str],
    *,
    may_be_running: bool = False,
) -> str:
    """The notice for a caller whose worker ended, or errored, before it replied.

    ``may_be_running`` is for ERROR: the terminal is kept so it can be
    inspected, and the worker may still be running, so the notice says so and
    tells the caller how to close it.
    """
    still_running = (
        "The worker may still be running; its terminal is kept for inspection, and a "
        "later send_message from it still reaches you.\n"
        f'When done with it: delete_terminal("{worker_id}")\n'
        if may_be_running
        else ""
    )
    return (
        f"[CAO worker ended] Worker {worker_id} (profile {profile or 'unknown'}) {how} "
        "before sending you a result.\n"
        f"{still_running}"
        "Below is its last response if it could be read.\n"
        "---\n"
        f"{last_response if last_response else _UNREADABLE}"
    )


def build_delivery_failed_message(worker_id: str, profile: Optional[str], reason: str) -> str:
    return (
        f"[CAO delivery failed] Your message to worker {worker_id} "
        f"(profile {profile or 'unknown'}) was not delivered: {reason}. "
        "Check the worker's terminal before you send it again."
    )


def build_task_not_delivered_message(worker_id: str, profile: Optional[str]) -> str:
    return (
        f"[CAO delivery failed] cao-server restarted before the task for worker {worker_id} "
        f"(profile {profile or 'unknown'}) was delivered. Check the worker's terminal; "
        "it may never have received the task."
    )


def on_send_message(sender_id: str, receiver_id: str, message_id: int, expect_reply: bool) -> None:
    """Apply the edge rules for one send_message. Best-effort.

    Rules, first match wins: a reply to a terminal that is waiting on the
    sender closes that edge; a message to a terminal this sender created opens
    an edge; ``expect_reply`` opens an edge for any other terminal.
    """
    owed = [e for e in list_open_edges_into(sender_id) if e.caller_id == receiver_id]
    if owed:
        for edge in owed:
            close_delegation_edge(edge.id, EdgeState.REPLIED)
        return

    if find_open_edge(sender_id, receiver_id) is not None:
        return

    metadata = get_terminal_metadata(receiver_id) or {}
    if metadata.get("caller_id") == sender_id or expect_reply:
        create_delegation_edge(
            sender_id, receiver_id, EdgeSource.SEND_MESSAGE, message_id=message_id
        )


def _last_response(terminal_id: str) -> Optional[str]:
    try:
        return terminal_service.get_output(terminal_id, OutputMode.LAST)
    except Exception as exc:  # noqa: BLE001 - forwarding must not depend on extraction
        logger.warning("delegation: could not read last response of %s: %s", terminal_id, exc)
        return None


def _close_and_notify(edge: DelegationEdge, state: EdgeState, sender_id: str, text: str) -> bool:
    """Claim ``edge`` and queue ``text`` for its caller in one commit. True if claimed.

    The conditional close is the claim: concurrent evaluators can list the
    same edge, and only the one that closes it may send. The message is
    queued in the same commit as the close (see
    ``close_delegation_edge_with_message``).
    """
    return close_delegation_edge_with_message(edge.id, state, sender_id, edge.caller_id, text)


def _deliver(receiver_id: str, sender_id: str, registry: PluginRegistry | None) -> None:
    """Start delivering a queued forward or notice to ``receiver_id``; do not wait for it.

    The message is already PENDING (queued in the commit that closed its edge),
    so nothing here has to wait for the receiver to pick it up. Waiting would
    hold a ``DELETE /terminals/{id}`` for up to a full submit confirmation per
    idle caller, and in the status-event consumer would delay every other
    caller's forward behind one receiver that does not start. ``registry`` is
    passed through so PostSendMessageEvent fires for it, as for any inbox POST.
    """
    try:
        inbox_service.deliver_pending_in_background(receiver_id, registry=registry)
    except Exception as exc:  # noqa: BLE001 - caller may be gone; edge is already closed
        logger.warning("delegation: could not notify %s from %s: %s", receiver_id, sender_id, exc)


def _monotonic() -> float:
    return time.monotonic()


def _seconds_since_last_input(terminal_id: str) -> Optional[float]:
    sent_at = status_monitor.last_input_at(terminal_id)
    return None if sent_at is None else _monotonic() - sent_at


# Edge ids already warned about by _warn_if_waiting_on_undelivered, so a stuck
# edge is logged once, not on every 30 s sweep.
_warned_undelivered: Set[int] = set()


def _warn_if_waiting_on_undelivered(terminal_id: str, outbound: List[DelegationEdge]) -> None:
    """Log an outbound edge that has waited past the pickup timeout without delivery.

    Such an edge holds ``terminal_id``'s own forward back as nested work. Its
    message may still be queued for a worker that is busy, which is normal and
    can take long, so the edge is not ignored; but an edge whose delivery can
    never happen would hold the forward for good, and this makes it visible.
    """
    now = datetime.now(timezone.utc)
    for edge in outbound:
        if edge.delivered_at is not None or edge.id in _warned_undelivered:
            continue
        created = edge.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if (now - created).total_seconds() < DELEGATION_PICKUP_TIMEOUT_SECONDS:
            continue
        _warned_undelivered.add(edge.id)
        logger.warning(
            "delegation: %s is not forwarded while it waits on %s, whose task has not been "
            "delivered %.0fs after the edge opened (edge %d)",
            terminal_id,
            edge.worker_id,
            (now - created).total_seconds(),
            edge.id,
        )


def evaluate_completion(terminal_id: str, registry: PluginRegistry | None = None) -> int:
    """Auto-forward ``terminal_id`` to its waiting callers if it is done.

    Forwards only when every condition holds: an armed open edge points into
    the terminal, the terminal has no open edge of its own toward another
    terminal (nested delegation), no delivery to it is in flight, it has no
    PENDING inbox message, its status is IDLE or COMPLETED, and it has shown
    work on the last input it was sent. The last two conditions together
    mean the turn of that input has finished; the status alone stays latched
    at the previous turn's IDLE or COMPLETED until the new turn shows, and the
    screen in that window still holds the previous answer. A terminal that
    stays idle with no sign of work for ``DELEGATION_PICKUP_TIMEOUT_SECONDS``
    after its last input is forwarded anyway, with a header that says the
    input may not have been picked up.

    The delivery lock is held, without waiting, while the conditions are
    checked and the edges claimed: a delivery marks its message DELIVERED
    before it types it, and the lock is what keeps that window out of the
    check. Each edge is claimed before it is forwarded, so concurrent
    evaluators never forward the same edge twice.

    Returns the number of edges forwarded.

    Thread Safety:
        Safe to call concurrently for the same terminal from the status-event
        consumer and the sweep.
    """
    inbound = list_open_edges_into(terminal_id, armed_only=True)
    if not inbound:
        return 0
    with inbox_service.hold_delivery_if_idle(terminal_id) as held:
        if not held:
            return 0  # a delivery is in flight; a later event or sweep re-checks
        outbound = list_open_edges_out_of(terminal_id)
        if outbound:
            _warn_if_waiting_on_undelivered(terminal_id, outbound)
            return 0
        if get_pending_messages(terminal_id, limit=1):
            return 0
        if status_monitor.get_status(terminal_id) not in _DONE_STATES:
            return 0
        # Not started on its last input: pickup may still be on its way, so
        # wait, but only for so long. A lost paste or swallowed Enter never
        # shows work, and the caller must hear about it rather than nothing.
        started = status_monitor.started_on_last_input(terminal_id)
        waited = None if started else _seconds_since_last_input(terminal_id)
        if not started and (waited is None or waited < DELEGATION_PICKUP_TIMEOUT_SECONDS):
            return 0

        profile = (get_terminal_metadata(terminal_id) or {}).get("agent_profile")
        last = _last_response(terminal_id)
        if waited is not None:
            logger.warning(
                "delegation: %s shows no work %.0fs after its last input; forwarding anyway",
                terminal_id,
                waited,
            )

        def text(edge: DelegationEdge) -> str:
            if waited is None:
                return build_auto_forward_message(terminal_id, profile, edge.reason, last)
            return build_pickup_timeout_message(terminal_id, profile, waited, last)

        forwarded = [
            edge
            for edge in inbound
            if _close_and_notify(edge, EdgeState.AUTO_FORWARDED, terminal_id, text(edge))
        ]
    # Started after the worker's lock is released, and not waited for: the
    # delivery takes the caller's lock and can wait out a submit confirmation.
    for edge in forwarded:
        _deliver(edge.caller_id, terminal_id, registry)
    if forwarded:
        logger.info("delegation: auto-forwarded %s to %d caller(s)", terminal_id, len(forwarded))
    return len(forwarded)


def handle_worker_ended(
    terminal_id: str,
    how: str,
    registry: PluginRegistry | None = None,
    *,
    may_be_running: bool = False,
) -> None:
    """Close edges touching an ended worker and notify callers waiting on it.

    ``may_be_running`` is set for ERROR, whose terminal is kept (see
    ``build_worker_ended_message``).
    """
    inbound = list_open_edges_into(terminal_id)
    notified: List[DelegationEdge] = []
    if inbound:
        profile = (get_terminal_metadata(terminal_id) or {}).get("agent_profile")
        last = _last_response(terminal_id)
        text = build_worker_ended_message(
            terminal_id, profile, how, last, may_be_running=may_be_running
        )
        notified = [
            e for e in inbound if _close_and_notify(e, EdgeState.WORKER_ENDED, terminal_id, text)
        ]
    # Outbound edges, and any inbound edge opened since the listing, close
    # without a notice: their replies have no live receiver.
    close_open_edges_for_terminal(terminal_id, EdgeState.WORKER_ENDED)
    for edge in notified:
        _deliver(edge.caller_id, terminal_id, registry)


def handle_delivery_failed(
    worker_id: str,
    message_ids: List[int],
    reason: str,
    registry: PluginRegistry | None = None,
) -> None:
    """Close the edges of messages whose delivery failed for good, and tell each caller.

    A FAILED message is never retried, so its edges would never be armed: left
    OPEN, they are never swept, hold the caller's own forward back as nested
    work, and stop a follow-up from opening a fresh edge. Each edge is closed
    as worker_ended in the same commit that queues the caller's
    ``[CAO delivery failed]`` notice, so the caller hears back, as it would
    for a worker that ended.
    """
    edges = list_open_message_edges(message_ids)
    if not edges:
        return
    profile = (get_terminal_metadata(worker_id) or {}).get("agent_profile")
    text = build_delivery_failed_message(worker_id, profile, reason)
    notified = [e for e in edges if _close_and_notify(e, EdgeState.WORKER_ENDED, worker_id, text)]
    for edge in notified:
        _deliver(edge.caller_id, worker_id, registry)


def handle_task_never_delivered(worker_id: str) -> int:
    """Close the unarmed assign edges of a worker whose task can no longer be delivered.

    For the restart recovery: the task was accepted but cao-server restarted
    before it was delivered, so its edges would never be armed. Each caller is
    sent a ``[CAO delivery failed]`` notice, queued in the commit that closes
    its edge. The notice is not delivered here: this runs at startup, before
    the plugin registry and the status consumers exist, so the inbox
    reconciliation sweep delivers it, with the registry, once it is due.

    Returns the number of callers told.
    """
    edges = list_unarmed_assign_edges(worker_id)
    if not edges:
        return 0
    profile = (get_terminal_metadata(worker_id) or {}).get("agent_profile")
    text = build_task_not_delivered_message(worker_id, profile)
    return sum(1 for e in edges if _close_and_notify(e, EdgeState.WORKER_ENDED, worker_id, text))


def _log_cycles(worker_ids: List[str]) -> None:
    """Warn about idle terminals waiting on each other. Sends no messages."""
    idle = {w for w in worker_ids if status_monitor.get_status(w) in _DONE_STATES}
    for start in idle:
        seen, node = [start], start
        while True:
            nxt = [e.worker_id for e in list_open_edges_out_of(node) if e.worker_id in idle]
            if not nxt:
                break
            node = nxt[0]
            if node == start:
                # Log each cycle once, from its smallest member.
                if start == min(seen):
                    logger.warning("delegation cycle among idle terminals: %s", " -> ".join(seen))
                break
            if node in seen:
                break
            seen.append(node)


def sweep(registry: PluginRegistry | None = None) -> None:
    """Evaluate every armed worker. Covers missed events, herdr, and restarts."""
    for worker_id in list_armed_worker_ids():
        try:
            evaluate_completion(worker_id, registry)
        except Exception as exc:  # noqa: BLE001 - one worker must not stop the sweep
            logger.debug("delegation sweep failed for %s: %s", worker_id, exc)
    _log_cycles(list_armed_worker_ids())


async def run(registry: PluginRegistry | None = None) -> None:
    """Consume terminal.*.status: evaluate on IDLE/COMPLETED, end edges on ERROR."""
    queue = bus.subscribe("terminal.*.status")
    logger.info("DelegationCallbackService started")
    while True:
        event = await queue.get()
        try:
            status_value = event["data"]["status"]
            terminal_id = terminal_id_from_topic(event["topic"])
            if status_value in _DONE_STATE_VALUES:
                await asyncio.to_thread(evaluate_completion, terminal_id, registry)
            elif status_value == TerminalStatus.ERROR.value:
                await asyncio.to_thread(
                    functools.partial(
                        handle_worker_ended,
                        terminal_id,
                        "reached ERROR",
                        registry,
                        may_be_running=True,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the consumer
            logger.error("DelegationCallbackService error: %s", exc)
