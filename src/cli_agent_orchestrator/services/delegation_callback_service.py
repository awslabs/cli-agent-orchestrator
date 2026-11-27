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
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Collection, Iterable, List, Mapping, Optional, Set

from cli_agent_orchestrator.backends.base import TerminalBackend
from cli_agent_orchestrator.backends.registry import peek_backend
from cli_agent_orchestrator.clients.database import (
    clear_reply_in_flight,
    close_delegation_edge,
    close_delegation_edge_with_message,
    close_edges_replied_by,
    close_edges_with_delivered_reply,
    close_open_edges_for_terminal,
    count_open_edges_by_caller,
    create_delegation_edge,
    create_inbox_message,
    find_open_edge,
    get_inbox_message,
    get_pending_messages,
    get_terminal_metadata,
    list_armed_worker_ids,
    list_open_edges_into,
    list_open_edges_out_of,
    list_open_message_edges,
    list_reply_in_flight_edges,
    list_unarmed_assign_edges,
    mark_reply_in_flight,
)
from cli_agent_orchestrator.constants import DELEGATION_PICKUP_TIMEOUT_SECONDS
from cli_agent_orchestrator.models.delegation import (
    DelegationEdge,
    EdgeReason,
    EdgeSource,
    EdgeState,
)
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus
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


def build_delivery_failed_message(
    worker_id: str,
    profile: Optional[str],
    reason: str,
    message_id: Optional[int] = None,
    *,
    uncertain: bool = False,
) -> str:
    """The notice for a caller whose message to its worker failed for good.

    ``uncertain`` says the failure came while sending, so some or all of the
    text may have reached the worker.
    """
    message = f"Your message {message_id}" if message_id is not None else "Your message"
    if uncertain:
        outcome = f"may not have been delivered: {reason}. Some or all of it may have arrived. "
    else:
        outcome = f"was not delivered: {reason}. "
    return (
        f"[CAO delivery failed] {message} to worker {worker_id} "
        f"(profile {profile or 'unknown'}) {outcome}"
        "Check the worker's terminal before you send it again."
    )


def build_message_not_delivered_message(
    receiver_id: str, message_id: int, reason: str, uncertain: bool, receiver_gone: bool = False
) -> str:
    """The notice for a sender whose queued message failed for good (design D6).

    ``receiver_gone`` is for a receiver whose row no longer exists: sending
    again could only fail, so the notice says so instead of asking for it.
    """
    if receiver_gone:
        arrived = " Some or all of it may have arrived before it ended." if uncertain else ""
        return (
            f"[CAO delivery failed] Your message {message_id} to terminal {receiver_id} "
            f"was not delivered: {reason}. Terminal {receiver_id} no longer exists.{arrived}"
        )
    if uncertain:
        return (
            f"[CAO delivery failed] Your message {message_id} to terminal {receiver_id} "
            f"may not have been delivered: {reason}. Some or all of it may have arrived. "
            "Check the receiver's terminal before you send it again."
        )
    return (
        f"[CAO delivery failed] Your message {message_id} to terminal {receiver_id} "
        f"was not delivered: {reason}. Nothing was typed into the terminal. "
        "Send it again when you are ready."
    )


def build_reply_lost_message(
    worker_id: str, reason: str, reply: Optional[str], *, uncertain: bool = False
) -> str:
    """The notice for a caller whose ended worker's reply failed for good (design D7).

    The worker's row is gone by now, so no profile is named. ``uncertain`` says
    the failure came while sending, so the reply may already be in the
    caller's terminal.
    """
    outcome = (
        f"the reply may not have been delivered ({reason}), and the worker has ended. "
        "Some or all of it may already be above. "
        if uncertain
        else f"the reply could not be delivered ({reason}), and the worker has ended. "
    )
    return (
        f"[CAO delivery failed] Worker {worker_id} replied, but {outcome}"
        "Its reply, from the stored message:\n"
        "---\n"
        f"{reply if reply else _UNREADABLE}"
    )


