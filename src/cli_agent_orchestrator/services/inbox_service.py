"""Delivers queued inbox messages when terminals become ready.

Consumer: terminal.{id}.status
"""

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from itertools import groupby
from typing import Any, Optional

from cli_agent_orchestrator.backends.base import TerminalNotFoundError
from cli_agent_orchestrator.clients.database import (
    get_pending_message,
    get_pending_messages,
    is_message_pending,
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
from cli_agent_orchestrator.services import (
    callback_recovery,
    control_input_service,
    managed_launch,
    terminal_service,
    wake_receipts,
)
from cli_agent_orchestrator.services.control_input_contract import ACCEPTED, REFUSED
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.execution_mode import NATIVE_TUI
from cli_agent_orchestrator.services.status_monitor import status_monitor

# Imported by name (not via the terminal_service module handle) so the
# typed-refusal catch below keeps working in tests that replace the whole
# ``inbox_service.terminal_service`` module attribute with a mock.
from cli_agent_orchestrator.services.terminal_service import TerminalInputRefusedError
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

# How long the wake watcher waits for an unmanaged receiver to transition out
# of IDLE after a paste before it concludes the paste may not have started a
# turn.  A second, shorter window applies after the one allowed nudge.
WAKE_CONFIRMATION_SECONDS = 45.0
WAKE_NUDGE_WINDOW_SECONDS = 15.0

# Managed delivery already has a durable exact-message-id journal at the
# provider bridge. These bounded in-process stripes prevent two ordinary
# cao-server callers from entering that bridge concurrently without creating a
# terminal inbox status before a provider effect exists. A process restart
# drops the lock while the still-PENDING row is safely reconciled through the
# bridge journal.
MANAGED_DELIVERY_LOCK_TIMEOUT_SECONDS = 0.25
_MANAGED_DELIVERY_LOCKS = tuple(threading.Lock() for _ in range(256))


class _NativeManagedSendRefused(RuntimeError):
    """A native-TUI managed send was refused with zero bytes proven to the pane.

    Retryable: the typed refusal proves nothing reached the pane, so resetting
    the claimed rows to PENDING cannot duplicate a provider effect.
    """

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"sender run refused ({reason_code}); zero bytes reached the pane")


class _NativeManagedSendUndeliverable(RuntimeError):
    """A native-TUI managed send ended without a delivery proof and without a
    zero-byte proof (ambiguous or otherwise not accepted/refused).

    Never replayed blindly: the rows terminalize under the existing hard-failure
    semantics, preserving the inbox row state for later investigation without
    claiming that an unjournaled pane write can be reconstructed exactly.
    """


class _CallbackCompletionGenerationReplaced(RuntimeError):
    """The callback receiver changed before any generic delivery effect."""


# Statuses that mean "still parked": a transition to anything else is a wake.
_PARKED_STATUSES = frozenset(
    {TerminalStatus.IDLE.value, TerminalStatus.COMPLETED.value, TerminalStatus.IDLE}
)


@dataclass(frozen=True)
class _WakePreparation:
    key: tuple[str, str]
    terminal_id: str
    message_id: str
    topic: str
    queue: asyncio.Queue
    baseline_status: str
    delivery_identity: dict[str, Any]


