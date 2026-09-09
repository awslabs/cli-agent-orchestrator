"""Delivers queued inbox messages when terminals become ready.

Consumer: terminal.{id}.status
"""

import asyncio
import logging
import threading
from contextlib import contextmanager
from itertools import groupby
from typing import Dict, Iterator, Optional, Tuple

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

# terminal_id -> list of (attempt_token, boundary_generation) for dispatches
# not yet confirmed by a real status transition. The lock above serializes
# deliver_pending calls but does not stop a queued caller from proceeding
# once it is its turn: status_monitor.get_status() still returns the cached
# IDLE/COMPLETED from before the first send, because notify_input_sent()
# only arms the next PROCESSING detection, it does not flip the cached
# status itself, and the real detection needs actual terminal output to run.
# A second caller that only checks status would see the same stale ready
# value and dispatch its own message into a terminal that has not started
# working on the first one yet (reviewer-reproduced on #709: two IDLE checks
# and two sends in one cycle). Each entry closes that window for one
# dispatch attempt: it is confirmed once send_input returns and cleared only
# once InboxService.run() observes a status event whose generation is
# strictly newer than the one recorded at confirmation.
#
# ``boundary_generation`` is ``None`` from the moment an attempt is armed
# (before send_input is even called) until send_input returns. While it is
# None, no event can confirm the attempt, however new its generation: an
# event consumed during that window is about output that predates this
# dispatch, since the input has not yet crossed the backend dispatch
# boundary, and treating it as a confirmation clears the marker before the
# real post-dispatch transition ever arrives (#709 ninth review round;
# arming with an already-known pre-dispatch generation, as an earlier
# version did, let exactly that event confirm a dispatch that had not
# happened yet). Once send_input returns, the entry is resolved against
# status_monitor's own pre-write snapshot (recorded by notify_input_sent,
# after send_input's prep and immediately before the backend write), not
# against a snapshot taken at arm time before send_input was even called: a
# transition observed during PREP (get_terminal_metadata,
# inject_memory_context) predates the write and is not evidence of a
# response to it, so comparing against an arm-time snapshot could mistake
# leftover prep-window activity for confirmation (#709 twelfth review
# round). If nothing has moved since the pre-write snapshot, the boundary
# becomes the current generation and only a genuinely later transition can
# confirm it; if the generation already moved since the pre-write snapshot
# (a real transition landed at or after the write, including the metadata
# write send_input does after send_keys), that transition already is the
# confirmation and the entry is dropped immediately, since nothing strictly
# newer than an event that already happened will ever arrive (#709 eleventh
# review round).
#
# Attempts are tracked per-token, not as a single slot, because
# ``deliver_pending`` dispatches multiple sender groups sequentially under
# one terminal lock (num_messages > 1): if group 1's send succeeds and group
# 2's then fails, group 2's own token is the only one its abort may remove.
# A single shared slot would have group 2's arm overwrite group 1's already-
# confirmed entry, so group 2's abort would strand (remove all trace of)
# a dispatch that did reach the terminal and is still owed a confirmation
# (#709 tenth review round).
#
# A prior version also expired an entry on elapsed time alone, but a
# provider is not contractually bound to emit any output within a fixed
# window, so a slow or silent start left the cached status unchanged and let
# the next caller (in particular the five-second OpenCode poller) dispatch a
# second message into the same unconfirmed cycle (#709 fifth review round).
# An entry now only ever clears on a genuine transition; a terminal whose
# provider truly never produces another status event again holds its
# remaining PENDING messages rather than risk another interleaved send, the
# same terminal already received the first message that set the marker.
_dispatch_active_guard = threading.Lock()
_dispatch_active: Dict[str, list] = {}
_dispatch_attempt_counter = 0


def _next_dispatch_token() -> int:
    """Caller must hold ``_dispatch_active_guard``."""
    global _dispatch_attempt_counter
    _dispatch_attempt_counter += 1
    return _dispatch_attempt_counter


def _clear_dispatch_active(terminal_id: str, event_generation: int) -> None:
    """Drop every outstanding attempt this event postdates. An attempt whose
    boundary is still ``None`` (not yet confirmed) or whose boundary this
    event has not exceeded proves nothing about what happened after its own
    dispatch and stays outstanding (#709)."""
    with _dispatch_active_guard:
        attempts = _dispatch_active.get(terminal_id)
        if not attempts:
            return
        remaining = [
            (token, boundary)
            for token, boundary in attempts
            if boundary is None or event_generation <= boundary
        ]
        if remaining:
            _dispatch_active[terminal_id] = remaining
        else:
            del _dispatch_active[terminal_id]


def _is_dispatch_active(terminal_id: str) -> bool:
    """True while at least one dispatch for this terminal has not yet been
    confirmed by a later status event. Elapsed time alone never clears an
    attempt: only a genuinely newer transition (see _clear_dispatch_active)
    proves the cycle advanced (#709 fifth review round)."""
    with _dispatch_active_guard:
        return bool(_dispatch_active.get(terminal_id))


