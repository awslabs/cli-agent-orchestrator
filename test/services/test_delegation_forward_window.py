"""The auto-forward never sends a terminal's previous answer (PR #1247 review, P2-1).

The inbox marks a message DELIVERED before it types it, and the receiver's
status stays at its latched IDLE or COMPLETED until the monitor sees the new
turn. In that window every other completion condition holds, and the screen
still shows the previous answer. These tests reproduce the three paths the
review found, with the reviewer's method: the real inbox delivery and edge
rules over a real database, a real StatusMonitor, and one ``evaluate_completion``
call made from inside the submit confirmation, while the worker's status is the
latched IDLE and "PREVIOUS ANSWER" is on its screen. The pane itself is mocked.
"""

import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.clients.database import (
    create_delegation_edge,
    create_inbox_message,
    create_terminal,
    get_inbox_messages,
    list_open_edges_into,
    mark_assign_edges_delivered,
)
from cli_agent_orchestrator.constants import DELEGATION_PICKUP_TIMEOUT_SECONDS
from cli_agent_orchestrator.models.delegation import EdgeSource
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import delegation_callback_service as svc
from cli_agent_orchestrator.services import inbox_service as inbox_module
from cli_agent_orchestrator.services import status_monitor as monitor_module
from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.status_monitor import StatusMonitor

pytestmark = pytest.mark.usefixtures("isolated_memory_db")

TOP, SUP, W1 = "aaaa0001", "aaaa0002", "aaaa0003"


def _output(monitor: StatusMonitor, terminal_id: str, status: TerminalStatus) -> None:
    """One chunk of terminal output landed and was detected as ``status``."""
    with monitor._lock:
        monitor._capture_generation[terminal_id] = (
            monitor._capture_generation.get(terminal_id, 0) + 1
        )
        monitor._output_generation[terminal_id] = monitor._output_generation.get(terminal_id, 0) + 1
    monitor._apply_detection(terminal_id, status)


def _finished_turn(monitor: StatusMonitor, terminal_id: str) -> None:
    """The terminal ran a turn and now sits at a latched IDLE."""
    monitor.notify_input_sent(terminal_id)
    _output(monitor, terminal_id, TerminalStatus.PROCESSING)
    _output(monitor, terminal_id, TerminalStatus.IDLE)


def _forwards_to(receiver_id: str) -> list:
    return [
        m.message
        for m in get_inbox_messages(receiver_id, limit=50)
        if m.message.startswith("[CAO auto-forward]")
    ]


@pytest.fixture
def world(monkeypatch):
    """Terminals TOP -> SUP -> W1, a real monitor, and a mocked pane.

    ``inside`` records ``(terminal_id, forwarded)`` for every evaluation made
    from inside a submit confirmation; ``screens`` is what each pane shows.
    """
    create_terminal(TOP, "cao-s", "top", "claude_code", "supervisor")
    create_terminal(SUP, "cao-s", "sup", "claude_code", "supervisor", caller_id=TOP)
    create_terminal(W1, "cao-s", "w1", "claude_code", "developer", caller_id=SUP)

    monitor = StatusMonitor()
    monkeypatch.setattr(svc, "status_monitor", monitor)
    monkeypatch.setattr(inbox_module, "status_monitor", monitor)
    no_provider = MagicMock()
    no_provider.get_provider.return_value = None
    monkeypatch.setattr(monitor_module, "provider_manager", no_provider)
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", backend)
    monkeypatch.setattr(inbox_module, "INBOX_SUBMIT_CONFIRMATION", True)

    screens = {TOP: "TOP ANSWER", SUP: "SUP PREVIOUS ANSWER", W1: "PREVIOUS ANSWER"}
    terminal = MagicMock()
    terminal.get_output.side_effect = lambda terminal_id, _mode: screens[terminal_id]
    monkeypatch.setattr(svc, "terminal_service", terminal)

    pastes = []

    def dispatch_input(terminal_id, message, **_kwargs):
        pastes.append((terminal_id, message))
        monitor.notify_input_sent(terminal_id)
        return monitor.output_generation(terminal_id)

    paste = MagicMock()
    paste.dispatch_input.side_effect = dispatch_input
    monkeypatch.setattr(inbox_module, "terminal_service", paste)

    inside = []

    def confirm(_self, terminal_id, _message, _boundary, **_kwargs):
        inside.append((terminal_id, svc.evaluate_completion(terminal_id)))
        return True

    monkeypatch.setattr(InboxService, "_confirm_submitted", confirm)
    for terminal_id in (TOP, SUP, W1):
        _finished_turn(monitor, terminal_id)
    return MagicMock(monitor=monitor, screens=screens, inside=inside, pastes=pastes)


def _assign(caller_id: str, worker_id: str) -> None:
    create_delegation_edge(caller_id, worker_id, EdgeSource.ASSIGN)
    mark_assign_edges_delivered(worker_id)