class InboxService:
    @staticmethod
    @contextlib.contextmanager
    def _callback_completion_delivery_claim(messages):
        """Hold exact supervisor generations across the final pane send."""
        bound = [message for message in messages if message.callback_completion_key is not None]
        keys = {
            (
                message.receiver_id,
                "callback-target-generation",
                message.expected_receiver_generation,
            )
            for message in bound
        }
        if any(not generation for _terminal_id, _kind, generation in keys):
            raise _CallbackCompletionGenerationReplaced(
                "callback completion lacks its receiver generation"
            )
        with callback_recovery.generation_lifecycle_claims(keys):
            if any(
                not callback_recovery.current_delivery_binding_matches(message) for message in bound
            ):
                raise _CallbackCompletionGenerationReplaced(
                    "the original supervisor generation was replaced"
                )
            yield

    def _deliver_callback_completions_via_pane(
        self,
        terminal_id: str,
        messages: list[InboxMessage],
        *,
        registry,
        native_managed: bool,
        managed_identity,
    ) -> None:
        """Deliver completion rows through the governed pane/native adapter.

        Ordinary supervisors are intentionally not managed-launch reservations;
        they still need a callback route.  These rows never use the generic
        inbox status claim: the callback lifecycle claim and post-effect receipt
        are the only completion authority.
        """
        if not messages:
            return
        if not native_managed and status_monitor.get_status(terminal_id) not in (
            TerminalStatus.IDLE,
            TerminalStatus.COMPLETED,
        ):
            return
        for message in messages:
            try:
                with self._callback_completion_delivery_claim([message]):
                    callback_recovery.claim_callback_effect(
                        message.callback_completion_key,
                        message.id,
                    )
                    if native_managed:
                        self._send_native_managed_text(
                            terminal_id, message.message, managed_identity
                        )
                    else:
                        # The generic pane writer is neither a provider-native
                        # acknowledgement nor byte-for-byte callback proof.
                        # Retain the claimed attempt for ADMIN disposition
                        # until this target has a receipt-bearing adapter.
                        callback_recovery.mark_callback_effect_ambiguous(
                            message.callback_completion_key
                        )
                        continue
                    callback_recovery.commit_callback_effect(
                        message.callback_completion_key,
                        message.id,
                    )
            except callback_recovery.CallbackRecoveryRefused:
                # The claim function has durably recorded the exact pre-I/O
                # replacement fence; the ADMIN disposition owns any release.
                continue
            except Exception as exc:  # noqa: BLE001 - effect result is unknowable
                logger.warning(
                    "Callback completion pane delivery is ambiguous for %s/%s: %s",
                    terminal_id,
                    message.id,
                    exc,
                )
                try:
                    callback_recovery.mark_callback_effect_ambiguous(
                        message.callback_completion_key
                    )
                except callback_recovery.CallbackRecoveryError:
                    pass

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
        self._wake_preparations: dict[tuple[str, str], _WakePreparation] = {}
        self._wake_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _managed_delivery_lock(message_id: int) -> threading.Lock:
        """One bounded process-local serialization stripe for one inbox id."""
        return _MANAGED_DELIVERY_LOCKS[message_id % len(_MANAGED_DELIVERY_LOCKS)]

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
        status = status_monitor.get_status(terminal_id)
        preparation = self._prepare_wake_confirmation(
            terminal_id, message_id, baseline_status=status
        )
        if preparation is not None:
            self._commit_wake_confirmation(preparation)

    @staticmethod
    def _status_value(status: Any) -> Any:
        return status.value if isinstance(status, TerminalStatus) else status

    @staticmethod
    def _delivery_identity(terminal_id: str) -> Optional[dict[str, Any]]:
        """The immutable-enough v1 pane identity a later nudge must still match."""
        try:
            terminal = terminal_service.get_terminal(terminal_id)
        except Exception:  # noqa: BLE001 - absence means no safe nudge authority
            return None
        if not isinstance(terminal, dict):
            return None
        fields = ("id", "provider", "session_name", "pane_id", "window_id")
        identity = {field: terminal.get(field) for field in fields}
        if identity["id"] != terminal_id or not identity["session_name"]:
            return None
        return identity

    def _record_wake_confirmed_if_current(
        self,
        terminal_id: str,
        message_id: str,
        *,
        delivery_identity: Optional[dict[str, Any]],
        observed: dict[str, Any],
    ) -> bool:
        """Confirm a wake only while the delivery-time terminal still owns it."""
        if delivery_identity is None or self._delivery_identity(terminal_id) != delivery_identity:
            wake_receipts.record_wake_unconfirmed(
                terminal_id,
                message_id,
                note="a wake transition was observed after the delivery-time terminal "
                "identity changed or became unreadable; it cannot confirm this delivery",
            )
            self._emit_wake_event(terminal_id, message_id, wake_receipts.WAKE_UNCONFIRMED)
            return False
        wake_receipts.record_wake_confirmed(terminal_id, message_id, observed=observed)
        self._emit_wake_event(terminal_id, message_id, wake_receipts.WAKE_CONFIRMED)
        return True

    def _prepare_wake_confirmation(
        self,
        terminal_id: str,
        message_id: Any,
        *,
        baseline_status: Any,
    ) -> Optional[_WakePreparation]:
        """Subscribe before the pane effect without yet opening durable intent."""
        baseline_value = self._status_value(baseline_status)
        if baseline_value not in _PARKED_STATUSES:
            return None
        delivery_identity = self._delivery_identity(terminal_id)
        if delivery_identity is None:
            logger.warning(
                "wake observation for %s/%s has no delivery-time terminal identity; "
                "delivery may proceed but no later nudge is authorized",
                terminal_id,
                message_id,
            )
            return None
        key = (terminal_id, str(message_id))
        with self._wake_lock:
            if key in self._wake_confirmations or key in self._wake_preparations:
                return None
            if wake_receipts.get(terminal_id, str(message_id)) is not None:
                return None
            topic = f"terminal.{terminal_id}.status"
            preparation = _WakePreparation(
                key=key,
                terminal_id=terminal_id,
                message_id=str(message_id),
                topic=topic,
                queue=bus.subscribe(topic),
                baseline_status=str(baseline_value),
                delivery_identity=delivery_identity,
            )
            self._wake_preparations[key] = preparation
            return preparation

    def _commit_wake_confirmation(self, preparation: _WakePreparation) -> None:
        """Open durable intent only after send success, retaining the pre-send queue."""
        with self._wake_lock:
            if self._wake_preparations.pop(preparation.key, None) is not preparation:
                bus.unsubscribe(preparation.topic, preparation.queue)
                return
            delivered_at = wake_receipts.utcnow()
            deadline_at = wake_receipts.deadline_iso(delivered_at, WAKE_CONFIRMATION_SECONDS)
            wake_receipts.ensure_watching(
                preparation.terminal_id,
                preparation.message_id,
                native_session_id=self._native_session_id_for(preparation.terminal_id),
                delivered_at=delivered_at,
                deadline_at=deadline_at,
                delivery_identity=preparation.delivery_identity,
                baseline_status=preparation.baseline_status,
            )
            self._arm_watcher_locked(
                preparation.key,
                preparation.terminal_id,
                preparation.message_id,
                deadline_at,
                queue=preparation.queue,
                baseline_status=preparation.baseline_status,
                delivery_identity=preparation.delivery_identity,
            )

    def _abort_wake_confirmation(self, preparation: _WakePreparation) -> None:
        """Cancel a provisional subscriber when the pane send did not succeed."""
        with self._wake_lock:
            self._wake_preparations.pop(preparation.key, None)
        bus.unsubscribe(preparation.topic, preparation.queue)

    def _arm_watcher_locked(
        self,
        key: tuple[str, str],
        terminal_id: str,
        message_id: str,
        deadline_at: str,
        *,
        queue: Optional[asyncio.Queue] = None,
        baseline_status: Optional[str] = None,
        delivery_identity: Optional[dict[str, Any]] = None,
        allow_nudge: bool = True,
    ) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            # No event loop (e.g. a sync call before run() started): the
            # ``watching`` sidecar is the truth and startup load will arm it.
            if queue is not None:
                bus.unsubscribe(f"terminal.{terminal_id}.status", queue)
            return
        if queue is None:
            queue = bus.subscribe(f"terminal.{terminal_id}.status")
        future = asyncio.run_coroutine_threadsafe(
            self._watch_wake(
                terminal_id,
                message_id,
                deadline_at,
                queue=queue,
                baseline_status=baseline_status,
                delivery_identity=delivery_identity,
                allow_nudge=allow_nudge,
            ),
            loop,
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
                self._arm_watcher_locked(
                    key,
                    terminal_id,
                    message_id,
                    record.get("deadline_at"),
                    baseline_status=record.get("baseline_status"),
                    delivery_identity=record.get("delivery_identity"),
                    allow_nudge=False,
                )

    async def _watch_wake(
        self,
        terminal_id: str,
        message_id: str,
        deadline_at: str,
        *,
        queue: Optional[asyncio.Queue] = None,
        baseline_status: Optional[str] = None,
        delivery_identity: Optional[dict[str, Any]] = None,
        allow_nudge: bool = True,
    ) -> None:
        """Watch one receiver for a wake transition, or nudge once and conclude."""
        key = (terminal_id, message_id)
        topic = f"terminal.{terminal_id}.status"
        queue = queue or bus.subscribe(topic)
        try:
            transition = await self._await_wake_transition(
                queue,
                terminal_id,
                deadline_at,
                baseline_status=baseline_status,
            )
            if transition is not None:
                self._record_wake_confirmed_if_current(
                    terminal_id,
                    message_id,
                    delivery_identity=delivery_identity,
                    observed=transition,
                )
                return
            if not allow_nudge:
                wake_receipts.record_wake_unconfirmed(
                    terminal_id,
                    message_id,
                    note="the watcher was restored after process restart and was "
                    "observation-only; no new nudge was sent",
                )
                self._emit_wake_event(
                    terminal_id,
                    message_id,
                    wake_receipts.WAKE_UNCONFIRMED,
                )
                return
            await self._nudge_once(
                queue,
                terminal_id,
                message_id,
                deadline_at,
                delivery_identity=delivery_identity,
            )
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
        self,
        queue: asyncio.Queue,
        terminal_id: str,
        deadline_at: str,
        *,
        baseline_status: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Return the first out-of-IDLE transition, or None at the deadline."""
        deadline_ts = wake_receipts.parse_iso_timestamp(deadline_at)
        last = status_monitor.get_status(terminal_id)
        sampled_value = self._status_value(last)
        last_value = baseline_status or sampled_value
        if baseline_status in _PARKED_STATUSES and sampled_value not in _PARKED_STATUSES:
            return {
                "event": "status-transition",
                "from_status": baseline_status,
                "to_status": sampled_value,
                "at": wake_receipts.utcnow(),
                "observation": "arm-time-sample",
            }
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
        self,
        queue: asyncio.Queue,
        terminal_id: str,
        message_id: str,
        deadline_at: str,
        *,
        delivery_identity: Optional[dict[str, Any]] = None,
    ) -> None:
        """Exactly one bare Enter, intent-before-effect, then a bounded re-watch."""
        # Re-resolve status: a transition in the gap is a real wake.
        status = status_monitor.get_status(terminal_id)
        status_value = self._status_value(status)
        if status_value not in _PARKED_STATUSES:
            self._record_wake_confirmed_if_current(
                terminal_id,
                message_id,
                delivery_identity=delivery_identity,
                observed={
                    "event": "status-transition",
                    "from_status": None,
                    "to_status": status_value,
                    "at": wake_receipts.utcnow(),
                },
            )
            return
        if delivery_identity is None or self._delivery_identity(terminal_id) != delivery_identity:
            wake_receipts.record_wake_unconfirmed(
                terminal_id,
                message_id,
                note="the delivery-time terminal identity could not be revalidated; "
                "no nudge was sent",
            )
            self._emit_wake_event(terminal_id, message_id, wake_receipts.WAKE_UNCONFIRMED)
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
            self._record_wake_confirmed_if_current(
                terminal_id,
                message_id,
                delivery_identity=delivery_identity,
                observed=transition,
            )
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
        required_message_id: int | None = None,
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
        managed_identity = managed_launch.managed_control_identity(terminal_id)
        native_managed = (
            managed_identity is not None and managed_identity.get("execution_mode") == NATIVE_TUI
        )
        if required_message_id is not None:
            exact = get_pending_message(terminal_id, required_message_id)
            messages = [exact] if exact is not None else []
        else:
            # Scan past parked identity-bound rows. With the production default
            # of one message, selecting only the oldest row lets one stale
            # recovery callback starve every later valid message forever.
            # Scan beyond a stale head for every managed transport.  Native
            # managed rows need this too: otherwise the default one-row scan
            # fails G1 and returns before a valid G2 row directly behind it.
            scan_parked = managed_identity is not None
            limit = 100 if scan_parked else (num_messages if num_messages > 0 else 100)
            messages = get_pending_messages(terminal_id, limit=limit)
        if not messages:
            return

        # M3 is exact-generation authority. A queued row that names an old
        # managed generation is never allowed to drift onto a successor or sit
        # PENDING for an infinite retry loop. Terminalize it before selecting
        # any bridge/pane path; post-claim ambiguity remains non-replayable in
        # the existing durable operation records.
        if managed_identity is not None:
            from cli_agent_orchestrator.constants import COMPANION_DIR
            from cli_agent_orchestrator.services import generation_fence

            generation = managed_identity.get("generation")
            if (
                isinstance(generation, str)
                and generation_fence.installed_receipt(COMPANION_DIR, terminal_id, generation)
                is not None
            ):
                for message in messages:
                    update_message_status(message.id, MessageStatus.FAILED)
                logger.info(
                    "Terminalized %d queued inbox row(s) for parked generation %s/%s",
                    len(messages),
                    terminal_id,
                    generation,
                )
                return
            if isinstance(generation, str):
                exact_messages = []
                for message in messages:
                    expected = message.expected_receiver_generation
                    if expected is None and managed_identity.get("vintage") == "v2":
                        # Pre-M3 generic rows were not bound to the receiver
                        # generation. Never let a crash between park receipt
                        # publication and eager DB cleanup retarget one onto a
                        # successor; it is visible terminal history, not work
                        # a current provider is entitled to receive.
                        update_message_status(message.id, MessageStatus.FAILED)
                        logger.info(
                            "Terminalized pre-M3 generationless inbox row %s for managed %s/%s",
                            message.id,
                            terminal_id,
                            generation,
                        )
                    elif expected is not None and expected != generation:
                        update_message_status(message.id, MessageStatus.FAILED)
                        logger.info(
                            "Terminalized old-generation inbox row %s for %s: expected %s, live %s",
                            message.id,
                            terminal_id,
                            expected,
                            generation,
                        )
                    else:
                        exact_messages.append(message)
                messages = exact_messages
                if not messages:
                    return
                # The wider scan exists only to dispose of stale rows. Keep
                # the ordinary delivery budget once the surviving rows are
                # exact-current, including for native TUI delivery below.
                if native_managed and num_messages > 0 and required_message_id is None:
                    messages = messages[:num_messages]

        # P1-7 (final conformance §20.2f): for a receiver with a live managed
        # provider session, deliver each exact message through its provider
        # bridge — the provider's own model-turn acceptance is recorded as the
        # durable submitted acknowledgement. Anything the bridge cannot take
        # falls through to the ordinary paste path, from which NO
        # acknowledgement is ever inferred.
        #
        # The managed branch dispatches on the reservation's execution mode.
        # An ACP generation (every v1; a v2 'acp'; a legacy NULL mode, which
        # never reads as native) keeps the byte-identical bridge path below.
        # A native-TUI v2 generation has NO bridge process by design, so the
        # bridge attempt could only fail and the preserve guard would park the
        # row forever; instead it skips both and falls through to the ordinary
        # idle-gated machinery, with the pane send performed by the
        # generation-bound native text delivery rather than the unmanaged
        # paste (which hard-refuses managed identities anyway).
        remaining = []
        if managed_identity is None or native_managed:
            remaining = messages
        else:
            eligible_attempts = 0
            for message in messages:
                if (
                    num_messages > 0
                    and required_message_id is None
                    and eligible_attempts >= num_messages
                ):
                    break
                lock = self._managed_delivery_lock(message.id)
                if not lock.acquire(timeout=MANAGED_DELIVERY_LOCK_TIMEOUT_SECONDS):
                    logger.info(
                        "Managed inbox delivery for %s/%s is already in progress; "
                        "leaving the row pending for a later cycle",
                        terminal_id,
                        message.id,
                    )
                    remaining.append(message)
                    continue
                try:
                    # Both callers may have selected the row before either
                    # reached this process-wide lock. Re-read the exact row
                    # under it; only the first still-PENDING caller may enter
                    # the provider's durable message-id journal.
                    if not is_message_pending(message.id):
                        continue
                    if (
                        message.is_identity_bound is True
                        and not callback_recovery.current_delivery_binding_matches(message)
                    ):
                        if message.callback_completion_key is not None:
                            logger.warning(
                                "Preserving generation-bound recovery callback %s for %s: "
                                "the original supervisor generation is no longer live",
                                message.id,
                                terminal_id,
                            )
                            continue
                        receipt = None
                        try:
                            receipt = callback_recovery.turn_receipt(message.callback_recovery_key)
                        except callback_recovery.CallbackRecoveryError:
                            callback_recovery.mark_delivery_ambiguous(
                                message.callback_recovery_key,
                                reason_code=(
                                    "stale-binding-reconciliation-failed-"
                                    "manual-resolution-required"
                                ),
                            )
                        if receipt is not None:
                            update_message_status(message.id, MessageStatus.DELIVERED)
                            eligible_attempts += 1
                            continue
                        logger.warning(
                            "Preserving identity-bound inbox message %s for %s: "
                            "the persisted managed generation/session/mode no "
                            "longer matches the live receiver",
                            message.id,
                            terminal_id,
                        )
                        callback_recovery.mark_delivery_ambiguous(
                            message.callback_recovery_key,
                            reason_code=(
                                "source-generation-replaced-" "manual-resolution-required"
                            ),
                        )
                        continue
                    eligible_attempts += 1
                    if message.callback_recovery_key is not None:
                        bridged = managed_launch.deliver_inbox_via_bridge(
                            terminal_id,
                            message_id=message.id,
                            message=message.message,
                            sender_id=message.sender_id,
                            sender_generation=message.sender_generation,
                            message_created_at=message.created_at,
                            expected_generation=message.expected_receiver_generation,
                            expected_provider=message.expected_provider,
                            expected_provider_session_id=(message.expected_provider_session_id),
                            expected_execution_mode=message.expected_execution_mode,
                            recovery_operation_key=message.callback_recovery_key,
                        )
                    else:
                        bridge_kwargs = {}
                        if message.callback_completion_key is not None:
                            bridge_kwargs["expected_generation"] = (
                                message.expected_receiver_generation
                            )
                            # Own the single provider bridge attempt before it
                            # can emit bytes.  A crash after this point is
                            # intentionally ambiguous and is never retried by
                            # a later inbox sweep.
                            callback_recovery.claim_callback_effect(
                                message.callback_completion_key,
                                message.id,
                            )
                        bridged = managed_launch.deliver_inbox_via_bridge(
                            terminal_id,
                            message_id=message.id,
                            message=message.message,
                            sender_id=message.sender_id,
                            **bridge_kwargs,
                        )
                    if bridged:
                        if message.callback_completion_key is not None:
                            callback_recovery.commit_callback_effect(
                                message.callback_completion_key,
                                message.id,
                            )
                        elif update_message_status(message.id, MessageStatus.DELIVERED) is False:
                            logger.warning(
                                "Managed bridge accepted inbox message %s for %s, but its "
                                "still-pending row could not be terminalized",
                                message.id,
                                terminal_id,
                            )
                        logger.info(
                            f"Delivered message {message.id} to terminal {terminal_id} "
                            "via the managed provider bridge (provider-native ack)"
                        )
                    else:
                        # The row never left PENDING. A later cycle re-enters
                        # the exact-message-id bridge journal, which adopts an
                        # existing acknowledgement and refuses blind replay
                        # after ambiguity.
                        if message.callback_completion_key is not None:
                            callback_recovery.mark_callback_effect_ambiguous(
                                message.callback_completion_key
                            )
                        else:
                            remaining.append(message)
                except Exception as exc:  # noqa: BLE001 - no duplicate effect
                    logger.error(
                        "Failed to deliver message %s to managed terminal %s: %s",
                        message.id,
                        terminal_id,
                        exc,
                    )
                    if message.callback_completion_key is not None:
                        try:
                            callback_recovery.mark_callback_effect_ambiguous(
                                message.callback_completion_key
                            )
                        except callback_recovery.CallbackRecoveryError:
                            pass
                    else:
                        remaining.append(message)
                    continue
                finally:
                    lock.release()
        messages = remaining
        if not messages:
            return
        # A callback-recovery operation belongs exclusively to the ACP provider
        # bridge. If its exact managed identity disappeared or changed,
        # preserving the row is the only safe outcome; it must never fall
        # through to native or unmanaged pane delivery.
        callback_completions = [
            message for message in messages if message.callback_completion_key is not None
        ]
        recovery_prompts = [
            message for message in messages if message.callback_recovery_key is not None
        ]
        if callback_completions:
            self._deliver_callback_completions_via_pane(
                terminal_id,
                callback_completions,
                registry=registry,
                native_managed=native_managed,
                managed_identity=managed_identity,
            )
        if recovery_prompts:
            messages = [
                message
                for message in messages
                if message.callback_recovery_key is None and message.callback_completion_key is None
            ]
            logger.info(
                "Preserving %d recovery prompt message(s) for %s; no generic "
                "delivery fallback is permitted",
                len(recovery_prompts),
                terminal_id,
            )
        elif callback_completions:
            messages = [message for message in messages if message.callback_completion_key is None]
        if not messages:
            return
        if managed_identity is not None and not native_managed:
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

        if native_managed:
            # A native pane is never FIFO-classified (its projection reports
            # not_fifo_monitored by design), so the legacy status gate below
            # would park its rows forever.  Delivery eligibility for a native
            # receiver is instead the provider-native live turn state,
            # observed per sender run under the payload write's own pane
            # lease; IDLE or COMPLETED admits the send because both are parked
            # at an input-ready composer, while an active/unknown state leaves
            # the rows PENDING. DELIVERED still follows only the typed ACCEPTED.
            # Wake receipts stay best-effort evidence and never gate the row,
            # so the wake-preparation path stays off here.
            idle_bound_delivery = False
        else:
            status = status_monitor.get_status(terminal_id)
            idle_bound_delivery = status in (TerminalStatus.IDLE, TerminalStatus.COMPLETED)
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

        # Claim each row before sending (#164). send_input() types into the tmux
        # pane; that output can re-enter deliver_pending, while independent
        # status/poller/reconcile callers can already be running. The atomic
        # conditional PENDING -> DELIVERED update admits exactly one caller to
        # the pane effect; the except path resets only that caller's rows.
        claimed_messages = []
        for message in messages:
            if update_message_status(message.id, MessageStatus.DELIVERED) is not False:
                claimed_messages.append(message)
        messages = claimed_messages
        if not messages:
            return

        # Deliver in contiguous runs of the same sender. With the default
        # num_messages=1 this is a single run; when draining all pending messages
        # (num_messages=0) a batch can span multiple senders, so each run is sent
        # separately to keep PostSendMessageEvent attribution correct — otherwise
        # every message would be attributed to messages[0].sender_id.
        for sender_id, group in groupby(messages, key=lambda m: m.sender_id):
            batch = list(group)
            combined = "\n".join(m.message for m in batch)
            preparations: list[_WakePreparation] = []
            try:
                if idle_bound_delivery:
                    # Re-prove the parked baseline immediately before this batch.
                    # The subscribers are provisional: they can buffer a transition
                    # emitted synchronously by send_input, but become durable only
                    # after that send returns successfully.
                    batch_status = status_monitor.get_status(terminal_id)
                    if batch_status in (
                        TerminalStatus.IDLE,
                        TerminalStatus.COMPLETED,
                    ):
                        for message in batch:
                            preparation = self._prepare_wake_confirmation(
                                terminal_id,
                                message.id,
                                baseline_status=batch_status,
                            )
                            if preparation is not None:
                                preparations.append(preparation)
                with self._callback_completion_delivery_claim(batch):
                    if native_managed:
                        self._send_native_managed_text(terminal_id, combined, managed_identity)
                    elif registry is None:
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
            except TerminalNotFoundError as e:
                for preparation in preparations:
                    self._abort_wake_confirmation(preparation)
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
            except _NativeManagedSendRefused as e:
                for preparation in preparations:
                    self._abort_wake_confirmation(preparation)
                # A permanent generation fence is an absorbing refusal, not
                # a transient no-byte condition. All other pre-write
                # refusals retain the existing retry behavior.
                terminal = (
                    MessageStatus.FAILED
                    if e.reason_code == "generation-fenced"
                    else MessageStatus.PENDING
                )
                for message in batch:
                    update_message_status(message.id, terminal)
                logger.info(
                    f"Native managed send for terminal {terminal_id} refused with "
                    f"zero bytes proven; set {len(batch)} message(s) to {terminal.value}: {e}"
                )
            except TerminalInputRefusedError as e:
                for preparation in preparations:
                    self._abort_wake_confirmation(preparation)
                # The v1 copy-mode-safe write boundary refused before any
                # payload byte (pane busy, identity drift under the lease,
                # or copy mode it could not prove exited).  Zero bytes are
                # proven, so the rows go back to PENDING under the same
                # at-most-once anchor as the native refusal above — the
                # queue/idle-gating contract is preserved, the message is
                # never marked delivered, and no provider submission is
                # ever claimed.  A later cycle re-attempts the same
                # payload; an ambiguous partial write never lands here and
                # keeps the hard-failure mapping below.
                for message in batch:
                    update_message_status(message.id, MessageStatus.PENDING)
                logger.info(
                    f"v1 send for terminal {terminal_id} refused "
                    f"({e.reason_code}) with zero bytes proven; leaving "
                    f"{len(batch)} message(s) pending: {e.detail}"
                )
            except _CallbackCompletionGenerationReplaced as e:
                for preparation in preparations:
                    self._abort_wake_confirmation(preparation)
                for message in batch:
                    update_message_status(message.id, MessageStatus.PENDING)
                logger.info(
                    "Callback completion delivery to %s was fenced before pane "
                    "I/O; leaving %d message(s) pending: %s",
                    terminal_id,
                    len(batch),
                    e,
                )
            except Exception as e:
                for preparation in preparations:
                    self._abort_wake_confirmation(preparation)
                for message in batch:
                    logger.error(f"Failed to deliver message {message.id} to {terminal_id}: {e}")
                    update_message_status(message.id, MessageStatus.FAILED)
            else:
                # The pane send has succeeded. Wake-receipt persistence is a
                # separate post-send observation: if it fails, do not mark the
                # already-sent inbox row FAILED and license duplicate delivery.
                for preparation in preparations:
                    try:
                        self._commit_wake_confirmation(preparation)
                    except Exception:  # noqa: BLE001 - delivery already succeeded
                        self._abort_wake_confirmation(preparation)
                        logger.exception(
                            "could not persist wake confirmation for delivered "
                            "message %s to terminal %s; the message remains delivered",
                            preparation.message_id,
                            terminal_id,
                        )

    @staticmethod
    def _send_native_managed_text(terminal_id: str, text: str, managed_identity: dict) -> None:
        """Send one claimed sender run to a native-TUI managed receiver.

        The run's messages are LF-joined into ONE payload, exactly like the
        unmanaged path's combined send: one composer submission per sender
        run, so a second message is never typed after the first turn has
        already started.  The composer plan inside is read from the bound
        generation's recorded provider_version (no live probe) and types
        literal bytes only — multiline included, no bracketed-paste framing.
        The inbox rows' atomic claim is the at-most-once anchor: claimed
        rows are never re-typed, and terminalized rows survive any bounce
        exactly as on the ordinary path.  ``send_input``'s managed guard
        stays untouched: this path never passes through it, and no
        acknowledgement is inferred beyond the typed ACCEPTED outcome.

        Raises:
            _NativeManagedSendRefused: the send was refused with zero bytes
                proven; the caller resets the batch to PENDING.
            _NativeManagedSendUndeliverable: the send ended neither accepted
                nor refused; the caller terminalizes the batch under the
                existing hard-failure semantics (no blind replay).
        """
        result = control_input_service.deliver_native_inbox_payload(
            terminal_id,
            text=text,
            expected_identity={
                "terminal_id": terminal_id,
                "terminal_generation": managed_identity.get("generation"),
            },
        )
        if result.outcome == ACCEPTED:
            return
        if result.outcome == REFUSED:
            raise _NativeManagedSendRefused(result.reason_code)
        raise _NativeManagedSendUndeliverable(
            f"sender run ended {result.outcome!r} "
            f"({result.reason_code}); not reattempted blindly"
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
