"""Server-side callback guarantee for assign and send_message delegations.

An edge records that a caller is owed a callback by a worker. Edges are opened
by assign (terminal create) and by send_message (inbox POST), armed once their
task has been delivered, and closed by a reply, an auto-forward, or the worker
ending. Evaluation runs on terminal status events and on a periodic sweep, so
it still applies on backends that do not publish status events.
"""

import asyncio
import logging
from typing import List, Optional

from cli_agent_orchestrator.clients.database import (
    close_delegation_edge,
    close_open_edges_for_terminal,
    create_delegation_edge,
    create_inbox_message,
    find_open_edge,
    get_pending_messages,
    get_terminal_metadata,
    list_armed_worker_ids,
    list_open_edges_into,
    list_open_edges_out_of,
)
from cli_agent_orchestrator.models.delegation import EdgeReason, EdgeSource, EdgeState
from cli_agent_orchestrator.models.terminal import TerminalStatus
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


def build_worker_ended_message(
    worker_id: str, profile: Optional[str], how: str, last_response: Optional[str]
) -> str:
    return (
        f"[CAO worker ended] Worker {worker_id} (profile {profile or 'unknown'}) {how} "
        "before sending you a result.\n"
        "Below is its last response if it could be read.\n"
        "---\n"
        f"{last_response if last_response else _UNREADABLE}"
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


def _enqueue(sender_id: str, receiver_id: str, text: str) -> None:
    try:
        create_inbox_message(sender_id=sender_id, receiver_id=receiver_id, message=text)
        inbox_service.deliver_pending(receiver_id)
    except Exception as exc:  # noqa: BLE001 - caller may be gone; edge is already closed
        logger.warning("delegation: could not notify %s from %s: %s", receiver_id, sender_id, exc)


def evaluate_completion(terminal_id: str) -> int:
    """Auto-forward ``terminal_id`` to its waiting callers if it is done.

    Forwards only when every condition holds: an armed open edge points into
    the terminal, the terminal has no open edge of its own toward another
    terminal (nested delegation), it has no PENDING inbox message, and its
    status is IDLE or COMPLETED. Each edge is claimed before it is forwarded,
    so concurrent evaluators never forward the same edge twice.

    Returns the number of edges forwarded.
    """
    inbound = list_open_edges_into(terminal_id, armed_only=True)
    if not inbound:
        return 0
    if list_open_edges_out_of(terminal_id):
        return 0
    if get_pending_messages(terminal_id, limit=1):
        return 0
    if status_monitor.get_status(terminal_id) not in _DONE_STATES:
        return 0

    profile = (get_terminal_metadata(terminal_id) or {}).get("agent_profile")
    last = _last_response(terminal_id)
    forwarded = 0
    for edge in inbound:
        if not close_delegation_edge(edge.id, EdgeState.AUTO_FORWARDED):
            continue  # another evaluator won the claim
        _enqueue(
            terminal_id,
            edge.caller_id,
            build_auto_forward_message(terminal_id, profile, edge.reason, last),
        )
        forwarded += 1
    if forwarded:
        logger.info("delegation: auto-forwarded %s to %d caller(s)", terminal_id, forwarded)
    return forwarded


def handle_worker_ended(terminal_id: str, how: str) -> None:
    """Close edges touching an ended worker and notify callers waiting on it."""
    closed = close_open_edges_for_terminal(terminal_id, EdgeState.WORKER_ENDED)
    if not closed:
        return
    profile = (get_terminal_metadata(terminal_id) or {}).get("agent_profile")
    last = _last_response(terminal_id)
    for edge in closed:
        _enqueue(
            terminal_id,
            edge.caller_id,
            build_worker_ended_message(terminal_id, profile, how, last),
        )


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


def sweep() -> None:
    """Evaluate every armed worker. Covers missed events, herdr, and restarts."""
    for worker_id in list_armed_worker_ids():
        try:
            evaluate_completion(worker_id)
        except Exception as exc:  # noqa: BLE001 - one worker must not stop the sweep
            logger.debug("delegation sweep failed for %s: %s", worker_id, exc)
    _log_cycles(list_armed_worker_ids())


async def run() -> None:
    """Consume terminal.*.status: evaluate on IDLE/COMPLETED, end edges on ERROR."""
    queue = bus.subscribe("terminal.*.status")
    logger.info("DelegationCallbackService started")
    while True:
        event = await queue.get()
        try:
            status_value = event["data"]["status"]
            terminal_id = terminal_id_from_topic(event["topic"])
            if status_value in _DONE_STATE_VALUES:
                await asyncio.to_thread(evaluate_completion, terminal_id)
            elif status_value == TerminalStatus.ERROR.value:
                await asyncio.to_thread(handle_worker_ended, terminal_id, "reached ERROR")
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the consumer
            logger.error("DelegationCallbackService error: %s", exc)
