"""Delivers queued inbox messages when terminals become ready.

Consumer: terminal.{id}.status
"""

import asyncio
import logging
import threading
import time
from itertools import groupby
from typing import Any, Optional

from cli_agent_orchestrator.backends.base import TerminalNotFoundError
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
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import managed_launch, terminal_service, wake_receipts
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

# How long the wake watcher waits for an unmanaged receiver to transition out
# of IDLE after a paste before it concludes the paste may not have started a
# turn.  A second, shorter window applies after the one allowed nudge.
WAKE_CONFIRMATION_SECONDS = 45.0
WAKE_NUDGE_WINDOW_SECONDS = 15.0

# Statuses that mean "still parked": a transition to anything else is a wake.
_PARKED_STATUSES = frozenset(
    {TerminalStatus.IDLE.value, TerminalStatus.COMPLETED.value, TerminalStatus.IDLE}
)


class InboxService:
    """Delivers one pending message per terminal per IDLE cycle.

    Also owns the unmanaged wake-confirmation watcher (cond-0072 scoped half):
    after an unmanaged paste it watches the receiver's status for a transition
    out of IDLE, records a durable wake receipt, and nudges at most once.  See
    :mod:`wake_receipts` for the truth and the idempotency boundary.
    """

    def __init__(self) -> None:
        # Execution cache only: all truth lives in the durable sidecar, so a
        # restart loses nothing here and never re-acts.  Keyed by
        # ``(terminal_id, message_id)``; the value is the scheduling future.
        self._wake_confirmations: dict[tuple[str, str], Any] = {}
        self._wake_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self, registry: PluginRegistry | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        # Re-arm or finalize watchers for records left ``watching`` by a prior
        # process: never a second nudge, never a reopened confirmed record.
        self._load_wake_confirmations()
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

    # --- unmanaged wake confirmation (scoped cond-0072 wake gap) ---------

    def _ensure_wake_confirmation(self, terminal_id: str, message_id: Any) -> None:
        """One idempotent trigger for one message's wake receipt.

        Called from :meth:`deliver_pending` after an unmanaged paste
        succeeds, so it covers the POST path, the event-loop path, the
        OpenCode poller, and the reconcile sweep simultaneously.  Does
        nothing when a watcher is already armed or a durable record already
        exists for the key — the at-most-one-watcher and at-most-one-nudge
        guarantees both fall out of that single check under the lock.
        """
        key = (terminal_id, str(message_id))
        with self._wake_lock:
            if key in self._wake_confirmations:
                return
            if wake_receipts.get(terminal_id, str(message_id)) is not None:
                # A record exists (watching, or already finalized).  Never
                # re-arm: a confirmed record is terminal, and a watching one
                # is either being observed or was left by a prior process and
                # will be loaded by ``_load_wake_confirmations``.
                return
            delivered_at = wake_receipts.utcnow()
            deadline_at = wake_receipts.deadline_iso(delivered_at, WAKE_CONFIRMATION_SECONDS)
            native_session_id = self._native_session_id_for(terminal_id)
            wake_receipts.ensure_watching(
                terminal_id,
                str(message_id),
                native_session_id=native_session_id,
                delivered_at=delivered_at,
                deadline_at=deadline_at,
            )
            self._arm_watcher_locked(key, terminal_id, str(message_id), deadline_at)

    def _arm_watcher_locked(
        self,
        key: tuple[str, str],
        terminal_id: str,
        message_id: str,
        deadline_at: str,
    ) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            # No event loop (e.g. a sync call before run() started): the
            # ``watching`` sidecar is the truth and startup load will arm it.
            return
        future = asyncio.run_coroutine_threadsafe(
            self._watch_wake(terminal_id, message_id, deadline_at), loop
        )
        self._wake_confirmations[key] = future

    def _native_session_id_for(self, terminal_id: str) -> Optional[str]:
        """The receiver's native session id, or explicit None for a v1 terminal.

        A v1 terminal exposes no native session: recorded as null, never
        invented, so the receipt is honest about what it could not observe.
        """
        identity = managed_launch.managed_control_identity(terminal_id)
        if not identity:
            return None
        return identity.get("native_session_id")

    def _load_wake_confirmations(self) -> None:
        """Re-arm or finalize ``watching`` records left by a prior process.

        Past deadline: finalize ``wake_unconfirmed`` without nudging (the
        in-flight nudge decision did not survive; fail closed).  Within
        deadline: re-arm observation only, and never send a second nudge once
        ``nudge_intent_at``/``nudge_sent_at`` exists.
        """
        now = time.time()
        for terminal_id, message_id, record in wake_receipts.iter_records():
            if record.get("state") != wake_receipts.WATCHING:
                continue
            deadline_ts = wake_receipts.parse_iso_timestamp(record.get("deadline_at"))
            key = (terminal_id, message_id)
            with self._wake_lock:
                if key in self._wake_confirmations:
                    continue
                if deadline_ts is not None and deadline_ts <= now:
                    wake_receipts.record_wake_unconfirmed(
                        terminal_id,
                        message_id,
                        note="watching record was past its deadline at startup; "
                        "the in-flight nudge decision did not survive, so no nudge was sent",
                    )
                    self._emit_wake_event(terminal_id, message_id, wake_receipts.WAKE_UNCONFIRMED)
                    continue
                self._arm_watcher_locked(key, terminal_id, message_id, record.get("deadline_at"))

    async def _watch_wake(self, terminal_id: str, message_id: str, deadline_at: str) -> None:
        """Watch one receiver for a wake transition, or nudge once and conclude."""
        key = (terminal_id, message_id)
        topic = f"terminal.{terminal_id}.status"
        queue = bus.subscribe(topic)
        try:
            transition = await self._await_wake_transition(queue, terminal_id, deadline_at)
            if transition is not None:
                wake_receipts.record_wake_confirmed(terminal_id, message_id, observed=transition)
                self._emit_wake_event(terminal_id, message_id, wake_receipts.WAKE_CONFIRMED)
                return
            await self._nudge_once(queue, terminal_id, message_id, deadline_at)
        except Exception:  # noqa: BLE001 - a watcher must not kill the loop
            logger.exception("wake watcher for %s/%s failed", terminal_id, message_id)
            try:
                wake_receipts.record_wake_unconfirmed(
                    terminal_id, message_id, note="the wake watcher raised unexpectedly"
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to record an unexpected wake-unconfirmed")
        finally:
            with self._wake_lock:
                self._wake_confirmations.pop(key, None)
            bus.unsubscribe(topic, queue)

    async def _await_wake_transition(
        self, queue: asyncio.Queue, terminal_id: str, deadline_at: str
    ) -> Optional[dict[str, Any]]:
        """Return the first out-of-IDLE transition, or None at the deadline."""
        deadline_ts = wake_receipts.parse_iso_timestamp(deadline_at)
        last = status_monitor.get_status(terminal_id)
        last_value = last.value if isinstance(last, TerminalStatus) else last
        while True:
            if deadline_ts is None:
                return None
            remaining = deadline_ts - time.time()
            if remaining <= 0:
                return None
            try:
                event = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            topic = event.get("topic", "")
            if terminal_id_from_topic(topic) != terminal_id:
                continue
            to_value = event.get("data", {}).get("status")
            if to_value in _PARKED_STATUSES:
                last_value = to_value
                continue
            return {
                "event": "status-transition",
                "from_status": last_value,
                "to_status": to_value,
                "at": wake_receipts.utcnow(),
            }

    async def _nudge_once(
        self, queue: asyncio.Queue, terminal_id: str, message_id: str, deadline_at: str
    ) -> None:
        """Exactly one bare Enter, intent-before-effect, then a bounded re-watch."""
        # Re-resolve status: a transition in the gap is a real wake.
        status = status_monitor.get_status(terminal_id)
        status_value = status.value if isinstance(status, TerminalStatus) else status
        if status_value not in _PARKED_STATUSES:
            wake_receipts.record_wake_confirmed(
                terminal_id,
                message_id,
                observed={
                    "event": "status-transition",
                    "from_status": None,
                    "to_status": status_value,
                    "at": wake_receipts.utcnow(),
                },
            )
            self._emit_wake_event(terminal_id, message_id, wake_receipts.WAKE_CONFIRMED)
            return
        existing = wake_receipts.get(terminal_id, message_id) or {}
        if existing.get("nudge_intent_at") is not None:
            # A prior incarnation already decided to nudge (or did): never a
            # second nudge.  Fail closed to unconfirmed rather than risk a
            # duplicate Enter that could submit a stranger's queued input.
            wake_receipts.record_wake_unconfirmed(
                terminal_id,
                message_id,
                note="no wake transition; a nudge was already recorded by a prior watcher "
                "and was not re-sent",
            )
            self._emit_wake_event(terminal_id, message_id, wake_receipts.WAKE_UNCONFIRMED)
            return
        # Intent before effect, durably: a crash here is recovered as
        # wake_unconfirmed with the nudge never re-sent.
        wake_receipts.record_nudge_intent(terminal_id, message_id, at=wake_receipts.utcnow())
        try:
            # One bare Enter through the identity-checked path; never re-paste
            # the message text.  send_special_key refuses managed panes and
            # re-verifies the pane, so the nudge cannot land in a stranger's
            # composer.
            terminal_service.send_special_key(terminal_id, "Enter")
        except Exception:  # noqa: BLE001 - a failed nudge is still recorded as sent-attempted
            logger.warning("wake nudge Enter for %s raised; recorded as attempted", terminal_id)
        wake_receipts.record_nudge_sent(terminal_id, message_id, at=wake_receipts.utcnow())
        # A second bounded window for the nudge to start a turn.
        post_deadline = wake_receipts.deadline_iso(
            wake_receipts.utcnow(), WAKE_NUDGE_WINDOW_SECONDS
        )
        transition = await self._await_wake_transition(queue, terminal_id, post_deadline)
        if transition is not None:
            wake_receipts.record_wake_confirmed(terminal_id, message_id, observed=transition)
            self._emit_wake_event(terminal_id, message_id, wake_receipts.WAKE_CONFIRMED)
        else:
            wake_receipts.record_wake_unconfirmed(
                terminal_id,
                message_id,
                note="no wake transition within the window after one nudge; the paste may "
                "not have started a turn",
            )
            self._emit_wake_event(terminal_id, message_id, wake_receipts.WAKE_UNCONFIRMED)

    def _emit_wake_event(self, terminal_id: str, message_id: str, state: str) -> None:
        """One event-bus record so a sentinel sees an open, alertable obligation."""
        try:
            bus.publish(
                f"inbox.{terminal_id}.wake-receipt",
                {
                    "message_id": message_id,
                    "terminal_id": terminal_id,
                    "state": state,
                    "source": wake_receipts.SOURCE,
                },
            )
        except Exception:  # noqa: BLE001 - the receipt is the truth; the event is advisory
            logger.warning(
                "could not publish wake-receipt event for %s/%s", terminal_id, message_id
            )

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
        limit = num_messages if num_messages > 0 else 100
        messages = get_pending_messages(terminal_id, limit=limit)
        if not messages:
            return

        # P1-7 (final conformance §20.2f): for a receiver with a live managed
        # provider session, deliver each exact message through its provider
        # bridge — the provider's own model-turn acceptance is recorded as the
        # durable submitted acknowledgement. Anything the bridge cannot take
        # falls through to the ordinary paste path, from which NO
        # acknowledgement is ever inferred.
        remaining = []
        managed_identity = managed_launch.managed_control_identity(terminal_id)
        for message in messages:
            if managed_launch.deliver_inbox_via_bridge(
                terminal_id,
                message_id=message.id,
                message=message.message,
                sender_id=message.sender_id,
            ):
                update_message_status(message.id, MessageStatus.DELIVERED)
                logger.info(
                    f"Delivered message {message.id} to terminal {terminal_id} "
                    "via the managed provider bridge (provider-native ack)"
                )
            else:
                remaining.append(message)
        messages = remaining
        if not messages:
            return
        if managed_identity is not None:
            # A managed bridge owns provider stdin.  If native delivery is
            # temporarily unavailable, preserve the inbox rows as pending;
            # falling through to terminal paste would write into a renderer
            # pane that cannot acknowledge or safely consume the message.
            logger.info(
                "Preserving %d pending message(s) for managed terminal %s; "
                "provider-native delivery is not currently available",
                len(messages),
                terminal_id,
            )
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
                # Unmanaged paste path only (managed receivers ack via the
                # bridge above).  One idempotent wake-confirmation trigger per
                # message: a watching receipt is opened, and the watcher (owned
                # by this service's event loop) awaits a wake transition or
                # nudges at most once.  Idempotent across the POST, event-loop,
                # poller, and reconcile paths that all funnel through here.
                for message in batch:
                    self._ensure_wake_confirmation(terminal_id, message.id)
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
