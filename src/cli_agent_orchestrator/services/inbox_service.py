"""Delivers queued inbox messages when terminals become ready.

Consumer: terminal.{id}.status
"""

import asyncio
import functools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import groupby
from typing import Callable, Dict, Iterable, Optional, Set

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
    INBOX_SWEEP_FANOUT,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
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
    ) -> None:
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
                self._deliver_pending_locked(terminal_id, num_messages, registry, notify)
        finally:
            notify()

    def _deliver_pending_locked(
        self,
        terminal_id: str,
        num_messages: int,
        registry: PluginRegistry | None,
        after_dispatch: Callable[[], None] = lambda: None,
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
                logger.info(f"Delivered {len(batch)} message(s) to terminal {terminal_id}")
                # The paste is in the pane: the immediate-delivery caller may
                # answer now. Everything below is confirmation and recovery.
                after_dispatch()
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
                if e.reason == TerminalInputBlockedReason.WAITING_USER_ANSWER:
                    for message in batch:
                        update_message_status(message.id, MessageStatus.PENDING)
                    logger.warning(
                        f"Terminal {terminal_id} is waiting on a user answer; leaving "
                        f"{len(batch)} message(s) pending for retry: {e}"
                    )
                else:
                    for message in batch:
                        logger.error(
                            f"Delivery into terminal {terminal_id} refused for "
                            f"message {message.id}: {e}"
                        )
                        update_message_status(message.id, MessageStatus.FAILED)
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
            if self._wait_for_pickup(terminal_id, boundary):
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
                pool.submit(self.deliver_pending, terminal_id, registry=registry): terminal_id
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
        )


inbox_service = InboxService()
