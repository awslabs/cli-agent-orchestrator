"""Delivers queued inbox messages when terminals become ready.

Consumer: terminal.{id}.status
"""

import asyncio
import logging
import threading
import time
from contextlib import contextmanager
from itertools import groupby
from typing import Dict, Iterator, Tuple

from cli_agent_orchestrator.backends.base import TerminalNotFoundError
from cli_agent_orchestrator.clients.database import (
    claim_pending_messages,
    get_pending_messages,
    list_pending_receiver_ids_by_provider,
    list_pending_receiver_ids_older_than,
    update_message_status,
)
from cli_agent_orchestrator.constants import (
    EAGER_INBOX_DELIVERY,
    INBOX_DISPATCH_COALESCE_WINDOW_S,
    INBOX_RECONCILE_GRACE_SECONDS,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

# terminal_id -> (lock, refcount). Same refcounted-per-key pattern as
# session_lock.py's session_lifecycle_lock, kept separate rather than shared:
# this guards a different critical section (message delivery, not session
# create/teardown) and the two have no reason to block each other.
_delivery_registry_guard = threading.Lock()
_delivery_locks: Dict[str, Tuple[threading.Lock, int]] = {}

# terminal_id -> monotonic time of the last dispatch not yet confirmed by a
# real status transition. The lock above serializes deliver_pending calls but
# does not stop a queued caller from proceeding once it is its turn:
# status_monitor.get_status() still returns the cached IDLE/COMPLETED from
# before the first send, because notify_input_sent() only arms the next
# PROCESSING detection, it does not flip the cached status itself, and the
# real detection needs actual terminal output to run. A second caller that
# only checks status would see the same stale ready value and dispatch its
# own message into a terminal that has not started working on the first one
# yet (reviewer-reproduced on #709: two IDLE checks and two sends in one
# cycle). This marker closes that window: it is set right after a successful
# send and normally cleared the instant InboxService.run() observes the next
# status event for that terminal. INBOX_DISPATCH_COALESCE_WINDOW_S is only the
# fallback for a terminal that stops producing status transitions entirely
# after the dispatch, so this can never wedge deliver_pending shut against the
# reconcile sweep the way an un-expiring flag would.
_dispatch_active_guard = threading.Lock()
_dispatch_active: Dict[str, float] = {}


def _clear_dispatch_active(terminal_id: str) -> None:
    """Drop the busy marker: the real status pipeline has moved past the
    cached ready value that authorized the last dispatch."""
    with _dispatch_active_guard:
        _dispatch_active.pop(terminal_id, None)


def _is_dispatch_active(terminal_id: str) -> bool:
    """True while a dispatch for this terminal is still within its coalescing
    window and no later status event has cleared it. Expires stale entries in
    place so a terminal that never produces another status transition does
    not stay wedged shut forever."""
    with _dispatch_active_guard:
        dispatched_at = _dispatch_active.get(terminal_id)
        if dispatched_at is None:
            return False
        if time.monotonic() - dispatched_at < INBOX_DISPATCH_COALESCE_WINDOW_S:
            return True
        del _dispatch_active[terminal_id]
        return False


def _mark_dispatch_active(terminal_id: str) -> None:
    with _dispatch_active_guard:
        _dispatch_active[terminal_id] = time.monotonic()


@contextmanager
def _terminal_delivery_lock(terminal_id: str) -> Iterator[None]:
    """Serialize deliver_pending end to end for one terminal.

    Claiming a row is atomic (#164, #406), but claim and delivery are two
    separate steps: two concurrent callers can each claim a different PENDING
    row for the same terminal and then both call terminal_service.send_input,
    interleaving their paste/delay/Enter sequences at the tmux pane (#709).
    Holding this lock across the whole read-check-claim-send-reset sequence
    makes deliveries to one terminal fully sequential again; other terminals
    are unaffected since each gets its own lock.
    """
    with _delivery_registry_guard:
        lock, count = _delivery_locks.get(terminal_id, (threading.Lock(), 0))
        _delivery_locks[terminal_id] = (lock, count + 1)
    try:
        with lock:
            yield
    finally:
        with _delivery_registry_guard:
            lock, count = _delivery_locks[terminal_id]
            if count <= 1:
                del _delivery_locks[terminal_id]
            else:
                _delivery_locks[terminal_id] = (lock, count - 1)


class InboxService:
    """Delivers one pending message per terminal per IDLE cycle."""

    async def run(self, registry: PluginRegistry | None = None) -> None:
        queue = bus.subscribe("terminal.*.status")
        logger.info("InboxService started")

        while True:
            try:
                event = await queue.get()
                status_value = event["data"]["status"]
                terminal_id = terminal_id_from_topic(event["topic"])
                # Any published status event means _apply_detection ran a genuine
                # transition for this terminal (it dedupes no-op repeats), so the
                # cached value a concurrent deliver_pending call would have seen is
                # now stale. Clear the busy marker before deciding whether to
                # deliver, so a real ready event right behind a dispatch is never
                # starved by its own dispatch (#709).
                _clear_dispatch_active(terminal_id)
                if status_value in (TerminalStatus.IDLE.value, TerminalStatus.COMPLETED.value):
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
        """
        with _terminal_delivery_lock(terminal_id):
            if _is_dispatch_active(terminal_id):
                # A prior dispatch for this terminal has not yet been confirmed
                # by a real status transition (#709): the lock only serializes
                # this call after that one, it does not prove the terminal has
                # actually started working on the first message. Coalesce
                # instead of consuming another row into the same stale ready
                # window; the still-PENDING message is picked up by the next
                # genuine status event or the reconcile sweep.
                return

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

            # Claim atomically (#164, #406): a concurrent deliver_pending call for this
            # terminal can reach this point before this one commits, so only an atomic
            # UPDATE, not a prior read, decides who delivers each message.
            messages = claim_pending_messages(terminal_id, limit=limit)
            if not messages:
                return

            # Deliver in contiguous runs of the same sender. With the default
            # num_messages=1 this is a single run; when draining all pending messages
            # (num_messages=0) a batch can span multiple senders, so each run is sent
            # separately to keep PostSendMessageEvent attribution correct: otherwise
            # every message would be attributed to messages[0].sender_id.
            for sender_id, group in groupby(messages, key=lambda m: m.sender_id):
                batch = list(group)
                combined = "\n".join(m.message for m in batch)
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
                    logger.info(f"Delivered {len(batch)} message(s) to terminal {terminal_id}")
                    # Mark busy (#709): the dispatch went through, but the cached
                    # status will not reflect it until the real pipeline detects
                    # the terminal's own output, so a concurrent or immediately
                    # following call must not read the still-stale ready status.
                    _mark_dispatch_active(terminal_id)
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
                except Exception as e:
                    for message in batch:
                        logger.error(
                            f"Failed to deliver message {message.id} to {terminal_id}: {e}"
                        )
                        update_message_status(message.id, MessageStatus.FAILED)

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