def _send(sender_id: str, receiver_id: str, text: str) -> None:
    """The inbox POST: queue the message, apply the edge rules, deliver."""
    message = create_inbox_message(sender_id, receiver_id, text)
    svc.on_send_message(sender_id, receiver_id, message.id, expect_reply=False)
    inbox_module.inbox_service.deliver_pending(receiver_id)


def _worker_answers(world, terminal_id: str, answer: str) -> None:
    world.screens[terminal_id] = answer
    _output(world.monitor, terminal_id, TerminalStatus.PROCESSING)
    _output(world.monitor, terminal_id, TerminalStatus.COMPLETED)


class TestForwardWaitsForTheDeliveredInput:
    def test_follow_up_after_the_edge_closed(self, world):
        """Path 1: the follow-up opens a new edge, which must not forward the old answer."""
        _send(SUP, W1, "follow-up")

        assert world.pastes == [(W1, "follow-up")]
        assert world.inside == [(W1, 0)]
        assert svc.evaluate_completion(W1) == 0  # delivered and armed, not started yet
        assert _forwards_to(SUP) == []

        _worker_answers(world, W1, "NEW ANSWER")

        assert svc.evaluate_completion(W1) == 1
        (forward,) = _forwards_to(SUP)
        assert forward.endswith("---\nNEW ANSWER")

    def test_follow_up_queued_while_the_assign_edge_is_open(self, world):
        """Path 2: the open assign edge must wait for the follow-up's turn."""
        _assign(SUP, W1)

        _send(SUP, W1, "follow-up")

        assert len(list_open_edges_into(W1)) == 1  # no second edge for the follow-up
        assert world.inside == [(W1, 0)]
        assert svc.evaluate_completion(W1) == 0
        assert _forwards_to(SUP) == []

        _worker_answers(world, W1, "NEW ANSWER")

        assert svc.evaluate_completion(W1) == 1
        (forward,) = _forwards_to(SUP)
        assert forward.endswith("---\nNEW ANSWER")

    def test_nested_forward_does_not_forward_the_parent_early(self, world):
        """Path 3: the forward from W1 into SUP opens the same window on SUP."""
        _assign(TOP, SUP)
        _assign(SUP, W1)
        world.screens[W1] = "W1 RESULT"

        assert svc.evaluate_completion(W1) == 1

        assert world.pastes[0][0] == SUP and world.pastes[0][1].endswith("W1 RESULT")
        assert world.inside == [(SUP, 0)]
        assert svc.evaluate_completion(SUP) == 0
        assert _forwards_to(TOP) == []

        _worker_answers(world, SUP, "SUP NEW ANSWER")

        assert svc.evaluate_completion(SUP) == 1
        (forward,) = _forwards_to(TOP)
        assert forward.endswith("---\nSUP NEW ANSWER")

    def test_message_marked_delivered_but_not_yet_typed(self, world, monkeypatch):
        """Before the paste arms the monitor, only the delivery lock keeps the old answer back.

        The worker did start on its previous input, so ``started_on_last_input``
        alone would allow the forward here.
        """
        _assign(SUP, W1)
        before_paste = []
        typed = world.monitor.notify_input_sent

        def evaluate_then_type(terminal_id, message, **_kwargs):
            before_paste.append(svc.evaluate_completion(terminal_id))
            typed(terminal_id)
            return world.monitor.output_generation(terminal_id)

        monkeypatch.setattr(inbox_module.terminal_service, "dispatch_input", evaluate_then_type)

        _send(SUP, W1, "follow-up")

        assert before_paste == [0]
        assert _forwards_to(SUP) == []


def _later(monkeypatch, seconds: float) -> None:
    """Move the evaluator's clock ``seconds`` past now."""
    now = time.monotonic()
    monkeypatch.setattr(svc, "_monotonic", lambda: now + seconds)


def _pickup_timeouts_to(receiver_id: str) -> list:
    return [m for m in _forwards_to(receiver_id) if "has not started on the last message" in m]


