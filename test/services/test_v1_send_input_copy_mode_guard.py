"""cond-0178 F5: copy-mode-safe legacy/v1 ordinary delivery (supervisor tell).

The installed P1: an ordinary supervisor ``conduct tell`` to a legacy-v1
terminal returned success while the target pane sat in tmux copy mode and
the text rested unsubmitted in the provider composer — the payload's Enter
was consumed by the mode.  The v2 control-input path already exits a proven
copy mode and submits; this brings the v1 ordinary write
(:func:`terminal_service.send_input`, the sink the inbox idle-gated
delivery uses) under the same identity-bound pane-arbiter boundary:

- the whole write happens under the exact pane's input lease;
- the F4 copy-mode guard re-proves the bound identity, reads
  ``pane_in_mode``, cancels only the exact proven copy-mode pane, and
  re-proves mode 0 before any payload byte;
- the original payload is then written exactly once, inside the same
  lease;
- anything unproven is the typed zero-byte ``TerminalInputRefusedError``
  before the status arm, never a speculative cancel and never a delivery
  claim — and the inbox maps it back to PENDING, preserving the
  queue/idle-gating contract (a row is never DELIVERED-unwritten and no
  provider submission is ever claimed);
- an ambiguous payload-write failure keeps the existing hard-failure
  mapping and is never auto-replayed.

The v2 surfaces keep their own coverage in ``test_copy_mode_guard.py``;
these tests pin the v1 boundary and its inbox outcome mapping.
"""

import threading
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service, terminal_service
from cli_agent_orchestrator.services.control_input_contract import (
    REASON_COPY_MODE_ACTIVE,
    REASON_IDENTITY_MISMATCH,
    REASON_PANE_BUSY,
    REASON_PANE_DEAD,
)
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.pane_input_arbiter import (
    pane_input_lease,
    reset_pane_input_arbiter,
)

TERMINAL = "92f8d2fb"
PANE = "%75"
WINDOW = "@75"
PANE_PID = 4242
GENERATION = "gen-3"
# Absolute and already canonical, so normalize_server_identity is an
# identity function on it.
SOCKET = "/private/tmp/tmux-501/cao-test"
SESSION = "cao-final-p1-acceptance"
WINDOW_NAME = "supervisor-terra"
MESSAGE = "Copy-mode tell check: reply with exactly CODEX_TELL_COPYMODE_OK."


class FakePaneIdentity:
    """Stands in for tmux's observed pane facts."""

    def __init__(
        self,
        *,
        pane_id=PANE,
        window_id=WINDOW,
        pane_pid=PANE_PID,
        dead=False,
        server_socket_path=SOCKET,
    ):
        self.pane_id = pane_id
        self.window_id = window_id
        self.pane_pid = pane_pid
        self.dead = dead
        self.server_socket_path = server_socket_path


