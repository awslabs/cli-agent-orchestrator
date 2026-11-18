"""Delivers queued inbox messages when terminals become ready.

Consumer: terminal.{id}.status
"""

import asyncio
import functools
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from itertools import groupby
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from cli_agent_orchestrator.backends.base import InputNotSentError, TerminalNotFoundError
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    close_edges_replied_by,
    fail_orphaned_pending_messages,
    get_pending_messages,
    list_pending_receiver_ids_by_provider,
    list_pending_receiver_ids_older_than,
    mark_message_edges_delivered,
    record_delivery_failure,
    update_message_status,
)
from cli_agent_orchestrator.constants import (
    EAGER_INBOX_DELIVERY,
    INBOX_MAX_DELIVERY_ATTEMPTS,
    INBOX_RECONCILE_GRACE_SECONDS,
    INBOX_SUBMIT_CONFIRM_TIMEOUT,
    INBOX_SUBMIT_CONFIRMATION,
    INBOX_SUBMIT_MAX_RESUBMITS,
    INBOX_SWEEP_FANOUT,
)
from cli_agent_orchestrator.models.inbox import (
    DeliveryOutcome,
    InboxMessage,
    MessageStatus,
    OrchestrationType,
)
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import (
    TerminalInputBlockedError,
    TerminalInputBlockedReason,
    TerminalStatus,
)
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

# Poll interval for the submit-confirmation wait. Short because the common case
# is a healthy delivery that is already PROCESSING on the first or second read.
# A dropped delivery costs INBOX_SUBMIT_CONFIRM_TIMEOUT for the first wait plus
# one more per recovery actually sent, so up to
# INBOX_SUBMIT_CONFIRM_TIMEOUT * (1 + INBOX_SUBMIT_MAX_RESUBMITS) -- all of it
# on that one terminal's delivery thread, under that terminal's lock.
_SUBMIT_CONFIRM_POLL_INTERVAL = 0.25

# Statuses proving the receiver consumed this delivery (left its ready state).
# WAITING_USER_ANSWER counts: the message was read and the agent is now asking.
_SUBMIT_STARTED_STATUSES = {
    TerminalStatus.PROCESSING,
    TerminalStatus.COMPLETED,
    TerminalStatus.WAITING_USER_ANSWER,
}


def _short_reason(exc: Exception) -> str:
    """One line naming a delivery failure, for the caller's notice."""
    text = " ".join(str(exc).split())
    reason = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return reason if len(reason) <= 200 else reason[:197] + "..."


OutcomeCallback = Callable[[int, DeliveryOutcome, Optional[str]], None]


