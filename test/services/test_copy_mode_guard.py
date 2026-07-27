"""cond-0178: copy-mode-safe managed delivery at the pane write boundary.

The dashboard wheel path (cond-0131) can leave a managed tmux pane in copy
mode (``pane_in_mode=1``), where a payload Enter is consumed by the mode
instead of submitting — the silent wedge behind cond-0178.  The guard at
the write boundary proves the exact pane's mode under the pane lease,
exits a proven copy mode with the one non-payload keystroke this path may
ever send (``send-keys -X cancel``), re-proves the exit, and only then
delivers the payload exactly once.  Anything it cannot prove is the typed
zero-byte ``copy-mode-active`` refusal — never a speculative cancel, never
a delivery claim over an Enter the mode may have consumed.

These tests cover the fork-side acceptance slice of G1-G5 with stubs:
exact-pane recovery and once-only delivery for the control, sequence,
steer, and inbox surfaces; exit failure and identity mismatch as zero-byte
refusals with no cancel aimed at a new generation; arbiter serialization
of concurrent writers; and no double cancel or payload replay.  The wheel
path itself is untouched by the guard, so "scrolling unchanged" is
asserted here as its contrapositive: a pane not in copy mode receives no
non-payload keystroke at all.
"""

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import control_input_service as service
from cli_agent_orchestrator.services import inbox_service as inbox_module
from cli_agent_orchestrator.services.control_input_contract import (
    ACCEPTED,
    EVENT_OUTCOME_SENT,
    REASON_COPY_MODE_ACTIVE,
    REASON_IDENTITY_MISMATCH,
    REASON_PANE_BUSY,
    REASON_PANE_DEAD,
    REFUSED,
)
from cli_agent_orchestrator.services.control_input_journal import ControlInputJournal
from cli_agent_orchestrator.services.pane_input_arbiter import (
    pane_input_lease,
    reset_pane_input_arbiter,
)

TERMINAL = "9f8e7d6c"
CONTROL = "ctl-copymode-01"
PANE = "%40"
WINDOW = "@40"
PANE_PID = 4242
GENERATION = "gen-7"
# Absolute and already canonical, so normalize_server_identity is an
# identity function on it.
SOCKET = "/private/tmp/tmux-501/cao-test"
TEXT = "/compact"


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
        self.session_name = "cao"
        self.window_name = "worker-1"
        self.bracketed_paste_proven = False
        self.dead = dead
        self.server_socket_path = server_socket_path