class V1CopyModeFakeTmux:
    """A tmux client that models copy mode as observable pane state.

    Only the three guard primitives exist: identity reads, the mode read,
    and the copy-mode-exit control.  The exit control is recorded on the
    shared ``events`` log so a test can assert it landed before the
    payload and was the only non-payload keystroke.  ``mode_reading`` is
    what the guard observes: True/False are proven readings, None is
    "could not look".
    """

    def __init__(
        self,
        events,
        identities=None,
        *,
        mode_reading=False,
        cancel_accepted=True,
        mode_after_cancel=False,
    ):
        if identities is None:
            identities = [FakePaneIdentity()]
        self._identities = list(identities)
        self.mode_reading = mode_reading
        self.cancel_accepted = cancel_accepted
        self.mode_after_cancel = mode_after_cancel
        self._events = events
        self.mode_queries = 0

    def pane_control_identity(
        self,
        *,
        pane_id=None,
        session_name=None,
        window_name=None,
        deadline_monotonic=None,
    ):
        self._events.append(("identity", pane_id))
        if len(self._identities) > 1:
            return self._identities.pop(0)
        return self._identities[0]

    def pane_in_copy_mode(
        self,
        pane_id,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        assert expected_server_identity == SOCKET
        self.mode_queries += 1
        self._events.append(("mode", pane_id))
        return self.mode_reading

    def send_copy_mode_cancel(
        self,
        pane_id,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        assert expected_server_identity == SOCKET
        self._events.append(("cancel", pane_id))
        if not self.cancel_accepted:
            return False
        self.mode_reading = self.mode_after_cancel
        return True


class RecordingBackend:
    """The backend sink: records each send_keys on the shared events log."""

    def __init__(self, events, *, fail_with=None):
        self._events = events
        self._fail_with = fail_with

    def send_keys(
        self,
        session_name,
        window_name,
        keys,
        *,
        enter_count=1,
        force_bracketed_paste=False,
        submit_delay=0.3,
        pane_id=None,
    ):
        self._events.append(("payload", pane_id, keys, enter_count))
        if self._fail_with is not None:
            raise self._fail_with


def _metadata(**overrides):
    fields = {
        "pane_id": PANE,
        "window_id": WINDOW,
        "session_id": "$7",
        "pane_pid": str(PANE_PID),
        "server_socket_path": SOCKET,
        "generation": GENERATION,
        "provider": "codex",
        "tmux_session": SESSION,
        "tmux_window": WINDOW_NAME,
    }
    fields.update(overrides)
    return fields


def _verified_target():
    return terminal_service.VerifiedPaneTarget(
        PANE,
        SESSION,
        WINDOW_NAME,
        window_id=WINDOW,
        pane_pid=PANE_PID,
        server_socket_path=SOCKET,
    )


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Pane locks and wake-receipt sidecars follow the test state root."""
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", str(tmp_path / "state"))
    from cli_agent_orchestrator.services import wake_receipts

    monkeypatch.setattr(wake_receipts, "WAKE_RECEIPT_DIR", tmp_path / "wake-receipts")
    reset_pane_input_arbiter()
    yield
    reset_pane_input_arbiter()


@pytest.fixture
def v1_harness(monkeypatch):
    """Wire send_input's dependencies: an unmanaged terminal on a fake tmux.

    Returns (events, make_tmux) — ``events`` is the shared ordered log the
    fake tmux client and the recording backend both append to, so a test
    asserts the exact pane-visible sequence.
    """

    events = []

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_launch.managed_control_identity",
        lambda terminal_id: None,
    )
    monkeypatch.setattr(terminal_service, "_get_terminal_metadata_any", lambda tid: _metadata())
    monkeypatch.setattr(
        terminal_service, "verified_pane_target", lambda *a, **k: _verified_target()
    )
    provider_manager = MagicMock()
    provider_manager.get_provider.return_value = None
    monkeypatch.setattr(terminal_service, "provider_manager", provider_manager)
    status_monitor = MagicMock()
    monkeypatch.setattr(terminal_service, "status_monitor", status_monitor)
    monkeypatch.setattr(terminal_service, "inject_memory_context", lambda msg, tid: msg)
    monkeypatch.setattr(terminal_service, "update_last_active", lambda tid: None)

    def make_tmux(**kwargs):
        client = V1CopyModeFakeTmux(events, **kwargs)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.control_input_service._tmux_client",
            lambda: client,
        )
        return client

    def make_backend(**kwargs):
        backend = RecordingBackend(events, **kwargs)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        return backend

    return events, make_tmux, make_backend, status_monitor


@contextmanager
def _pane_held_elsewhere(pane_id=PANE):
    """Hold the pane lease from another thread (the lease is non-reentrant)."""
    entered = threading.Event()
    release = threading.Event()

    def _hold():
        with pane_input_lease(pane_id, holder="competitor"):
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=_hold, daemon=True)
    thread.start()
    assert entered.wait(5)
    try:
        yield
    finally:
        release.set()
        thread.join(5)


class TestV1SendInputCopyModeGuard:
    """G6 mechanism: exact-pane exit, then once-only payload+Enter."""

    def test_copy_mode_exit_then_payload_exactly_once(self, v1_harness):
        events, make_tmux, make_backend, status_monitor = v1_harness
        client = make_tmux(mode_reading=True)
        make_backend()

        assert terminal_service.send_input(TERMINAL, MESSAGE) is True

        # The one non-payload keystroke preceded the one payload write,
        # both aimed at the exact proven pane: identity re-proof, mode
        # read, cancel, identity re-proof, mode re-proof, payload.
        assert [event[0] for event in events] == [
            "identity",
            "mode",
            "cancel",
            "identity",
            "mode",
            "payload",
        ]
        kind, pane_id, keys, enter_count = events[-1]
        assert (pane_id, keys, enter_count) == (PANE, MESSAGE, 1)
        # The exit was proven before the write: two mode reads, one cancel.
        assert client.mode_queries == 2
        status_monitor.notify_input_sent.assert_called_once_with(TERMINAL)
        status_monitor.clear_rolling_buffer.assert_called_once_with(TERMINAL)

    def test_no_copy_mode_means_no_cancel_and_payload_once(self, v1_harness):
        events, make_tmux, make_backend, status_monitor = v1_harness
        client = make_tmux(mode_reading=False)
        make_backend()

        assert terminal_service.send_input(TERMINAL, MESSAGE) is True

        # Scrolling is untouched: a pane not in copy mode receives no
        # non-payload keystroke at all, and the payload is written once.
        assert [event[0] for event in events] == ["identity", "mode", "payload"]
        assert client.mode_queries == 1

    def test_unobservable_mode_is_zero_byte_refusal_before_status_arm(self, v1_harness):
        events, make_tmux, make_backend, status_monitor = v1_harness
        make_tmux(mode_reading=None)
        make_backend()

        with pytest.raises(terminal_service.TerminalInputRefusedError) as excinfo:
            terminal_service.send_input(TERMINAL, MESSAGE)

        assert excinfo.value.reason_code == REASON_COPY_MODE_ACTIVE
        # "Could not look" is never "not in copy mode": no cancel, no
        # payload, and detection was never disturbed for a write that
        # never happened.
        assert [event[0] for event in events] == ["identity", "mode"]
        status_monitor.notify_input_sent.assert_not_called()
        status_monitor.clear_rolling_buffer.assert_not_called()

    def test_rejected_exit_is_zero_byte_refusal_with_one_cancel_only(self, v1_harness):
        events, make_tmux, make_backend, status_monitor = v1_harness
        make_tmux(mode_reading=True, cancel_accepted=False)
        make_backend()

        with pytest.raises(terminal_service.TerminalInputRefusedError) as excinfo:
            terminal_service.send_input(TERMINAL, MESSAGE)

        assert excinfo.value.reason_code == REASON_COPY_MODE_ACTIVE
        assert [event[0] for event in events] == ["identity", "mode", "cancel"]
        status_monitor.notify_input_sent.assert_not_called()

    def test_unconfirmed_exit_never_delivers_and_never_cancels_twice(self, v1_harness):
        events, make_tmux, make_backend, status_monitor = v1_harness
        make_tmux(mode_reading=True, mode_after_cancel=True)
        make_backend()

        with pytest.raises(terminal_service.TerminalInputRefusedError) as excinfo:
            terminal_service.send_input(TERMINAL, MESSAGE)

        assert excinfo.value.reason_code == REASON_COPY_MODE_ACTIVE
        # The re-proof still read 1: one cancel, no payload, no retry.
        assert [event[0] for event in events] == [
            "identity",
            "mode",
            "cancel",
            "identity",
            "mode",
        ]
        status_monitor.notify_input_sent.assert_not_called()

    def test_identity_drift_at_the_guard_aims_no_cancel_and_no_payload(self, v1_harness):
        events, make_tmux, make_backend, status_monitor = v1_harness
        # The under-lease identity read already reports a different root
        # process: the pane the caller proved is not the pane in front of
        # the write now.
        make_tmux(identities=[FakePaneIdentity(pane_pid=9999)], mode_reading=True)
        make_backend()

        with pytest.raises(terminal_service.TerminalInputRefusedError) as excinfo:
            terminal_service.send_input(TERMINAL, MESSAGE)

        assert excinfo.value.reason_code == REASON_IDENTITY_MISMATCH
        # No cancel is ever aimed at a pane whose identity did not
        # re-prove, and no payload is written.
        assert [event[0] for event in events] == ["identity"]
        status_monitor.notify_input_sent.assert_not_called()

    def test_dead_pane_at_the_guard_refuses_without_cancel(self, v1_harness):
        events, make_tmux, make_backend, status_monitor = v1_harness
        make_tmux(identities=[FakePaneIdentity(dead=True)], mode_reading=True)
        make_backend()

        with pytest.raises(terminal_service.TerminalInputRefusedError) as excinfo:
            terminal_service.send_input(TERMINAL, MESSAGE)

        assert excinfo.value.reason_code == REASON_PANE_DEAD
        assert [event[0] for event in events] == ["identity"]
        status_monitor.notify_input_sent.assert_not_called()

    def test_pane_busy_is_typed_zero_byte_refusal(self, v1_harness):
        events, make_tmux, make_backend, status_monitor = v1_harness
        make_tmux(mode_reading=True)
        make_backend()

        with _pane_held_elsewhere():
            with pytest.raises(terminal_service.TerminalInputRefusedError) as excinfo:
                terminal_service.send_input(TERMINAL, MESSAGE)

        # G7 serialization: a competing writer (wheel/tell race) gets the
        # typed busy refusal with zero bytes — never an interleaved write.
        assert excinfo.value.reason_code == REASON_PANE_BUSY
        assert events == []
        status_monitor.notify_input_sent.assert_not_called()

    def test_payload_write_failure_is_ambiguous_not_refusal(self, v1_harness):
        events, make_tmux, make_backend, status_monitor = v1_harness
        make_tmux(mode_reading=True)
        make_backend(fail_with=RuntimeError("tmux write failed part-way"))

        with pytest.raises(RuntimeError):
            terminal_service.send_input(TERMINAL, MESSAGE)

        # The write was attempted: bytes may have landed, so the outcome
        # is the ordinary ambiguous failure — it propagates as-is and is
        # never re-typed by this path.
        assert [event[0] for event in events] == [
            "identity",
            "mode",
            "cancel",
            "identity",
            "mode",
            "payload",
        ]


def _make_message(id=1, receiver_id=TERMINAL, message=MESSAGE, status=MessageStatus.PENDING):
    return InboxMessage(
        id=id,
        sender_id="conduct-tell",
        receiver_id=receiver_id,
        message=message,
        status=status,
        created_at=datetime.now(),
    )


class TestInboxCopyModeOutcomeMapping:
    """G7 queue semantics: typed zero-byte refusal stays pending; never a
    delivered-or-submitted claim; ambiguous writes are never replayed."""

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_copy_mode_refusal_leaves_row_pending_never_delivered(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.send_input.side_effect = terminal_service.TerminalInputRefusedError(
            REASON_COPY_MODE_ACTIVE, "the copy-mode state could not be observed"
        )

        InboxService().deliver_pending(TERMINAL)

        # The optimistic DELIVERED claim is reset to PENDING — the
        # queue/idle-gating contract is preserved and the same payload is
        # re-attempted by a later cycle; nothing is marked FAILED, and no
        # delivery or provider submission is claimed.
        assert mock_update.call_args_list == [
            call(1, MessageStatus.DELIVERED),
            call(1, MessageStatus.PENDING),
        ]

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_ambiguous_write_failure_maps_failed_and_is_never_replayed(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.send_input.side_effect = RuntimeError("tmux write failed part-way")

        InboxService().deliver_pending(TERMINAL)

        # A failure after the write may have started is ambiguous: the
        # existing hard-failure mapping stands and the payload is not
        # re-typed by a later cycle.
        assert mock_update.call_args_list == [
            call(1, MessageStatus.DELIVERED),
            call(1, MessageStatus.FAILED),
        ]

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_successful_guarded_delivery_stays_delivered(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.send_input.return_value = True

        InboxService().deliver_pending(TERMINAL)

        mock_term_svc.send_input.assert_called_once_with(TERMINAL, MESSAGE)
        mock_update.assert_called_once_with(1, MessageStatus.DELIVERED)