def _mark_dispatch_active(terminal_id: str, generation: Optional[int] = None) -> None:
    """Arm and immediately confirm one attempt at ``generation`` (a fresh
    read when omitted). A single-step convenience for callers that already
    know the confirming generation up front; ``deliver_pending`` itself uses
    the two-step ``_arm_dispatch_active``/``_confirm_dispatch_active`` below
    so an attempt has no confirmable boundary until send_input returns."""
    if generation is None:
        generation = status_monitor.get_status_generation(terminal_id)
    with _dispatch_active_guard:
        token = _next_dispatch_token()
        _dispatch_active.setdefault(terminal_id, []).append((token, generation))


def _arm_dispatch_active(terminal_id: str) -> int:
    """Record a new dispatch attempt with no confirmable boundary yet, and
    return its token. The marker exists for the whole dispatch window so no
    status event confirming it can be consumed before the marker does (#709
    eighth review round). The token lets a later confirm/abort affect only
    this attempt, never a sibling sender group's (#709 tenth review round).

    The confirming boundary itself is not read here: a snapshot taken before
    send_input is even called cannot tell a transition that happened during
    send_input's own prep (get_terminal_metadata, inject_memory_context) from
    one that happened at or after the actual backend write, and the two mean
    opposite things (#709 twelfth review round). See _confirm_dispatch_active,
    which sources the boundary from status_monitor.get_pre_write_generation
    instead: a snapshot notify_input_sent takes itself, after send_input's
    prep and immediately before the write."""
    with _dispatch_active_guard:
        token = _next_dispatch_token()
        _dispatch_active.setdefault(terminal_id, []).append((token, None))
        return token


def _confirm_dispatch_active(terminal_id: str, token: int) -> None:
    """Resolve ``token``'s attempt once send_input has returned, i.e. once
    input has actually crossed the backend dispatch boundary.

    The comparison boundary is status_monitor.get_pre_write_generation(
    terminal_id): the transition counter as notify_input_sent saw it, which
    send_input calls after its own prep (get_terminal_metadata,
    inject_memory_context) and immediately before the backend write
    (send_keys). A transition during that prep is already folded into this
    value and cannot be mistaken for a response to a write that had not
    happened yet; only a transition at or after the actual write can exceed
    it (#709 twelfth review round: a snapshot taken before send_input was
    even called, as an earlier version did, could not tell the two apart).

    Reads the current generation inside the same critical section that
    decides the outcome, closing the snapshot-to-confirmation gap a
    separate pre-read would leave open. Two cases:

    * The generation is unchanged since the pre-write snapshot: no
      transition has been observed since the write. Arm a boundary at the
      current value, same as before, so only a strictly newer future event
      can clear it.
    * The generation has already advanced: a genuine post-write transition
      landed and was (or will be) consumed by InboxService.run() while this
      attempt's boundary was still None and could not confirm it. That
      transition already reflects what happened after this dispatch, so
      there is nothing left for a future event to confirm; arming a
      boundary now would need something strictly newer than an event that
      already happened and will not repeat, stranding the marker forever
      (#709 eleventh review round). Resolve the attempt immediately instead.

    If notify_input_sent was never observed for this terminal (defensive;
    should not happen on the success path, since send_input always calls it
    before send_keys), falls back to a fresh read as the boundary, the same
    as the unchanged-generation case above.

    A no-op if ``token``'s entry was already removed by an abort (should
    not happen: confirm and abort are mutually exclusive outcomes of the
    same attempt) or by a status reset."""
    with _dispatch_active_guard:
        attempts = _dispatch_active.get(terminal_id)
        if not attempts:
            return
        current_generation = status_monitor.get_status_generation(terminal_id)
        pre_write_generation = status_monitor.get_pre_write_generation(terminal_id)
        if pre_write_generation is not None and current_generation > pre_write_generation:
            remaining = [(t, b) for t, b in attempts if t != token]
        else:
            remaining = [(t, current_generation) if t == token else (t, b) for t, b in attempts]
        if remaining:
            _dispatch_active[terminal_id] = remaining
        else:
            del _dispatch_active[terminal_id]


def _abort_dispatch_active(terminal_id: str, token: int) -> None:
    """Drop only ``token``'s attempt, after a send that never reached the
    terminal (#709 eighth review round). Never removes a sibling sender
    group's still-outstanding attempt (#709 tenth review round): nothing
    happened at the terminal for THIS attempt, so there is nothing for
    ``_clear_dispatch_active``'s generation check to confirm, but another
    attempt for the same terminal may already be armed or confirmed and
    still owed one. Safe to call when ``token``'s entry is no longer
    present."""
    with _dispatch_active_guard:
        attempts = _dispatch_active.get(terminal_id)
        if not attempts:
            return
        remaining = [(t, b) for t, b in attempts if t != token]
        if remaining:
            _dispatch_active[terminal_id] = remaining
        else:
            del _dispatch_active[terminal_id]