def build_task_not_delivered_message(worker_id: str, profile: Optional[str]) -> str:
    return (
        f"[CAO delivery failed] cao-server restarted before the task for worker {worker_id} "
        f"(profile {profile or 'unknown'}) was delivered. Check the worker's terminal; "
        "it may never have received the task."
    )


def _publishes_waiting(backend: TerminalBackend) -> bool:
    """True if ``backend`` overrides the no-op ``set_waiting_counts``.

    tmux and the herdr TUI keep the default, so for them the count queries
    that feed it are skipped.
    """
    return getattr(type(backend), "set_waiting_counts", None) is not (
        TerminalBackend.set_waiting_counts
    )


class _WaitingLabelPublisher:
    """Writes ``cao.waiting`` on one background thread, never on a request path.

    A write is an RPC to herdr-core and can take as long as the core takes to
    answer. Callers only name the terminals whose count may have changed
    (``request``); the thread reads each count from the database when it writes,
    so a request that arrives while a write is in flight is folded into the
    next one and the last write always reflects the latest database state.
    ``repair`` takes the same lock for its read and write, so the sweep cannot
    interleave with a refresh either.

    Thread Safety:
        All methods are thread-safe. ``request`` holds a lock only to add to a
        set; it never waits for a write.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._pending: Set[str] = set()
        self._busy = False
        self._stopped = False
        self._thread: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()
        self._failing = False  # guarded by _write_lock

    def request(self, caller_ids: Set[str]) -> None:
        """Queue ``caller_ids`` for a write. Returns at once."""
        with self._cond:
            if self._stopped:
                return
            self._pending.update(caller_ids)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="cao-waiting-labels", daemon=True
                )
                self._thread.start()
            self._cond.notify_all()

    def flush(self, timeout: float) -> bool:
        """Wait until nothing is queued or being written. False if ``timeout`` ran out."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._pending and not self._busy, timeout)

    def stop(self, timeout: float = 1.0) -> None:
        """Stop the thread; queued requests are dropped (the next sweep repairs them)."""
        with self._cond:
            self._stopped = True
            self._pending.clear()
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def repair(self) -> None:
        """Write every managed terminal's count from the database (``complete=True``)."""
        self._write(count_open_edges_by_caller, complete=True)

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._pending or self._stopped)
                if self._stopped:
                    return
                caller_ids = set(self._pending)
                self._pending.clear()
                self._busy = True
            try:
                self._write(
                    lambda: {c: len(list_open_edges_out_of(c)) for c in caller_ids},
                    complete=False,
                )
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def _write(self, read_counts: Callable[[], Mapping[str, int]], *, complete: bool) -> None:
        """Read the counts and hand them to the backend, under one lock. Never raises."""
        backend = peek_backend()
        if backend is None or not _publishes_waiting(backend):
            return
        with self._write_lock:
            try:
                backend.set_waiting_counts(read_counts(), complete=complete)
            except Exception as exc:  # noqa: BLE001 - a label must never break the service
                # One warning when the failures start; the sweep retries every 30 s.
                log = logger.debug if self._failing else logger.warning
                log("delegation: could not publish cao.waiting: %s", exc)
                self._failing = True
            else:
                self._failing = False


_publisher = _WaitingLabelPublisher()


def refresh_waiting_labels(caller_ids: Iterable[str]) -> None:
    """Publish how many terminals each of ``caller_ids`` waits on, as ``cao.waiting``.

    Call it after an edge opens or closes. It returns at once: the count is
    read from the database, and the label written through the backend, on a
    background thread (``_WaitingLabelPublisher``), because the write is an RPC
    that must never hold up the assign, send_message, delete or failed delivery
    that caused it. Only the herdr-app backend shows the label; for any other
    backend, or none, it does nothing. A failure is logged once per episode
    and never reaches the caller; the next ``sweep`` repairs a label that a
    failed or dropped write left behind.
    """
    ids = {caller_id for caller_id in caller_ids if caller_id}
    backend = peek_backend()
    if not ids or backend is None or not _publishes_waiting(backend):
        return
    _publisher.request(ids)