class CopyModeFakeTmux:
    """A tmux client that models copy mode as observable pane state.

    Records every keystroke aimed at the pane — payload writes and the
    copy-mode-exit control alike — in ``writes``, so a test asserts the
    exact sequence the pane received.  ``mode_reading`` is what the guard
    observes: True/False are proven readings, None is "could not look".
    Offers no other write path, exactly like the sibling fakes: a fallback
    would fail here with AttributeError, not pass silently.
    """

    def __init__(
        self,
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
        self.writes = []
        self.mode_queries = 0

    def pane_control_identity(
        self,
        *,
        pane_id=None,
        session_name=None,
        window_name=None,
        deadline_monotonic=None,
    ):
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
        self.mode_queries += 1
        return self.mode_reading

    def send_copy_mode_cancel(
        self,
        pane_id,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        self.writes.append(
            {
                "pane_id": pane_id,
                "copy_mode_cancel": True,
                "expected_server_identity": expected_server_identity,
            }
        )
        if not self.cancel_accepted:
            return False
        self.mode_reading = self.mode_after_cancel
        return True

    # Keyword-only and undefaulted, exactly like the real primitives.
    def send_literal_line(
        self,
        pane_id,
        text,
        submit=True,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        self.writes.append(
            {
                "pane_id": pane_id,
                "text": text,
                "submit": submit,
                "expected_server_identity": expected_server_identity,
            }
        )
        return 1

    def send_sequence_key(
        self,
        pane_id,
        key,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        self.writes.append(
            {
                "pane_id": pane_id,
                "key": key,
                "expected_server_identity": expected_server_identity,
            }
        )

    def send_steer_chord(
        self,
        pane_id,
        chord,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        self.writes.append(
            {
                "pane_id": pane_id,
                "chord": chord,
                "expected_server_identity": expected_server_identity,
            }
        )


def _cancel_write():
    return {
        "pane_id": PANE,
        "copy_mode_cancel": True,
        "expected_server_identity": SOCKET,
    }


def _metadata(**overrides):
    fields = {
        "pane_id": PANE,
        "generation": GENERATION,
        "provider": "claude-code",
        "tmux_session": "cao",
        "server_socket_path": SOCKET,
    }
    fields.update(overrides)
    return fields


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Pane locks and the journal both follow the state root, never the host's."""
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", str(tmp_path / "state"))
    reset_pane_input_arbiter()
    service.reset_control_input_journal()
    yield
    reset_pane_input_arbiter()
    service.reset_control_input_journal()


@pytest.fixture
def journal(tmp_path):
    return ControlInputJournal(tmp_path / "journal" / "control-input.sqlite3")


@pytest.fixture
def make_tmux(monkeypatch):
    """Wire a CopyModeFakeTmux as this server's tmux client, unmanaged flavour."""

    def _make(**kwargs):
        client = CopyModeFakeTmux(**kwargs)
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        monkeypatch.setattr(service, "_terminal_metadata", lambda terminal_id: _metadata())
        monkeypatch.setattr(service, "_managed_identity", lambda terminal_id: None)
        return client

    return _make


def _deliver(journal, **overrides):
    kwargs = {"control_id": CONTROL, "text": TEXT, "enter": True}
    kwargs.update(overrides)
    return service.deliver_control_input(TERMINAL, journal=journal, **kwargs)


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


class TestControlPathCopyModeGuard:
    """G1/G2 mechanism: exact-pane exit, then once-only text+Enter."""

    def test_copy_mode_exit_then_delivered_exactly_once(self, make_tmux, journal):
        client = make_tmux(mode_reading=True)

        result = _deliver(journal)

        assert result.outcome == ACCEPTED
        assert result.text_sent is True and result.enter_sent is True
        # One exit control to the exact proven pane, then the one payload
        # write — in that order, and nothing else ever touched the pane.
        assert client.writes == [
            _cancel_write(),
            {
                "pane_id": PANE,
                "text": TEXT,
                "submit": True,
                "expected_server_identity": SOCKET,
            },
        ]
        # Two proven readings: in mode before the exit, out of it after.
        assert client.mode_queries == 2
        record = journal.get(CONTROL)
        assert record.state == "delivered"

    def test_no_copy_mode_means_no_keystroke_but_the_payload(self, make_tmux, journal):
        """Never speculative: a proven-inactive mode gets no exit control.

        This is also the scrolling-preservation pin from the guard's side:
        the only pane effect the guard can produce is the exit control on
        a pane proven in copy mode, so an ordinary delivery is byte-for-
        byte what it was before cond-0178.
        """
        client = make_tmux(mode_reading=False)

        result = _deliver(journal)

        assert result.outcome == ACCEPTED
        assert client.writes == [
            {
                "pane_id": PANE,
                "text": TEXT,
                "submit": True,
                "expected_server_identity": SOCKET,
            }
        ]
        assert client.mode_queries == 1

    def test_unobservable_mode_refuses_then_retry_delivers_once(self, make_tmux, journal):
        """G5: detection that cannot be proven is zero bytes, reattemptable."""
        client = make_tmux(mode_reading=None)

        first = _deliver(journal)

        assert first.outcome == REFUSED
        assert first.reason_code == REASON_COPY_MODE_ACTIVE
        # No payload and no speculative cancel: "could not look" is never
        # read as "in copy mode" either.
        assert client.writes == []

        # The refusal promised re-attemptability; the next observation is
        # healthy and the control lands exactly once across both attempts.
        client.mode_reading = False
        second = _deliver(journal)

        assert second.outcome == ACCEPTED
        assert client.writes == [
            {
                "pane_id": PANE,
                "text": TEXT,
                "submit": True,
                "expected_server_identity": SOCKET,
            }
        ]
        assert journal.get(CONTROL).state == "delivered"

    def test_rejected_exit_is_zero_bytes_and_one_cancel_only(self, make_tmux, journal):
        client = make_tmux(mode_reading=True, cancel_accepted=False)

        result = _deliver(journal)

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_COPY_MODE_ACTIVE
        # The exit was attempted once on the proven pane; no payload, and
        # no second speculative cancel.
        assert client.writes == [_cancel_write()]
        assert journal.get(CONTROL).state == "refused"

    def test_unconfirmed_exit_never_delivers_and_never_cancels_twice(self, make_tmux, journal):
        """G5: tmux acked the exit but the re-proof still reads copy mode."""
        client = make_tmux(mode_reading=True, cancel_accepted=True, mode_after_cancel=True)

        result = _deliver(journal)

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_COPY_MODE_ACTIVE
        assert client.writes == [_cancel_write()]
        # Both mode readings happened; the exit is the only keystroke.
        assert client.mode_queries == 2
        assert journal.get(CONTROL).state == "refused"

    def test_identity_flip_at_the_guard_aims_no_cancel_at_the_new_pane(self, make_tmux, journal):
        """G3: a generation flip found at detection keeps the identity reasons.

        No exit control and no payload reach the incarnation actually in
        front of the writer — the cancel is licensed only by a re-proven
        identity, not by the identity the control was bound to.
        """
        client = make_tmux(
            identities=[
                FakePaneIdentity(),
                FakePaneIdentity(),
                FakePaneIdentity(pane_pid=9999),
            ],
            mode_reading=True,
        )

        result = _deliver(journal)

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_IDENTITY_MISMATCH
        assert client.writes == []
        assert client.mode_queries == 0

    def test_dead_pane_at_the_guard_is_pane_dead_without_cancel(self, make_tmux, journal):
        client = make_tmux(
            identities=[
                FakePaneIdentity(),
                FakePaneIdentity(),
                FakePaneIdentity(dead=True),
            ],
            mode_reading=True,
        )

        result = _deliver(journal)

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PANE_DEAD
        assert client.writes == []
        assert client.mode_queries == 0

    def test_concurrent_writer_is_arbiter_serialized_with_no_cancel(self, make_tmux, journal):
        """G4: the loser of the lease race wrote nothing and cancelled nothing."""
        client = make_tmux(mode_reading=True)

        with _pane_held_elsewhere():
            result = _deliver(journal)

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PANE_BUSY
        assert client.writes == []
        assert client.mode_queries == 0


class TestSequencePathCopyModeGuard:
    """The §5 v3 sequence surface gets the same guard under the same lease."""

    def test_sequence_recovers_and_delivers_events_once(self, make_tmux, journal):
        client = make_tmux(mode_reading=True)

        result = _deliver(
            journal,
            events=[
                {"type": "text", "text": "hello"},
                {"type": "key", "key": "Enter"},
            ],
            text=None,
            enter=None,
        )

        assert result.outcome == ACCEPTED
        # One exit control, then the one fused text+Enter write — each
        # event exactly once, in order.
        assert client.writes == [
            _cancel_write(),
            {
                "pane_id": PANE,
                "text": "hello",
                "submit": True,
                "expected_server_identity": SOCKET,
            },
        ]
        assert [event["outcome"] for event in result.events] == [
            EVENT_OUTCOME_SENT,
            EVENT_OUTCOME_SENT,
        ]

    def test_sequence_exit_failure_is_per_event_zero_bytes(self, make_tmux, journal):
        client = make_tmux(mode_reading=True, cancel_accepted=False)

        result = _deliver(
            journal,
            events=[
                {"type": "text", "text": "hello"},
                {"type": "key", "key": "Enter"},
            ],
            text=None,
            enter=None,
        )

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_COPY_MODE_ACTIVE
        assert client.writes == [_cancel_write()]
        # The v3 answer carries the per-event zero-byte proof.
        assert [event["outcome"] for event in result.events] == ["refused", "refused"]


class _FakeAdapter:
    """A native control adapter that types through the transport, recording calls."""

    def __init__(self, chords=frozenset()):
        self.executed = []
        self._chords = chords

    def steer_chords(self, _version):
        return self._chords

    def execute_composer_plan(self, *, plan, transport, submit, deadline_monotonic):
        self.executed.append((plan, submit))
        transport.send_literal("payload line")
        if submit:
            transport.send_enter()
        return {"lines_typed": 1, "enter_sent": submit}


def _managed_resolved():
    return service.ResolvedControlIdentity(
        terminal_id=TERMINAL,
        terminal_incarnation=None,
        terminal_generation="11111111-2222-3333-4444-555555555555",
        provider="kimi_cli",
        native_session_id="ns-1",
        execution_mode="native_tui",
        session_name="cao-worker",
        provider_process_id="4321@marker",
        provider_version="0.29.2",
        pane_id=PANE,
        window_id=WINDOW,
        pane_pid=PANE_PID,
        pane_dead=False,
        managed=True,
        recorded_pane_id=PANE,
        bound_server_socket_path=SOCKET,
        observed_server_socket_path=SOCKET,
    )


class TestManagedNativeCopyModeGuard:
    """The managed adapter path: guard, then the proven composer plan, once."""

    def _wire(self, monkeypatch, client, adapter, plan):
        resolved = _managed_resolved()
        monkeypatch.setattr(service, "resolve_control_identity", lambda tid: resolved)
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        monkeypatch.setattr(
            service,
            "_native_composer_preflight",
            lambda *a, **k: (adapter, plan, None),
        )
        from cli_agent_orchestrator.services import managed_launch_v2

        monkeypatch.setattr(managed_launch_v2, "native_control_adapter", lambda provider: adapter)
        return resolved

    def test_managed_native_recovers_and_types_the_plan_once(self, make_tmux, journal, monkeypatch):
        client = make_tmux(mode_reading=True)
        adapter = _FakeAdapter()
        self._wire(monkeypatch, client, adapter, {"deliverable": True})

        result = _deliver(journal, text="hello")

        assert result.outcome == ACCEPTED
        assert adapter.executed == [({"deliverable": True}, True)]
        assert client.writes == [
            _cancel_write(),
            {
                "pane_id": PANE,
                "text": "payload line",
                "submit": False,
                "expected_server_identity": SOCKET,
            },
            {
                "pane_id": PANE,
                "text": "",
                "submit": True,
                "expected_server_identity": SOCKET,
            },
        ]

    def test_steer_chord_recovers_then_text_and_chord_once(self, make_tmux, journal, monkeypatch):
        """The steer surface: the exit is the only non-payload keystroke added."""
        client = make_tmux(mode_reading=True)
        adapter = _FakeAdapter(chords=frozenset({"C-s"}))
        self._wire(monkeypatch, client, adapter, {"deliverable": True})

        # A chord control sets enter=false: the chord replaces Enter as the
        # submit/steer effect.
        result = _deliver(journal, text="urgent steer", enter=False, chord="C-s")

        assert result.outcome == ACCEPTED
        assert result.chord == "C-s" and result.chord_sent is True
        assert client.writes == [
            _cancel_write(),
            {
                "pane_id": PANE,
                "text": "payload line",
                "submit": False,
                "expected_server_identity": SOCKET,
            },
            {
                "pane_id": PANE,
                "chord": "C-s",
                "expected_server_identity": SOCKET,
            },
        ]

    def test_managed_native_exit_failure_is_zero_bytes(self, make_tmux, journal, monkeypatch):
        client = make_tmux(mode_reading=True, cancel_accepted=False)
        adapter = _FakeAdapter()
        self._wire(monkeypatch, client, adapter, {"deliverable": True})

        result = _deliver(journal, text="hello")

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_COPY_MODE_ACTIVE
        assert adapter.executed == []
        assert client.writes == [_cancel_write()]


class TestInboxPayloadCopyModeGuard:
    """G1's inbox half: `conduct tell` / worker callbacks to a native receiver."""

    def _wire(self, monkeypatch, client, adapter, plan, turn_status=TerminalStatus.IDLE):
        from cli_agent_orchestrator.services import managed_launch_v2

        resolved = _managed_resolved()
        # Each test models a fresh server lifetime unless it deliberately
        # performs multiple deliveries itself.
        with service._native_kimi_dispatch_guard_lock:
            service._native_kimi_dispatch_times.clear()
        monkeypatch.setattr(service, "resolve_control_identity", lambda tid: resolved)
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        monkeypatch.setattr(
            service,
            "_native_composer_preflight",
            lambda *a, **k: (adapter, plan, None),
        )
        monkeypatch.setattr(
            managed_launch_v2, "_observe_turn_state", lambda provider, **kwargs: turn_status
        )
        return resolved

    def test_inbox_payload_recovers_and_delivers_once(self, make_tmux, monkeypatch):
        client = make_tmux(mode_reading=True)
        adapter = _FakeAdapter()
        self._wire(monkeypatch, client, adapter, {"deliverable": True})

        result = service.deliver_native_inbox_payload(TERMINAL, text="hello")

        assert result.outcome == ACCEPTED
        assert result.enter_sent is True
        assert adapter.executed == [({"deliverable": True}, True)]
        # One exit control on the exact pane, then the payload, once.
        assert client.writes == [
            _cancel_write(),
            {
                "pane_id": PANE,
                "text": "payload line",
                "submit": False,
                "expected_server_identity": SOCKET,
            },
            {
                "pane_id": PANE,
                "text": "",
                "submit": True,
                "expected_server_identity": SOCKET,
            },
        ]

    def test_inbox_payload_exit_failure_is_zero_bytes_refused(self, make_tmux, monkeypatch):
        client = make_tmux(mode_reading=True, cancel_accepted=False)
        adapter = _FakeAdapter()
        self._wire(monkeypatch, client, adapter, {"deliverable": True})

        result = service.deliver_native_inbox_payload(TERMINAL, text="hello")

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_COPY_MODE_ACTIVE
        assert result.chunks_sent == 0
        assert adapter.executed == []
        assert client.writes == [_cancel_write()]

    def test_inbox_payload_identity_flip_aims_no_cancel(self, make_tmux, monkeypatch):
        """Exit failure by identity: the new generation gets no exit, no bytes."""
        client = make_tmux(
            identities=[FakePaneIdentity(), FakePaneIdentity(pane_pid=9999)],
            mode_reading=True,
        )
        adapter = _FakeAdapter()
        self._wire(monkeypatch, client, adapter, {"deliverable": True})

        result = service.deliver_native_inbox_payload(TERMINAL, text="hello")

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_IDENTITY_MISMATCH
        assert client.writes == []
        assert adapter.executed == []

    def test_copy_mode_refusal_is_the_zero_byte_queue_signal(self, monkeypatch):
        """The inbox lane's honest queue: a copy-mode-active REFUSED resets the
        claimed batch to PENDING rather than terminalizing it — the refusal
        proves zero bytes, so the same payload is delivered by a later cycle."""
        monkeypatch.setattr(
            service,
            "deliver_native_inbox_payload",
            lambda tid, **kwargs: service.NativePayloadResult(
                REFUSED, REASON_COPY_MODE_ACTIVE, "the exit could not be confirmed"
            ),
        )

        with pytest.raises(inbox_module._NativeManagedSendRefused):
            inbox_module.InboxService._send_native_managed_text(
                TERMINAL, "hello", {"generation": GENERATION}
            )
