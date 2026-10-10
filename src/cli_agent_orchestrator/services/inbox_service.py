"""Delivers queued inbox messages when terminals become ready.

Consumer: terminal.{id}.status
"""

import asyncio
import logging
import threading
from itertools import groupby
from typing import Dict, List, Optional

from cli_agent_orchestrator.clients.database import (
    get_pending_messages,
    list_pending_receiver_ids_by_provider,
    list_pending_receiver_ids_older_than,
    update_message_status,
)
from cli_agent_orchestrator.constants import (
    EAGER_INBOX_DELIVERY,
    INBOX_RECONCILE_GRACE_SECONDS,
)
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

# A queued message that cannot be delivered is retried indefinitely; every
# multiple of this many cumulative failed attempts escalates the log to a LOUD
# error so a genuinely-unreachable terminal is observable instead of silently
# spinning forever. This is pure observability -- a failed delivery is NEVER
# moved to a non-retriable state (harness-control#921): a coordination message
# leaves the inbox only on successful delivery or when its receiver terminal is
# deleted (which already drops it out of `list_pending_receiver_ids_older_than`'s
# join), never because a paste-buffer into a stale/renamed tmux window returned
# a non-zero exit.
_LOUD_ERROR_EVERY_N_ATTEMPTS = 10

# In-cycle delivery attempts before falling back to the reconcile sweep. The
# second attempt re-resolves the target implicitly: send_input() re-reads
# terminal metadata fresh on every call, so a cached/stale window suffix is
# never reused on the retry. Deliberately no sleep between attempts -- the
# immediate (on-POST) delivery path calls deliver_pending() inline rather than
# on a worker thread in some callers, so a blocking sleep here could stall it;
# the reconcile sweep (INBOX_RECONCILE_GRACE_SECONDS later) already provides
# the time-spaced retry for anything a same-cycle re-resolve cannot recover.
_IN_CYCLE_DELIVERY_ATTEMPTS = 2


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

        # message_id -> cumulative failed delivery attempts across reconcile
        # cycles (harness-control#921). Best-effort and in-memory: a process
        # restart clears it, which simply re-arms the loud-error counter -- the
        # message itself stays PENDING in the database regardless, so nothing is
        # lost by the reset. Pruned on successful delivery so healthy traffic
        # never accumulates entries. An entry for a message whose terminal was
        # later deleted lingers until the next process restart (a bounded, tiny
        # leak). No separate lock is taken: mutation of a given key only ever
        # happens while holding that terminal's `_delivery_lock`, and distinct
        # keys (different message ids) are never mutated by the same call, so
        # there is no read-modify-write race to guard against beyond what the
        # per-terminal lock already serializes.
        self._delivery_attempts: Dict[int, int] = {}

    def _delivery_lock(self, terminal_id: str) -> threading.Lock:
        with self._delivery_locks_guard:
            lock = self._delivery_locks.get(terminal_id)
            if lock is None:
                lock = threading.Lock()
                self._delivery_locks[terminal_id] = lock
            return lock

    async def run(self, registry: PluginRegistry | None = None) -> None:
        queue = bus.subscribe("terminal.*.status")
        logger.info("InboxService started")

        while True:
            try:
                event = await queue.get()
                status_value = event["data"]["status"]
                if status_value in (TerminalStatus.IDLE.value, TerminalStatus.COMPLETED.value):
                    terminal_id = terminal_id_from_topic(event["topic"])
                    # deliver_pending does blocking DB + tmux I/O. Offload it to a
                    # worker thread so this consumer keeps yielding to the event loop
                    # (StatusMonitor/LogWriter must not be starved — see the threading
                    # note in docs/event-driven-architecture.md). The registry is
                    # threaded through so status-driven deliveries fire
                    # PostSendMessageEvent hooks with the same attribution as the
                    # immediate and OpenCode-poller paths.
                    await asyncio.to_thread(self.deliver_pending, terminal_id, registry=registry)
            except Exception as e:
                logger.error(f"Error in InboxService: {e}")

    def deliver_pending(
        self,
        terminal_id: str,
        num_messages: int = 1,
        registry: PluginRegistry | None = None,
    ) -> None:
        """Deliver pending message(s) to a ready terminal. Use num_messages=0 for all.

        Status comes from the StatusMonitor (the event-driven source of truth).
        Delivery normally happens on IDLE/COMPLETED; providers that accept input
        mid-turn (``accepts_input_while_processing``) also receive messages while
        PROCESSING/WAITING_USER_ANSWER when ``EAGER_INBOX_DELIVERY`` is on (#251).
        When a plugin registry is supplied, the originating sender and a
        ``send_message`` orchestration type are threaded to ``terminal_service``
        so ``PostSendMessageEvent`` hooks fire with correct attribution.

        Safe to call from any thread: the whole read→mark→send sequence is
        serialized per terminal (see __init__ for why that is load-bearing).
        """
        with self._delivery_lock(terminal_id):
            self._deliver_pending_locked(terminal_id, num_messages, registry)

    def _deliver_pending_locked(
        self,
        terminal_id: str,
        num_messages: int,
        registry: PluginRegistry | None,
    ) -> None:
        limit = num_messages if num_messages > 0 else 100
        messages = get_pending_messages(terminal_id, limit=limit)
        if not messages:
            return

        status = status_monitor.get_status(terminal_id)
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
            if not eager_eligible:
                return

        # Mark DELIVERED before sending (#164). send_input() types into the tmux
        # pane; that output flows back through the FIFO/StatusMonitor pipeline and
        # can re-emit an IDLE/COMPLETED status event, re-entering deliver_pending.
        # If the messages were still PENDING then, they would be delivered twice.
        # Marking them DELIVERED first closes that window; a failed send resets
        # them to PENDING (never FAILED) so they are retried, not lost
        # (harness-control#921).
        for message in messages:
            update_message_status(message.id, MessageStatus.DELIVERED)

        # Deliver in contiguous runs of the same sender. With the default
        # num_messages=1 this is a single run; when draining all pending messages
        # (num_messages=0) a batch can span multiple senders, so each run is sent
        # separately to keep PostSendMessageEvent attribution correct — otherwise
        # every message would be attributed to messages[0].sender_id.
        for sender_id, group in groupby(messages, key=lambda m: m.sender_id):
            batch = list(group)
            combined = "\n".join(m.message for m in batch)
            error = self._deliver_batch(terminal_id, combined, sender_id, registry)
            if error is None:
                # Delivered. Clear the retry counters so a later, unrelated
                # failure for a reused message id starts fresh.
                for message in batch:
                    self._delivery_attempts.pop(message.id, None)
                logger.info(f"Delivered {len(batch)} message(s) to terminal {terminal_id}")
            else:
                self._requeue_for_retry(terminal_id, batch, error)

    def _deliver_batch(
        self,
        terminal_id: str,
        combined: str,
        sender_id: str,
        registry: PluginRegistry | None,
    ) -> Optional[Exception]:
        """Type a batch into the terminal, with a bounded in-cycle retry that
        re-resolves the target each try. Returns ``None`` on success, or the last
        exception if every attempt failed (the caller re-queues; a message is
        never dropped here).

        Every failure kind funnels into the retry on purpose and is treated as
        transient/retriable: a stale or renamed tmux window (paste-buffer exit
        != 0 -> ``CalledProcessError``), a pane not yet mapped
        (``TerminalNotFoundError``), a provider briefly in ERROR
        (``TerminalInputBlockedError`` -- may recover once the provider settles),
        or a terminal transiently absent mid-reissue (``ValueError``). None of
        these justify losing a coordination message; the reconcile sweep keeps
        retrying until the terminal is reachable again or is deleted
        (harness-control#921).
        """
        last_error: Optional[Exception] = None
        for _attempt in range(_IN_CYCLE_DELIVERY_ATTEMPTS):
            try:
                if registry is None:
                    terminal_service.send_input(terminal_id, combined)
                else:
                    terminal_service.send_input(
                        terminal_id,
                        combined,
                        registry=registry,
                        sender_id=sender_id,
                        orchestration_type=OrchestrationType.SEND_MESSAGE,
                    )
                return None
            except Exception as e:  # noqa: BLE001 -- re-queued by the caller, never dropped
                # The next iteration re-resolves the target: send_input re-reads
                # terminal metadata fresh, so a cached/stale window is not reused.
                last_error = e
        return last_error

    def _requeue_for_retry(
        self, terminal_id: str, batch: List[InboxMessage], error: Exception
    ) -> None:
        """Leave an undeliverable batch PENDING so the reconcile sweep retries it.

        The messages were optimistically flipped to DELIVERED before the send;
        reset them to PENDING (never FAILED) so ``get_pending_messages`` and the
        reconcile sweep pick them up again — a coordination message is never
        dropped on a failed delivery (harness-control#921). Escalate to a LOUD
        error every ``_LOUD_ERROR_EVERY_N_ATTEMPTS`` cumulative failures so a
        genuinely unreachable terminal is observable rather than silently
        spinning; the message stays retriable regardless.
        """
        for message in batch:
            attempts = self._delivery_attempts.get(message.id, 0) + 1
            self._delivery_attempts[message.id] = attempts
            update_message_status(message.id, MessageStatus.PENDING)
            if attempts % _LOUD_ERROR_EVERY_N_ATTEMPTS == 0:
                logger.error(
                    f"Inbox message {message.id} to terminal {terminal_id} STILL "
                    f"undelivered after {attempts} attempts; kept PENDING for retry "
                    "(a coordination message is never dropped on a failed delivery). "
                    f"Terminal may have a stale tmux window or be unreachable: {error}"
                )
            else:
                logger.warning(
                    f"Delivery to terminal {terminal_id} failed (attempt {attempts}); "
                    f"leaving message {message.id} PENDING for retry: {error}"
                )

    def poll_opencode_pending_messages(self, registry: PluginRegistry | None = None) -> None:
        """Poll OpenCode terminals for pending inbox messages.

        OpenCode-specific wakeup path for providers whose pipe-pane logs do not
        change after the TUI settles, so the FIFO-driven StatusMonitor may not
        emit an IDLE/COMPLETED transition to trigger delivery on its own.
        """
        for terminal_id in list_pending_receiver_ids_by_provider(ProviderType.OPENCODE_CLI.value):
            try:
                self.deliver_pending(terminal_id, registry=registry)
            except Exception as e:
                logger.debug(f"OpenCode inbox poll failed for {terminal_id}: {e}")

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
        """
        for terminal_id in list_pending_receiver_ids_older_than(INBOX_RECONCILE_GRACE_SECONDS):
            try:
                self.deliver_pending(terminal_id, registry=registry)
            except Exception as e:
                logger.debug(f"Inbox reconciliation failed for {terminal_id}: {e}")


inbox_service = InboxService()