def flush_waiting_labels(timeout: float = 5.0) -> bool:
    """Wait for the queued ``cao.waiting`` writes to finish; False if ``timeout`` ran out.

    For tests and orderly shutdown; request paths never call it.
    """
    return _publisher.flush(timeout)


def _repair_waiting_labels() -> None:
    """Recompute every managed terminal's ``cao.waiting`` from the database."""
    _publisher.repair()


def on_send_message(sender_id: str, receiver_id: str, message_id: int, expect_reply: bool) -> None:
    """Apply the edge rules for one send_message. Best-effort.

    Rules, first match wins: a reply to a terminal that is waiting on the
    sender marks that edge's reply in flight, and the edge closes as replied
    once the reply is delivered; a message to a terminal this sender created opens
    an edge; ``expect_reply`` opens an edge for any other terminal. The caller
    whose edge opened or closed has its ``cao.waiting`` label refreshed; marking
    a reply changes no count, so only a reply already delivered refreshes it.
    """
    owed = [e for e in list_open_edges_into(sender_id) if e.caller_id == receiver_id]
    if owed:
        # The reply ends the wait once it is delivered, not when it is stored
        # (reply-delivery-failure D1): inbox_service closes these as replied.
        for edge in owed:
            mark_reply_in_flight(edge.id, message_id)
        # A delivery that ran before the mark existed could not settle it.
        # Only DELIVERED is settled here; a failed or missing reply is left to
        # the sweep, which also handles a sender that has ended.
        _settle_reply_mark(receiver_id, message_id, None, delivered_only=True)
        return

    if find_open_edge(sender_id, receiver_id) is not None:
        return

    metadata = get_terminal_metadata(receiver_id) or {}
    if metadata.get("caller_id") == sender_id or expect_reply:
        create_delegation_edge(
            sender_id, receiver_id, EdgeSource.SEND_MESSAGE, message_id=message_id
        )
        refresh_waiting_labels([sender_id])


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
    ``close_delegation_edge_with_message``). Everything sent here is a notice
    CAO wrote, so it is marked as one.
    """
    return close_delegation_edge_with_message(
        edge.id, state, sender_id, edge.caller_id, text, is_system_notice=True
    )


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
    # An edge whose reply is in flight is not forwarded: the worker's real
    # answer is on its way (reply-delivery-failure D2).
    inbound = list_open_edges_into(terminal_id, armed_only=True, exclude_reply_in_flight=True)
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
    refresh_waiting_labels(edge.caller_id for edge in forwarded)
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
    ``build_worker_ended_message``). An inbound edge with a reply in flight is
    left open with no notice: the reply's delivery outcome settles it (D8).
    """
    inbound = list_open_edges_into(terminal_id, exclude_reply_in_flight=True)
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
    closed_silently = close_open_edges_for_terminal(terminal_id, EdgeState.WORKER_ENDED)
    for edge in notified:
        _deliver(edge.caller_id, terminal_id, registry)
    # The callers' counts fall. A terminal that is kept (ERROR) closed its own
    # outbound edges, so its own count is 0; a deleted one takes its label along.
    own = [terminal_id] if may_be_running else []
    refresh_waiting_labels([*own, *(edge.caller_id for edge in [*notified, *closed_silently])])