class DeliveryHandOff:
    """Decides who reports the failure of the inbox POST's own message (design D5).

    The POST waits for its message's outcome only up to a bound. While it
    waits, a failure of that message is the POST's to report: the message is
    failed at once, with no retry and no notice. Once the POST has given up
    (answered ``in_progress``), the sender is no longer listening, so a later
    failure is handled as a queued one. ``offer`` and ``abandon`` take the same
    lock, so exactly one of the two paths runs for a failure.

    Thread Safety:
        Safe from any thread: the POST calls ``abandon`` from the event loop
        while ``deliver_pending`` calls ``offer`` from a worker thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outcome: Optional[Tuple[DeliveryOutcome, Optional[str]]] = None
        self._abandoned = False

    def offer(self, outcome: DeliveryOutcome, error: Optional[str]) -> bool:
        """Hand ``outcome`` to the POST. True while the POST is still waiting.

        The first outcome offered is the one the POST reports; a later offer
        changes nothing. False once the POST has given up, so the caller
        handles the message as queued.
        """
        with self._lock:
            if self._abandoned:
                return False
            if self._outcome is None:
                self._outcome = (outcome, error)
            return True

    def abandon(self) -> Optional[Tuple[DeliveryOutcome, Optional[str]]]:
        """End the POST's wait: the outcome offered so far, or None.

        None means no outcome was offered, and every later ``offer`` returns
        False.
        """
        with self._lock:
            if self._outcome is None:
                self._abandoned = True
            return self._outcome


def _record_failure(message_id: int, reason: str) -> int:
    """Count a failed attempt; a bookkeeping failure ends the retries rather than looping."""
    try:
        return record_delivery_failure(message_id, reason)
    except Exception as exc:  # noqa: BLE001 - delivery status must still be set
        logger.warning(f"Could not record the failed attempt for message {message_id}: {exc}")
        return INBOX_MAX_DELIVERY_ATTEMPTS


class InboxService:
    """Delivers one pending message per terminal per IDLE cycle."""

    def __init__(self) -> None:
        # deliver_pending is read(PENDING) → status check → mark DELIVERED → send,
        # with no atomic claim at the DB layer, so two concurrent calls for the
        # SAME terminal can both read the same oldest row before either marks it
        # and deliver one task twice. Concurrent callers are real: the status-event
        # consumer and the immediate POST path both dispatch to worker threads, and
        # the OpenCode poller and the reconcile sweep add more. Serialize the whole
        # read→mark→send sequence per terminal; as a side effect this also keeps
        # two pastes from ever interleaving in one pane. The lock map is tiny
        # (one Lock per terminal id ever delivered to in this process) and is not
        # reaped — terminal ids are bounded by session lifecycle.
        self._delivery_locks_guard = threading.Lock()
        self._delivery_locks: Dict[str, threading.Lock] = {}
        # Created on first use by deliver_pending_in_background.
        self._background_pool_guard = threading.Lock()
        self._background_pool: Optional[ThreadPoolExecutor] = None

    def _delivery_lock(self, terminal_id: str) -> threading.Lock:
        with self._delivery_locks_guard:
            lock = self._delivery_locks.get(terminal_id)
            if lock is None:
                lock = threading.Lock()
                self._delivery_locks[terminal_id] = lock
            return lock

    async def run(self, registry: PluginRegistry | None = None) -> None:
        """Consume status events and deliver to each ready terminal on its own task.

        One task per terminal, never one shared await: a delivery now includes
        the submit confirmation (#78), which can hold that terminal's lock for
        INBOX_SUBMIT_CONFIRM_TIMEOUT * (1 + INBOX_SUBMIT_MAX_RESUBMITS) seconds
        when the receiver swallowed the Enter. Awaiting it inline here (as this
        loop did before) stalled delivery to EVERY other terminal for that long
        (PR #1004 review, P2-2). Spawning instead keeps the consumer reading;
        the per-terminal delivery lock still keeps deliveries to one terminal
        in order.

        Events for a terminal whose delivery is still in flight are coalesced
        into a single re-check that runs when that delivery finishes, so a
        burst of status transitions cannot queue a thread per event behind the
        lock -- and the re-check preserves the old semantics, where the later
        event's delivery ran after the earlier one and saw the pending rows it
        left behind.
        """
        queue = bus.subscribe("terminal.*.status")
        logger.info("InboxService started")

        in_flight: Dict[str, asyncio.Task] = {}
        recheck: Set[str] = set()

        def _finished(terminal_id: str, task: "asyncio.Task[None]") -> None:
            in_flight.pop(terminal_id, None)
            if not task.cancelled() and task.exception() is not None:
                logger.error(f"Error in InboxService: {task.exception()}")
            if terminal_id in recheck:
                recheck.discard(terminal_id)
                _spawn(terminal_id)

        def _spawn(terminal_id: str) -> None:
            # deliver_pending does blocking DB + tmux I/O (and now the
            # confirmation wait). Offload it to a worker thread so this consumer
            # keeps yielding to the event loop (StatusMonitor/LogWriter must not
            # be starved — see the threading note in
            # docs/event-driven-architecture.md). The registry is threaded
            # through so status-driven deliveries fire PostSendMessageEvent
            # hooks with the same attribution as the immediate and
            # OpenCode-poller paths.
            task = asyncio.create_task(
                asyncio.to_thread(self.deliver_pending, terminal_id, registry=registry)
            )
            in_flight[terminal_id] = task
            task.add_done_callback(functools.partial(_finished, terminal_id))

        try:
            while True:
                try:
                    event = await queue.get()
                    status_value = event["data"]["status"]
                    if status_value in (
                        TerminalStatus.IDLE.value,
                        TerminalStatus.COMPLETED.value,
                    ):
                        terminal_id = terminal_id_from_topic(event["topic"])
                        if terminal_id in in_flight:
                            recheck.add(terminal_id)
                        else:
                            _spawn(terminal_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Error in InboxService: {e}")
        finally:
            for task in in_flight.values():
                task.cancel()

    def deliver_pending(
        self,
        terminal_id: str,
        num_messages: int = 1,
        registry: PluginRegistry | None = None,
        after_dispatch: Optional[Callable[[], None]] = None,
        source: Optional[str] = None,
        on_outcome: Optional[OutcomeCallback] = None,
        report_to_sender: Optional[int] = None,
        hand_off: Optional[DeliveryHandOff] = None,
    ) -> int:
        """Deliver pending message(s) to a ready terminal. Use num_messages=0 for all.

        Status comes from the StatusMonitor (the event-driven source of truth).
        Delivery normally happens on IDLE/COMPLETED; providers that accept input
        mid-turn (``accepts_input_while_processing``) also receive messages while
        PROCESSING when ``EAGER_INBOX_DELIVERY`` is on (#251), and while
        WAITING_USER_ANSWER unless the provider's choice widget would eat the
        paste (``blocks_orchestrated_input_while_waiting_user_answer``, #896).
        Every delivery is tagged with a ``send_message`` orchestration type so
        ``terminal_service``'s own guards see machine-originated input (#896);
        when a plugin registry is supplied the originating sender is threaded
        too, so ``PostSendMessageEvent`` hooks fire with correct attribution.

        ``after_dispatch`` is called exactly once, as soon as the immediate
        dispatch decision is over: right after the paste reached the pane (BEFORE
        the submit confirmation, which can take a long time), or on return when
        nothing was delivered. It lets a caller that only needs to know the
        attempt was made -- the inbox POST -- answer without waiting out a
        confirmation. It is never an acknowledgement that the receiver accepted
        the message.

        ``on_outcome(message_id, outcome, error)`` is called at most once per
        message this call handles: DELIVERED when the paste reached the pane
        (before the submit confirmation), or FAILED, UNCERTAIN or RETRYING when
        it did not. A message left alone (receiver not ready, older messages
        ahead, an unresolved pane or a transient refusal that leaves it
        PENDING) gets no call. An exception from the callback is logged and
        never changes the delivery. ``report_to_sender`` names the message
        whose outcome the caller returns to its sender (the inbox POST): that
        message is never retried by the server (reply-delivery-failure D5).
        With a ``hand_off``, that holds only while the POST still waits: a
        failure the hand-off refuses is handled as a queued one.

        Safe to call from any thread: the whole read→mark→send sequence is
        serialized per terminal (see __init__ for why that is load-bearing).
        """
        notified = False

        def notify() -> None:
            nonlocal notified
            if notified or after_dispatch is None:
                return
            notified = True
            after_dispatch()

        try:
            with self._delivery_lock(terminal_id):
                return self._deliver_pending_locked(
                    terminal_id,
                    num_messages,
                    registry,
                    notify,
                    source=source,
                    on_outcome=on_outcome,
                    report_to_sender=report_to_sender,
                    hand_off=hand_off,
                )
        finally:
            notify()

    def _deliver_pending_locked(
        self,
        terminal_id: str,
        num_messages: int,
        registry: PluginRegistry | None,
        after_dispatch: Callable[[], None] = lambda: None,
        source: Optional[str] = None,
        on_outcome: Optional[OutcomeCallback] = None,
        report_to_sender: Optional[int] = None,
        hand_off: Optional[DeliveryHandOff] = None,
    ) -> int:
        """Return the number of messages actually handed to the pane.

        The count lets an event-driven caller (the herdr settle-retry) stop
        re-checking once a delivery lands, instead of running out its whole
        window. Zero covers both "nothing pending" and "pending but the
        readiness gate is still closed"; the caller only needs the positive
        signal.
        """
        reported: Set[int] = set()

        def report(message_id: int, outcome: DeliveryOutcome, error: Optional[str]) -> None:
            if on_outcome is None or message_id in reported:
                return
            reported.add(message_id)
            try:
                on_outcome(message_id, outcome, error)
            except Exception as exc:  # noqa: BLE001 - a caller's callback never fails a delivery
                logger.warning(f"Delivery outcome callback failed for message {message_id}: {exc}")

        limit = num_messages if num_messages > 0 else 100
        messages = get_pending_messages(terminal_id, limit=limit)
        if not messages:
            return 0

        status = status_monitor.get_status(terminal_id)
        eager = False
        delivered_count = 0
        if status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
            # Not ready on the normal path. Eager delivery (#251) lets providers
            # that accept input mid-turn receive messages while PROCESSING or
            # WAITING_USER_ANSWER; only in that case do we need the provider.
            eager_eligible = False
            if EAGER_INBOX_DELIVERY and status in (
                TerminalStatus.PROCESSING,
                TerminalStatus.WAITING_USER_ANSWER,
            ):
                provider = provider_manager.get_provider(terminal_id)
                eager_eligible = provider is not None and getattr(
                    provider, "accepts_input_while_processing", False
                )
                # ...except into an open dialog on a provider whose choice widget
                # EATS pasted text (#896). WAITING_USER_ANSWER on claude_code means
                # an AskUserQuestion/approval dialog is up: the Ink Select consumes
                # the paste as the answer and the trailing Enter submits whichever
                # option is highlighted. The message would be marked DELIVERED, the
                # agent would never see it, and a decision reserved for a human
                # would have been made by an inbox message. The provider already
                # declares this with the same property send_input's guard reads, so
                # gate on the capability rather than banning the state outright.
                if status == TerminalStatus.WAITING_USER_ANSWER and getattr(
                    provider, "blocks_orchestrated_input_while_waiting_user_answer", False
                ):
                    eager_eligible = False
            if not eager_eligible:
                return 0
            eager = True

        # Mark DELIVERED before sending (#164). send_input() types into the tmux
        # pane; that output flows back through the FIFO/StatusMonitor pipeline and
        # can re-emit an IDLE/COMPLETED status event, re-entering deliver_pending.
        # If the messages were still PENDING then, they would be delivered twice.
        # Marking them DELIVERED first closes that window; the except path resets
        # them to FAILED.
        for message in messages:
            update_message_status(message.id, MessageStatus.DELIVERED)

        # Event-inbox backends whose service delivers off the event loop
        # (herdr-app) run the submit confirmation too. They have no output
        # generation, so pickup is judged by a transition from the status read
        # just before each dispatch, as the first-task path does for them
        # (terminal_service._wait_for_post_dispatch_start).
        confirm = INBOX_SUBMIT_CONFIRMATION and not eager
        native_confirm = (
            confirm
            and get_backend().supports_event_inbox()
            and get_backend().event_inbox_confirms_submit() is True
        )

        # Deliver in contiguous runs of the same sender. With the default
        # num_messages=1 this is a single run; when draining all pending messages
        # (num_messages=0) a batch can span multiple senders, so each run is sent
        # separately to keep PostSendMessageEvent attribution correct — otherwise
        # every message would be attributed to messages[0].sender_id.
        for sender_id, group in groupby(messages, key=lambda m: m.sender_id):
            batch = list(group)
            combined = "\n".join(m.message for m in batch)
            # Set once the paste is in the pane: a later failure is never "nothing typed".
            dispatched = False
            try:
                pre_dispatch_status = (
                    status_monitor.get_status(terminal_id) if native_confirm else None
                )
                # Taken from dispatch_input's return value -- sampled AFTER the
                # monitor is armed and the rolling buffer cleared, before any key
                # reaches the pane (terminal_service.py:4008-4022). That is the
                # only correct boundary: anything sampled earlier (e.g. a plain
                # output_generation() call before send_input) can be exceeded by
                # unrelated output that lands in the window before send_input
                # does its provider work and clears the buffer, which would let a
                # swallowed Enter read as confirmed -- the exact failure this
                # gate exists to catch.
                if registry is None:
                    # Still tagged send_message (#896) so dispatch_input's
                    # WAITING_USER_ANSWER guard sees machine-originated input on
                    # this path too; only the plugin attribution differs (no
                    # registry means no PostSendMessageEvent, so sender_id has
                    # nothing to attribute and is left off).
                    boundary = terminal_service.dispatch_input(
                        terminal_id,
                        combined,
                        orchestration_type=OrchestrationType.SEND_MESSAGE,
                    )
                else:
                    boundary = terminal_service.dispatch_input(
                        terminal_id,
                        combined,
                        registry=registry,
                        sender_id=sender_id,
                        orchestration_type=OrchestrationType.SEND_MESSAGE,
                    )
                # Name the trigger when the caller supplied one: an event-driven
                # delivery and a reconciliation-sweep delivery are otherwise
                # indistinguishable here, which is exactly how a dead event path
                # (herdr 0.9.x pane.updated) went unnoticed.
                logger.info(
                    "Delivered %d message(s) to terminal %s%s",
                    len(batch),
                    terminal_id,
                    f" (trigger: {source})" if source else "",
                )
                dispatched = True
                delivered_count += len(batch)
                # The paste is in the pane: the immediate-delivery caller may
                # answer now. Everything below is confirmation and recovery.
                after_dispatch()
                for message in batch:
                    report(message.id, DeliveryOutcome.DELIVERED, None)
                # Delegation edges opened by this batch are armed only after the
                # submit confirmation has run. An edge armed on the paste alone
                # could be evaluated while the receiver's screen still holds
                # the previous answer, before it shows the new turn. They are armed even
                # when the confirmation gives up, because pickup can lag
                # delivery by more than that wait. The evaluator's own check
                # that the receiver started on its last input keeps an
                # unpicked delivery from forwarding the previous answer, and
                # its pickup timeout tells the caller if it never starts.
                if confirm:
                    if get_backend().supports_event_inbox() and not native_confirm:
                        # The herdr TUI backend pushes live-event delivery
                        # inline on its single asyncio event loop thread
                        # (herdr_inbox_service.py's _event_loop -> _deliver). The
                        # settle-retry path is offloaded via asyncio.to_thread,
                        # but this on-event path is not, so _confirm_submitted's
                        # blocking time.sleep() here would stall that whole
                        # session's event processing for up to
                        # INBOX_SUBMIT_CONFIRM_TIMEOUT * (1 + INBOX_SUBMIT_MAX_RESUBMITS)
                        # seconds. Skip confirmation there until the live-event
                        # path is driven off-loop too (tracked as a follow-up to
                        # the delivery-ready-signal work) -- the drop stays
                        # undetected on herdr, same as before this fix, instead
                        # of freezing the session.
                        logger.debug(
                            "Skipping inbox submit confirmation for terminal %s: "
                            "event-inbox backend delivery runs on the API event loop",
                            terminal_id,
                        )
                    else:
                        # Still holding the per-terminal delivery lock,
                        # deliberately: the pile-up in #78 ({MSG1}{MSG2} in one
                        # composer) is a second delivery landing while the first
                        # sits unsubmitted. The lock only DELAYS that second
                        # delivery until this confirmation ends; what prevents
                        # the pile-up is a recovery that submits the first paste
                        # (bare Enter), which needs the provider to read its
                        # composer -- see _confirm_submitted. Other terminals
                        # are not held: each delivery runs on its own thread.
                        # Eager delivery is excluded because its receiver was
                        # already PROCESSING -- "left the ready state" proves
                        # nothing there.
                        self._confirm_submitted(
                            terminal_id,
                            combined,
                            boundary,
                            registry=registry,
                            sender_id=sender_id,
                            native=native_confirm,
                            pre_dispatch_status=pre_dispatch_status,
                        )
                self._arm_message_edges(terminal_id, batch)
                self._settle_reply_edges(terminal_id, batch)
            except TerminalNotFoundError as e:
                # Pane not resolvable yet (e.g. a herdr pane that isn't mapped
                # for this window). Treat as transient: reset to PENDING so the
                # reconcile sweep retries rather than marking FAILED. These were
                # optimistically set to DELIVERED above. (#271 semantic.)
                for message in batch:
                    update_message_status(message.id, MessageStatus.PENDING)
                logger.warning(
                    f"Pane not resolvable for terminal {terminal_id}; leaving "
                    f"{len(batch)} message(s) pending for retry: {e}"
                )
            except TerminalInputBlockedError as e:
                # send_input's own guard refused. For a dialog this is transient
                # -- the terminal flipped to WAITING_USER_ANSWER between the
                # status gate above and the paste, and a human will answer it --
                # so reset to PENDING for the reconcile sweep rather than FAILED,
                # which would strand the message permanently (#896). The other
                # raise site (provider process gone, status ERROR) is terminal:
                # keep FAILED there, since no later sweep can ever deliver it.
                #
                # Branch on the exception's own reason, not a second status read:
                # on herdr, status_monitor.get_status() shells out to the herdr CLI
                # and returns UNKNOWN on any error, which would misclassify a
                # provably-transient dialog refusal as FAILED and strand it anyway.
                #
                # A Kiro terminal this server is still initializing has not passed
                # its identity check yet (#1074); that is transient too, and the
                # reconcile sweep delivers once the check has been recorded.
                if e.reason in (
                    TerminalInputBlockedReason.WAITING_USER_ANSWER,
                    TerminalInputBlockedReason.IDENTITY_PENDING,
                ):
                    for message in batch:
                        update_message_status(message.id, MessageStatus.PENDING)
                    logger.warning(
                        f"Terminal {terminal_id} cannot take input yet; leaving "
                        f"{len(batch)} message(s) pending for retry: {e}"
                    )
                else:
                    # Refused before typing, and no later attempt can succeed.
                    self._fail_batch(
                        terminal_id,
                        batch,
                        e,
                        not_sent=True,
                        retryable=False,
                        report_to_sender=report_to_sender,
                        hand_off=hand_off,
                        report=report,
                        registry=registry,
                        reason="the terminal refused input"
                        + (f" ({e.reason.value})" if e.reason else ""),
                    )
            except Exception as e:
                self._fail_batch(
                    terminal_id,
                    batch,
                    e,
                    not_sent=isinstance(e, InputNotSentError) and not dispatched,
                    retryable=True,
                    report_to_sender=report_to_sender,
                    hand_off=hand_off,
                    report=report,
                    registry=registry,
                )

        return delivered_count

    def deliver_pending_in_background(
        self, terminal_id: str, registry: PluginRegistry | None = None
    ) -> "Future[None]":
        """Queue ``deliver_pending`` for ``terminal_id`` on a shared pool and return at once.

        For a caller that has already stored the message and must not wait out
        the submit confirmation, which holds an idle receiver for
        INBOX_SUBMIT_CONFIRM_TIMEOUT per attempt when it does not start. This is
        the split the inbox POST endpoint makes: the message is stored, so the
        caller's answer is already true, and the delivery and its confirmation
        carry on by themselves. ``registry`` is passed through, so
        PostSendMessageEvent fires as it does for the POST. A failure is logged,
        never raised.

        The pool has INBOX_SWEEP_FANOUT workers, the bound the sweeps fan out
        over, so a burst (a session teardown with many waiting callers) cannot
        start a thread per message. Deliveries to one receiver still run one at
        a time under its lock, so a burst to one busy receiver can hold every
        worker and delay deliveries to others until it drains.

        Shutdown: the pool's workers are joined at interpreter exit, so exit
        waits for queued and running deliveries, each up to a full submit
        confirmation. A process killed between a message being marked
        DELIVERED and its paste leaves it DELIVERED but never typed; the inbox
        POST path has the same window.

        Returns the delivery's future, so a test can wait on it.

        Thread Safety:
            Safe from any thread.
        """

        def deliver() -> None:
            try:
                self.deliver_pending(terminal_id, registry=registry)
            except Exception as e:  # noqa: BLE001 - nobody waits on this future
                logger.warning(f"Background delivery to {terminal_id} failed: {e}")

        with self._background_pool_guard:
            if self._background_pool is None:
                self._background_pool = ThreadPoolExecutor(
                    max_workers=INBOX_SWEEP_FANOUT, thread_name_prefix="cao-deliver"
                )
            pool = self._background_pool
        return pool.submit(deliver)

    def _fail_batch(
        self,
        terminal_id: str,
        batch: List[InboxMessage],
        error: Exception,
        *,
        not_sent: bool,
        retryable: bool,
        report_to_sender: Optional[int],
        hand_off: Optional[DeliveryHandOff],
        report: OutcomeCallback,
        registry: PluginRegistry | None,
        reason: Optional[str] = None,
    ) -> None:
        """Retry, or fail for good and report, each message of a batch whose delivery failed.

        A queued message that failed before anything was typed goes back to
        PENDING until INBOX_MAX_DELIVERY_ATTEMPTS attempts have failed. The
        message whose outcome goes back to its sender (``report_to_sender``),
        a message that may have been typed, and a permanent refusal are failed
        at once. Every failed attempt is counted on its message.

        Args:
            terminal_id: The receiver.
            batch: The messages of the failed dispatch.
            error: What the dispatch raised.
            not_sent: True when nothing of the batch can have been typed.
            retryable: False for a refusal no later attempt can get past.
            report_to_sender: Id of the message the inbox POST reports on.
            hand_off: The POST's hand-off for that message; when it refuses
                the failure, the message is handled as queued.
            report: Outcome reporter of this ``deliver_pending`` call.
            registry: Plugin registry for the failure notices.
            reason: Text for the notices; derived from ``error`` when None.
        """
        reason = reason or _short_reason(error)
        final_outcome = DeliveryOutcome.FAILED if not_sent else DeliveryOutcome.UNCERTAIN
        final: List[InboxMessage] = []
        reported_ids: Set[int] = set()
        for message in batch:
            reported = message.id == report_to_sender and (
                hand_off is None or hand_off.offer(final_outcome, reason)
            )
            if reported:
                reported_ids.add(message.id)
            attempts = _record_failure(message.id, reason)
            if not_sent and retryable and not reported and attempts < INBOX_MAX_DELIVERY_ATTEMPTS:
                update_message_status(message.id, MessageStatus.PENDING)
                report(message.id, DeliveryOutcome.RETRYING, reason)
                logger.warning(
                    f"Message {message.id} to {terminal_id} not delivered "
                    f"(attempt {attempts} of {INBOX_MAX_DELIVERY_ATTEMPTS}), retrying: {reason}"
                )
                continue
            logger.error(f"Failed to deliver message {message.id} to {terminal_id}: {error}")
            update_message_status(message.id, MessageStatus.FAILED)
            report(message.id, final_outcome, reason)
            final.append(message)
        if final:
            self._report_failed_messages(
                terminal_id,
                final,
                reason,
                uncertain=not not_sent,
                reported_ids=reported_ids,
                registry=registry,
            )

    @staticmethod
    def _report_failed_messages(
        terminal_id: str,
        messages: List[InboxMessage],
        reason: str,
        *,
        uncertain: bool,
        reported_ids: Set[int],
        registry: PluginRegistry | None,
    ) -> None:
        """Settle the edges of messages failed for good and tell whoever must know.

        See ``delegation_callback_service.handle_messages_failed``. Imported
        here because that module imports this one. Best-effort: a failure must
        not change the messages' FAILED status.
        """
        try:
            from cli_agent_orchestrator.services import delegation_callback_service

            delegation_callback_service.handle_messages_failed(
                terminal_id,
                messages,
                reason,
                uncertain=uncertain,
                reported_ids=reported_ids,
                registry=registry,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not report failed messages for {terminal_id}: {exc}")

    @staticmethod
    def _settle_reply_edges(terminal_id: str, batch: List[InboxMessage]) -> None:
        """Close as replied the edges whose reply this batch delivered (design D1).

        Bookkeeping, like arming: a failure here must not mark a delivered
        batch FAILED.
        """
        try:
            close_edges_replied_by([m.id for m in batch])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not settle reply edges for {terminal_id}: {exc}")

    @staticmethod
    def _arm_message_edges(terminal_id: str, batch: List[InboxMessage]) -> None:
        """Arm the send_message delegation edges opened by ``batch``.

        Arming is bookkeeping. A failure here must not reach the delivery's
        exception handlers, which would mark a delivered batch as FAILED.
        """
        try:
            mark_message_edges_delivered([m.id for m in batch])
        except Exception as arm_exc:  # noqa: BLE001
            logger.warning(f"Could not arm delegation edges for {terminal_id}: {arm_exc}")

    @contextmanager
    def hold_delivery_if_idle(self, terminal_id: str) -> Iterator[bool]:
        """Hold the terminal's delivery lock if no delivery holds it; yield whether held.

        A delivery marks its messages DELIVERED before it types them, and the
        receiver's status stays at its latched IDLE or COMPLETED until the new
        turn shows. A reader that must not mistake that window for a finished
        turn (the delegation callback) holds this while it decides. It never
        waits: a delivery in flight yields False, and the caller retries on a
        later status event or sweep.

        Thread Safety:
            Safe from any thread. Must not be held while delivering to the same
            terminal: the lock is not reentrant.
        """
        lock = self._delivery_lock(terminal_id)
        held = lock.acquire(blocking=False)
        try:
            yield held
        finally:
            if held:
                lock.release()

    def _wait_for_pickup(
        self,
        terminal_id: str,
        boundary: int,
        *,
        native: bool = False,
        pre_dispatch_status: TerminalStatus | None = None,
    ) -> bool:
        """Wait for a started status that is EVIDENCE OF THIS DELIVERY.

        Mirrors ``terminal_service._wait_for_post_dispatch_start`` (the
        deferred-init path's confirmation) in synchronous form, and for the same
        reason: a status VALUE cannot say when it was earned. A COMPLETED latched
        from the receiver's previous turn satisfies "not ready" instantly, so the
        causal pair from ``status_observation`` is required -- the generation the
        status was EARNED at must exceed the delivery boundary.

        The herdr TUI backend never gets here: it pushes delivery inline on
        its own asyncio event loop thread, so ``_deliver_pending_locked`` skips
        confirmation for it rather than block that loop (see the caller).

        ``native`` (herdr-app, which delivers off the loop): status is derived
        on demand and has no output generation, so ``boundary`` cannot be
        used. The rule is the first-task path's for event-inbox backends
        (``terminal_service._wait_for_post_dispatch_start``): a started status
        counts if it is PROCESSING or differs from ``pre_dispatch_status``, the
        reading taken just before the dispatch. Native activity after the send
        (herdr-core reported ``working``, ``status_monitor.started_on_last_input``)
        also counts: herdr-core's terminal.send itself waits up to 5 s for the
        agent to start, so a short turn can be over before the wait begins.
        """
        deadline = time.monotonic() + INBOX_SUBMIT_CONFIRM_TIMEOUT
        while True:
            observation = status_monitor.status_observation(terminal_id)
            if native:
                if (
                    observation.status in _SUBMIT_STARTED_STATUSES
                    and (
                        observation.status == TerminalStatus.PROCESSING
                        or observation.status != pre_dispatch_status
                    )
                ) or status_monitor.started_on_last_input(terminal_id) is True:
                    return True
            elif (
                observation.status in _SUBMIT_STARTED_STATUSES
                and observation.output_generation is not None
                and observation.output_generation > boundary
            ):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_SUBMIT_CONFIRM_POLL_INTERVAL)

    def _confirm_submitted(
        self,
        terminal_id: str,
        message: str,
        boundary: int,
        *,
        registry: PluginRegistry | None,
        sender_id: str | None,
        native: bool = False,
        pre_dispatch_status: TerminalStatus | None = None,
    ) -> bool:
        """Confirm the receiver began processing this delivery; re-submit if not.

        Reuses ``terminal_service.redeliver_dropped_message_with_boundary`` --
        the same decision helper the deferred-init (#479/#496) and synchronous
        step (#562) paths use -- so the three delivery paths share one notion of
        what a dropped send looks like and how to recover from it. The recovery
        is bare Enter when the text is still visible in the receiver's composer
        (only the Enter was swallowed), a full re-send when the provider read
        its composer and the text is NOT there and the provider is
        probe-capable, and nothing at all otherwise.

        Both gates are on on purpose. ``full_resend_requires_probe``: without a
        direct status probe there is no way to tell "paste dropped" from "the
        agent already read it and the prompt scrolled away", and re-pasting an
        inbox message into a working agent delivers the same instruction twice.
        ``full_resend_requires_composer_evidence``: a full re-send pastes into
        the composer without clearing it, so unless the provider can show the
        composer does not still hold the first paste, the re-send would join the
        two copies and submit both (PR #1004 review, P2-3). A provider with
        neither ``extract_current_composer`` nor a probe therefore gets no
        recovery at all, only the log line; which providers those are is
        enumerated in ``TestInboxRecoveryCoverageByProvider``
        (test/services/test_inbox_service.py). ``screen_may_show_previous_turn``:
        an inbox receiver usually still shows its last turn's completion
        marker, so a probe that cannot tell turns apart must not read that as
        pickup.

        Returns True when the delivery was confirmed (or the receiver was found
        already working), False when it stayed unconfirmed. The message is left
        DELIVERED either way: it HAS been typed into the pane, so reverting it to
        PENDING would let the reconcile sweep paste it a second time.
        """

        def picked_up(since: int) -> bool:
            # ``native``: judged against the pre-dispatch reading every time, as
            # the first-task path keeps one reading across its resubmits.
            return self._wait_for_pickup(
                terminal_id, since, native=native, pre_dispatch_status=pre_dispatch_status
            )

        if picked_up(boundary):
            return True

        for attempt in range(1, INBOX_SUBMIT_MAX_RESUBMITS + 1):
            logger.warning(
                "Inbox delivery to %s shows no pickup %ss after send — "
                "attempting recovery (attempt %d)",
                terminal_id,
                INBOX_SUBMIT_CONFIRM_TIMEOUT,
                attempt,
            )
            try:
                already_started, new_boundary = (
                    terminal_service.redeliver_dropped_message_with_boundary(
                        terminal_id,
                        message,
                        attempt,
                        full_resend_requires_probe=True,
                        full_resend_requires_composer_evidence=True,
                        screen_may_show_previous_turn=True,
                        registry=registry,
                        sender_id=sender_id,
                        orchestration_type=OrchestrationType.SEND_MESSAGE,
                    )
                )
            except Exception:
                # A failed recovery is not a failed delivery: the message was
                # typed into the pane and is already DELIVERED. Log and stop.
                logger.warning(
                    "Inbox redelivery to %s failed (attempt %d)",
                    terminal_id,
                    attempt,
                    exc_info=True,
                )
                return False
            if already_started:
                # The helper's direct probe found the receiver running under a
                # lagging cached status (#496): delivery is confirmed.
                return True
            if new_boundary is None:
                # The helper deliberately sent nothing (not probe-capable, or
                # execution evidence was ambiguous). Waiting again would only
                # burn another timeout against the same unchanged state.
                return False
            # The recency bar advances per attempt: evidence has to be earned
            # after THIS re-submission, not after the original send.
            boundary = new_boundary
            if picked_up(boundary):
                return True

        logger.error(
            "Inbox delivery to %s is unconfirmed after %d recovery attempt(s); "
            "the message may be sitting unsubmitted in the agent's prompt",
            terminal_id,
            INBOX_SUBMIT_MAX_RESUBMITS,
        )
        return False

    def _deliver_to_each(
        self,
        terminal_ids: Iterable[str],
        registry: PluginRegistry | None,
        failure_label: str,
        source: Optional[str] = None,
    ) -> None:
        """Run ``deliver_pending`` for every receiver, in parallel, failures isolated.

        The sweeps used to walk receivers one after another. With the submit
        confirmation inside each delivery, one receiver that swallowed its Enter
        held the whole sweep for up to a full confirmation cycle per such
        receiver (PR #1004 review, P2-2). Fan out instead; the per-terminal lock
        keeps this from racing the status-event and POST paths.
        """
        ids = list(terminal_ids)
        if not ids:
            return
        with ThreadPoolExecutor(max_workers=min(len(ids), INBOX_SWEEP_FANOUT)) as pool:
            futures = {
                pool.submit(
                    self.deliver_pending, terminal_id, registry=registry, source=source
                ): terminal_id
                for terminal_id in ids
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.debug(f"{failure_label} failed for {futures[future]}: {e}")

    def poll_opencode_pending_messages(self, registry: PluginRegistry | None = None) -> None:
        """Poll OpenCode terminals for pending inbox messages.

        OpenCode-specific wakeup path for providers whose pipe-pane logs do not
        change after the TUI settles, so the FIFO-driven StatusMonitor may not
        emit an IDLE/COMPLETED transition to trigger delivery on its own.
        """
        self._deliver_to_each(
            list_pending_receiver_ids_by_provider(ProviderType.OPENCODE_CLI.value),
            registry,
            "OpenCode inbox poll",
            source="opencode-poll",
        )

    def reconcile_orphaned_messages(self, registry: PluginRegistry | None = None) -> None:
        """Re-attempt delivery for messages stuck in PENDING past the grace window.

        Provider-agnostic safety net for issue #131: when a receiving terminal is
        already idle, the immediate (on POST) delivery path may miss on a stale
        status, and an idle terminal produces no new output so the event-driven
        StatusMonitor never emits an IDLE/COMPLETED event to wake delivery —
        leaving the message orphaned. This sweep finds any such message and routes
        it back through the normal delivery gate (``deliver_pending``).

        Only messages older than ``INBOX_RECONCILE_GRACE_SECONDS`` are considered,
        so the sweep never competes with the fast paths for freshly queued
        messages — it only adopts ones they have already missed.

        Messages whose receiver terminal row is gone are not retried — but they
        are no longer left claiming PENDING either (#708): they are failed and
        logged, so an operator can tell an undeliverable message apart from one
        that is about to be delivered.
        """
        try:
            orphaned = fail_orphaned_pending_messages(INBOX_RECONCILE_GRACE_SECONDS)
            if orphaned:
                logger.warning(
                    "Failed %d pending inbox message(s) whose receiver terminal no longer "
                    "exists: %s",
                    len(orphaned),
                    ", ".join(orphaned),
                )
        except Exception as e:
            logger.error(f"Failed to sweep orphaned inbox messages: {e}")

        self._deliver_to_each(
            list_pending_receiver_ids_older_than(INBOX_RECONCILE_GRACE_SECONDS),
            registry,
            "Inbox reconciliation",
            source="reconcile-sweep",
        )


inbox_service = InboxService()