class TestDeliveryNeverPickedUp:
    """A lost paste or swallowed Enter (#78) shows no work; the caller must still hear."""

    @pytest.fixture
    def unconfirmed(self, world, monkeypatch):
        monkeypatch.setattr(InboxService, "_confirm_submitted", lambda *_a, **_kw: False)
        _send(SUP, W1, "follow-up")
        return world

    def test_unconfirmed_delivery_still_arms_the_edge(self, unconfirmed):
        (edge,) = list_open_edges_into(W1)
        assert edge.delivered_at is not None
        assert svc.evaluate_completion(W1) == 0

    def test_late_pickup_forwards_the_new_answer(self, unconfirmed, monkeypatch):
        _later(monkeypatch, DELEGATION_PICKUP_TIMEOUT_SECONDS - 1)
        _worker_answers(unconfirmed, W1, "NEW ANSWER")

        assert svc.evaluate_completion(W1) == 1
        (forward,) = _forwards_to(SUP)
        assert forward.endswith("---\nNEW ANSWER")
        assert _pickup_timeouts_to(SUP) == []

    def test_no_forward_before_the_pickup_timeout(self, unconfirmed, monkeypatch):
        _later(monkeypatch, DELEGATION_PICKUP_TIMEOUT_SECONDS - 1)

        assert svc.evaluate_completion(W1) == 0
        assert _forwards_to(SUP) == []

    def test_sweep_tells_the_caller_after_the_pickup_timeout(self, unconfirmed, monkeypatch):
        _later(monkeypatch, DELEGATION_PICKUP_TIMEOUT_SECONDS + 1)

        svc.sweep()

        (forward,) = _pickup_timeouts_to(SUP)
        assert forward.startswith(f"[CAO auto-forward] Worker {W1} (profile developer) ")
        assert "may not have been picked up" in forward
        assert f'Resend the message: send_message(receiver_id="{W1}"' in forward
        assert forward.endswith("---\nPREVIOUS ANSWER")
        assert list_open_edges_into(W1) == []

    def test_timeout_counts_from_the_last_input(self, unconfirmed, monkeypatch):
        _later(monkeypatch, DELEGATION_PICKUP_TIMEOUT_SECONDS + 1)
        unconfirmed.monitor.notify_input_sent(W1)  # e.g. a resubmission just now
        _later(monkeypatch, 1)

        assert svc.evaluate_completion(W1) == 0


class TestHerdrReportsTheTurnStart:
    """herdr skips the submit confirmation; its own agent_status reports stand in."""

    @pytest.fixture
    def herdr(self, world, monkeypatch):
        backend = MagicMock()
        backend.supports_event_inbox.return_value = True
        monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", backend)
        monkeypatch.setattr(monitor_module, "status_monitor", world.monitor)
        # herdr derives status on demand; here it reads the previous turn's "done".
        monkeypatch.setattr(world.monitor, "get_status", lambda _t: TerminalStatus.COMPLETED)
        _assign(SUP, W1)
        world.monitor.notify_input_sent(W1)  # a follow-up was typed into W1
        return world

    @staticmethod
    def _herdr_reports(agent_status: str, shape: str = "pane_updated") -> None:
        """Feed one herdr event through HerdrInboxService's real event loop.

        ``pane_updated`` is the broadcast frame (pane nested under data);
        ``agent_status_changed`` is the per-pane frame of older herdr releases,
        with the fields at the top of data. The event loop reads both.
        """
        service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=MagicMock())
        service.register_terminal(W1, "pane-1")
        if shape == "pane_updated":
            frame = {
                "event": "pane_updated",
                "data": {"pane": {"pane_id": "pane-1", "agent_status": agent_status}},
            }
        else:
            frame = {
                "event": "pane.agent_status_changed",
                "data": {"agent": "claude", "agent_status": agent_status, "pane_id": "pane-1"},
            }

        async def run():
            reader = asyncio.StreamReader()
            service._reader = reader
            reader.feed_data((json.dumps(frame) + "\n").encode())
            reader.feed_eof()
            with pytest.raises(ConnectionError):
                await service._event_loop()

        asyncio.run(run())

    def test_not_forwarded_until_herdr_reports_work(self, herdr):
        self._herdr_reports("done")

        assert svc.evaluate_completion(W1) == 0
        assert _forwards_to(SUP) == []

    @pytest.mark.parametrize(
        ("agent_status", "shape"),
        [
            ("working", "pane_updated"),
            ("blocked", "pane_updated"),
            ("working", "agent_status_changed"),
        ],
    )
    def test_forwarded_once_herdr_reported_work_after_the_input(self, herdr, agent_status, shape):
        self._herdr_reports(agent_status, shape)
        herdr.screens[W1] = "NEW ANSWER"

        assert svc.evaluate_completion(W1) == 1
        (forward,) = _forwards_to(SUP)
        assert forward.endswith("---\nNEW ANSWER")

    def test_work_reported_before_the_input_does_not_count(self, herdr):
        self._herdr_reports("working")
        herdr.monitor.notify_input_sent(W1)  # another input after that report

        assert svc.evaluate_completion(W1) == 0

    def test_never_reported_work_is_forwarded_after_the_pickup_timeout(self, herdr, monkeypatch):
        self._herdr_reports("done")
        _later(monkeypatch, DELEGATION_PICKUP_TIMEOUT_SECONDS + 1)

        assert svc.evaluate_completion(W1) == 1
        (forward,) = _pickup_timeouts_to(SUP)
        assert forward.endswith("---\nPREVIOUS ANSWER")