def handle_delivery_failed(
    worker_id: str,
    message_ids: List[int],
    reason: str,
    registry: PluginRegistry | None = None,
    *,
    uncertain: bool = False,
) -> None:
    """Close the edges of messages whose delivery failed for good, and tell each caller.

    A FAILED message is never retried, so its edges would never be armed: left
    OPEN, they are never swept, hold the caller's own forward back as nested
    work, and stop a follow-up from opening a fresh edge. Each edge is closed
    as worker_ended in the same commit that queues the caller's
    ``[CAO delivery failed]`` notice, so the caller hears back, as it would
    for a worker that ended. The notice names the message that opened the
    edge and, when ``uncertain``, says its text may have arrived.
    """
    edges = list_open_message_edges(message_ids)
    if not edges:
        return
    profile = (get_terminal_metadata(worker_id) or {}).get("agent_profile")
    notified = [
        e
        for e in edges
        if _close_and_notify(
            e,
            EdgeState.WORKER_ENDED,
            worker_id,
            build_delivery_failed_message(
                worker_id, profile, reason, e.message_id, uncertain=uncertain
            ),
        )
    ]
    for edge in notified:
        _deliver(edge.caller_id, worker_id, registry)
    refresh_waiting_labels(edge.caller_id for edge in notified)


def handle_messages_failed(
    receiver_id: str,
    messages: List[InboxMessage],
    reason: str,
    *,
    uncertain: bool = False,
    reported_ids: Collection[int] = (),
    registry: PluginRegistry | None = None,
) -> None:
    """Settle edges and tell the right terminal about messages failed for good.

    ``reported_ids`` are messages whose failure the sender already has from its
    own send_message (design D5); nobody else is told about them, and an edge
    such a message opened is closed without a notice. For the rest: an edge a
    message opened is closed and its caller, the sender, told once
    (``handle_delivery_failed``); a reply a message carried is no longer in
    flight, so its caller waits on the worker again (D7), and is told with the
    stored reply only when the worker has ended; and any other sender gets
    ``[CAO delivery failed]`` (D6). A failed CAO notice never causes another
    notice, so two terminals cannot trade failure notices. A notice is told
    apart by the mark CAO sets on its row, not by its text: a peer message that
    starts with ``[CAO `` is still reported to its sender.

    Args:
        receiver_id: The terminal the messages were for.
        messages: The messages marked FAILED for good.
        reason: One line naming the failure.
        uncertain: True when some or all of the text may have been typed.
        reported_ids: Messages whose outcome went back to their sender.
        registry: Plugin registry for the notices' delivery.
    """
    reported = set(reported_ids)
    ids = [m.id for m in messages]
    unreported = [i for i in ids if i not in reported]
    told = {e.message_id for e in list_open_message_edges(unreported)}
    if unreported:
        handle_delivery_failed(receiver_id, unreported, reason, registry, uncertain=uncertain)
    closed_quietly = [
        edge
        for edge in list_open_message_edges([i for i in ids if i in reported])
        if close_delegation_edge(edge.id, EdgeState.WORKER_ENDED, exclude_reply_in_flight=True)
    ]
    refresh_waiting_labels(edge.caller_id for edge in closed_quietly)

    by_id = {m.id: m for m in messages}
    for edge in clear_reply_in_flight(ids):
        reply = by_id.get(edge.reply_message_id) if edge.reply_message_id is not None else None
        _tell_caller_if_replier_ended(
            edge, reason, reply.message if reply else None, registry, uncertain=uncertain
        )

    for message in messages:
        if message.id in reported or message.id in told or message.is_system_notice:
            continue
        if get_terminal_metadata(message.sender_id) is None:
            continue
        notice = build_message_not_delivered_message(
            receiver_id,
            message.id,
            reason,
            uncertain,
            receiver_gone=get_terminal_metadata(receiver_id) is None,
        )
        try:
            create_inbox_message(receiver_id, message.sender_id, notice, is_system_notice=True)
        except ValueError:
            continue  # the sender's row went away in between
        _deliver(message.sender_id, receiver_id, registry)


def _tell_caller_if_replier_ended(
    edge: DelegationEdge,
    reason: str,
    reply: Optional[str],
    registry: PluginRegistry | None,
    *,
    uncertain: bool = False,
) -> None:
    """For a reply mark just cleared: if its sender has ended, close the edge and tell the caller.

    A live worker can resend, so its caller waits on it again and is told
    nothing (design D7). An ended worker never will, so the caller gets the
    stored reply text instead of a wait that cannot end.
    """
    if get_terminal_metadata(edge.worker_id) is not None:
        return
    text = build_reply_lost_message(edge.worker_id, reason, reply, uncertain=uncertain)
    if _close_and_notify(edge, EdgeState.WORKER_ENDED, edge.worker_id, text):
        _deliver(edge.caller_id, edge.worker_id, registry)
        refresh_waiting_labels([edge.caller_id])