def _drop_dispatch_active_on_status_reset(terminal_id: str) -> None:
    """status_monitor teardown hook: fires from clear_terminal/reset_buffer,
    i.e. exactly when ``_status_generations`` is popped for this terminal and
    its counter restarts from 0. Every attempt recorded before that restart
    is no longer comparable to anything the terminal can publish afterward:
    every post-reset event carries a generation at or below the old
    snapshot, so _clear_dispatch_active's check could never fire and the
    attempt would survive forever, silently and permanently stranding that
    terminal's inbox (reviewer-reported finding on #709). Drop every
    outstanding attempt unconditionally rather than re-deriving anything: a
    reset means whatever they were confirming no longer applies. This also
    reaps the entries when the terminal is torn down via clear_terminal,
    which nothing previously did, closing the same-cause slow leak the
    reviewer flagged alongside the stranding bug."""
    with _dispatch_active_guard:
        _dispatch_active.pop(terminal_id, None)


status_monitor.register_teardown_hook(_drop_dispatch_active_on_status_reset)


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
        if terminal_id not in _delivery_locks:
            _delivery_locks[terminal_id] = (threading.Lock(), 0)
        lock, count = _delivery_locks[terminal_id]
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
                # StatusMonitor is the only production publisher of this topic
                # and always includes a generation; ApprovalBridge only
                # subscribes to it. Default to 0 rather than raise so a
                # differently-shaped event (e.g. from a test harness) can't
                # take this consumer down: that never clears a real dispatch
                # marker early, it just leaves this one event unable to
                # confirm one (see _clear_dispatch_active).
                event_generation = event["data"].get("generation", 0)
                terminal_id = terminal_id_from_topic(event["topic"])
                # A published status event means _apply_detection ran a genuine
                # transition for this terminal (it dedupes no-op repeats), so a
                # cached value a concurrent deliver_pending call saw BEFORE this
                # transition is now stale. Clear the busy marker before deciding
                # whether to deliver, so a real ready event right behind a
                # dispatch is never starved by its own dispatch (#709), but only
                # when the event is newer than the dispatch it would confirm: this
                # queue can already hold an older event at dispatch time, and
                # clearing on that one would reopen the same window from the other
                # side (#709 third review round).
                _clear_dispatch_active(terminal_id, event_generation)
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

        Safe to call from any thread: the whole read→mark→send sequence is
        serialized per terminal (see _terminal_delivery_lock for why that is
        load-bearing).
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
                # Arm BEFORE dispatch (#709 eighth review round), not after.
                # Arming after send_input returns left a check-then-mark window: a
                # fast completion event could be published and consumed by
                # InboxService.run() while this call was still between its
                # post-dispatch read and the arm, find no marker to clear (none
                # existed yet), and then this call would install one for a
                # generation that event had already confirmed: nothing left to
                # arrive would ever clear it, coalescing every later message to
                # this terminal forever (reviewer-reproduced finding on #709).
                # Arming here, before send_input is even called, closes the
                # window: no status event caused by this dispatch can exist
                # before the marker does. The token identifies this sender
                # group's own attempt so a later abort can never remove a
                # sibling group's still-outstanding one (#709 tenth review
                # round). _confirm_dispatch_active compares against the
                # pre-write generation status_monitor recorded when send_input
                # called notify_input_sent (after its own prep, immediately
                # before the backend write), not against a snapshot taken here
                # before send_input was even called: a genuine completion can
                # land while this attempt's boundary is still None (during the
                # call itself, including the metadata write terminal_service.
                # send_input does after send_keys), and a boundary set to that
                # already-elapsed generation would need a strictly newer
                # future event to clear it that will never arrive, stranding
                # the marker (#709 eleventh review round), while a
                # transition observed during send_input's PREP, before the
                # write, is not evidence of a response to this dispatch at all
                # and must not confirm it either (#709 twelfth review round).
                # If the send never reaches the terminal (see the except
                # branches below), _abort_dispatch_active drops this attempt:
                # nothing will ever confirm a dispatch that did not happen.
                token = _arm_dispatch_active(terminal_id)
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
                    _confirm_dispatch_active(terminal_id, token)
                    logger.info(f"Delivered {len(batch)} message(s) to terminal {terminal_id}")
                except TerminalNotFoundError as e:
                    # Pane not resolvable yet (e.g. a herdr pane that isn't mapped
                    # for this window). Treat as transient: reset to PENDING so the
                    # reconcile sweep retries rather than marking FAILED. These were
                    # optimistically set to DELIVERED above. (#271 semantic.)
                    _abort_dispatch_active(terminal_id, token)
                    for message in batch:
                        update_message_status(message.id, MessageStatus.PENDING)
                    logger.warning(
                        f"Pane not resolvable for terminal {terminal_id}; leaving "
                        f"{len(batch)} message(s) pending for retry: {e}"
                    )
                except Exception as e:
                    _abort_dispatch_active(terminal_id, token)
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
