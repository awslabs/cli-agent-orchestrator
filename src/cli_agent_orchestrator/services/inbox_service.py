"""Delivers queued inbox messages when terminals become ready.

Consumer: terminal.{id}.status
"""

import asyncio
import logging
import threading
import time
from itertools import groupby
from typing import Dict

from cli_agent_orchestrator.backends.base import TerminalNotFoundError
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    fail_orphaned_pending_messages,
    get_pending_messages,
    list_pending_receiver_ids_by_provider,
    list_pending_receiver_ids_older_than,
    update_message_status,
)
from cli_agent_orchestrator.constants import (
    EAGER_INBOX_DELIVERY,
    INBOX_RECONCILE_GRACE_SECONDS,
    INBOX_SUBMIT_CONFIRM_TIMEOUT,
    INBOX_SUBMIT_CONFIRMATION,
    INBOX_SUBMIT_MAX_RESUBMITS,
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

# Poll interval for the submit-confirmation wait. Short because the common case
# is a healthy delivery that is already PROCESSING on the first or second read;
# the cost of a dropped delivery is bounded by INBOX_SUBMIT_CONFIRM_TIMEOUT.
_SUBMIT_CONFIRM_POLL_INTERVAL = 0.25

# Statuses proving the receiver consumed this delivery (left its ready state).
# WAITING_USER_ANSWER counts: the message was read and the agent is now asking.
_SUBMIT_STARTED_STATUSES = {
    TerminalStatus.PROCESSING,
    TerminalStatus.COMPLETED,
    TerminalStatus.WAITING_USER_ANSWER,
}


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
        eager = False
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
            eager = True

        # Mark DELIVERED before sending (#164). send_input() types into the tmux
        # pane; that output flows back through the FIFO/StatusMonitor pipeline and
        # can re-emit an IDLE/COMPLETED status event, re-entering deliver_pending.
        # If the messages were still PENDING then, they would be delivered twice.
        # Marking them DELIVERED first closes that window; the except path resets
        # them to FAILED.
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
            try:
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
                    boundary = terminal_service.dispatch_input(terminal_id, combined)
                else:
                    boundary = terminal_service.dispatch_input(
                        terminal_id,
                        combined,
                        registry=registry,
                        sender_id=sender_id,
                        orchestration_type=OrchestrationType.SEND_MESSAGE,
                    )
                logger.info(f"Delivered {len(batch)} message(s) to terminal {terminal_id}")
                if INBOX_SUBMIT_CONFIRMATION and not eager:
                    if get_backend().supports_event_inbox():
                        # Event-inbox backends (herdr) push delivery synchronously
                        # inline on their single asyncio event loop thread
                        # (herdr_inbox_service.py's _event_loop -> _deliver, with
                        # no asyncio.to_thread offload -- see the TODO at
                        # herdr_inbox_service.py:1276-1277). _confirm_submitted's
                        # blocking time.sleep() would stall that whole session's
                        # event processing for up to
                        # INBOX_SUBMIT_CONFIRM_TIMEOUT * (1 + INBOX_SUBMIT_MAX_RESUBMITS)
                        # seconds. Skip confirmation there until that offload
                        # exists -- the drop stays undetected on herdr, same as
                        # before this fix, instead of freezing the session.
                        logger.debug(
                            "Skipping inbox submit confirmation for terminal %s: "
                            "event-inbox backend delivery runs on the API event loop",
                            terminal_id,
                        )
                    else:
                        # Still holding the per-terminal delivery lock,
                        # deliberately: the pile-up in #78 ({MSG1}{MSG2} in one
                        # composer) is exactly a second delivery landing while
                        # the first sits unsubmitted. Eager delivery is excluded
                        # because its receiver was already PROCESSING -- "left
                        # the ready state" proves nothing there.
                        self._confirm_submitted(
                            terminal_id,
                            combined,
                            boundary,
                            registry=registry,
                            sender_id=sender_id,
                        )
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
                    logger.error(f"Failed to deliver message {message.id} to {terminal_id}: {e}")
                    update_message_status(message.id, MessageStatus.FAILED)

    def _wait_for_pickup(
        self,
        terminal_id: str,
        boundary: int,
    ) -> bool:
        """Wait for a started status that is EVIDENCE OF THIS DELIVERY.

        Mirrors ``terminal_service._wait_for_post_dispatch_start`` (the
        deferred-init path's confirmation) in synchronous form, and for the same
        reason: a status VALUE cannot say when it was earned. A COMPLETED latched
        from the receiver's previous turn satisfies "not ready" instantly, so the
        causal pair from ``status_observation`` is required -- the generation the
        status was EARNED at must exceed the delivery boundary.

        Callers never pass event-inbox (herdr) terminals here: that backend
        pushes delivery inline on its own asyncio event loop thread, so
        ``_deliver_pending_locked`` skips confirmation for it entirely rather
        than block that loop (see the caller).
        """
        deadline = time.monotonic() + INBOX_SUBMIT_CONFIRM_TIMEOUT
        while True:
            observation = status_monitor.status_observation(terminal_id)
            if (
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
    ) -> bool:
        """Confirm the receiver began processing this delivery; re-submit if not.

        Reuses ``terminal_service.redeliver_dropped_message_with_boundary`` --
        the same decision helper the deferred-init (#479/#496) and synchronous
        step (#562) paths use -- so the three delivery paths share one notion of
        what a dropped send looks like and how to recover from it. The recovery
        is bare Enter when the text is still visible in the receiver's composer
        (only the Enter was swallowed), a full re-send when it vanished and the
        provider is probe-capable, and nothing at all otherwise.

        ``full_resend_requires_probe=True`` on purpose: without a direct status
        probe there is no way to tell "paste dropped" from "the agent already
        read it and the prompt scrolled away", and re-pasting an inbox message
        into a working agent delivers the same instruction twice. For those
        providers the bare-Enter branch, which cannot duplicate anything, is the
        only recovery -- see Open questions in the report: for kiro_cli and
        claude_code even that branch is currently inert because neither
        implements ``extract_current_composer``.

        Returns True when the delivery was confirmed (or the receiver was found
        already working), False when it stayed unconfirmed. The message is left
        DELIVERED either way: it HAS been typed into the pane, so reverting it to
        PENDING would let the reconcile sweep paste it a second time.
        """
        if self._wait_for_pickup(terminal_id, boundary):
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
            if self._wait_for_pickup(terminal_id, boundary):
                return True

        logger.error(
            "Inbox delivery to %s is unconfirmed after %d recovery attempt(s); "
            "the message may be sitting unsubmitted in the agent's prompt",
            terminal_id,
            INBOX_SUBMIT_MAX_RESUBMITS,
        )
        return False

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

        for terminal_id in list_pending_receiver_ids_older_than(INBOX_RECONCILE_GRACE_SECONDS):
            try:
                self.deliver_pending(terminal_id, registry=registry)
            except Exception as e:
                logger.debug(f"Inbox reconciliation failed for {terminal_id}: {e}")


inbox_service = InboxService()