def _settle_reply_mark(
    caller_id: str,
    message_id: int,
    registry: PluginRegistry | None,
    *,
    delivered_only: bool = False,
) -> None:
    """Settle reply marks naming ``message_id`` once that message is final.

    DELIVERED closes the edges as replied; FAILED, or a message row that is
    gone, clears the mark (reply-delivery-failure F1). A PENDING reply is still
    in flight and is left alone. The caller's delivery lock is held, without
    waiting, while the status is read: a delivery marks its message DELIVERED
    before it types it, so the status is final only while no delivery to the
    caller runs. A delivery in flight settles the mark itself, or a later
    sweep does. ``delivered_only`` settles DELIVERED and leaves the rest.
    """
    with inbox_service.hold_delivery_if_idle(caller_id) as held:
        if not held:
            return
        # A delivered message from the worker already answered the caller, even
        # when the mark now names a later reply that is still pending or failed.
        if close_edges_with_delivered_reply(caller_id, message_id):
            refresh_waiting_labels([caller_id])
            return
        message = get_inbox_message(message_id)
        if message is not None and message.status == MessageStatus.PENDING:
            return
        if message is not None and message.status == MessageStatus.DELIVERED:
            if close_edges_replied_by([message_id]):
                refresh_waiting_labels([caller_id])
            return
        if delivered_only:
            return
        cleared = clear_reply_in_flight([message_id])
    reason = (message.last_error if message else None) or "the reply was not delivered"
    for edge in cleared:
        # The kind of failure is not recorded with the message, so assume the
        # text may have arrived rather than tell the caller it did not.
        _tell_caller_if_replier_ended(
            edge, reason, message.message if message else None, registry, uncertain=True
        )


def settle_stale_reply_marks(registry: PluginRegistry | None = None) -> None:
    """Settle reply marks that no delivery outcome will settle (sweep, F1).

    A mark outlives its message's outcome when the server restarted between
    the paste and the settlement, or when the settlement itself failed.
    """
    for edge in list_reply_in_flight_edges():
        if edge.reply_message_id is None:
            continue
        try:
            _settle_reply_mark(edge.caller_id, edge.reply_message_id, registry)
        except Exception as exc:  # noqa: BLE001 - one edge must not stop the sweep
            logger.debug("delegation: could not settle reply mark of edge %d: %s", edge.id, exc)


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
    told = [e for e in edges if _close_and_notify(e, EdgeState.WORKER_ENDED, worker_id, text)]
    refresh_waiting_labels(edge.caller_id for edge in told)
    return len(told)


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
    """Settle stale reply marks, evaluate every armed worker, then repair every ``cao.waiting`` label.

    Covers missed events, herdr, and restarts. Each step runs on its own: the
    label repair is what heals a missed update, so no other step's failure may
    skip it.
    """
    try:
        settle_stale_reply_marks(registry)
    except Exception as exc:  # noqa: BLE001 - the steps below still run
        logger.warning("delegation sweep could not settle reply marks: %s", exc)
    try:
        for worker_id in list_armed_worker_ids():
            try:
                evaluate_completion(worker_id, registry)
            except Exception as exc:  # noqa: BLE001 - one worker must not stop the sweep
                logger.debug("delegation sweep failed for %s: %s", worker_id, exc)
    except Exception as exc:  # noqa: BLE001 - the label repair below still runs
        logger.warning("delegation sweep could not list armed workers: %s", exc)
    try:
        _log_cycles(list_armed_worker_ids())
    except Exception as exc:  # noqa: BLE001 - the label repair below still runs
        logger.warning("delegation sweep could not check for cycles: %s", exc)
    _repair_waiting_labels()


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
