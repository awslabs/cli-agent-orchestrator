"""The delegation service keeps ``cao.waiting`` current (design D38).

Spec ``herdr-app-role-labels``, "A terminal that waits on workers carries
``cao.waiting``": every edge that opens or closes refreshes its caller's label
through the backend, the sweep repairs every managed terminal, and a failed or
absent backend never changes what the edge change does.
"""

import logging
import threading
import time
from types import SimpleNamespace
from typing import Dict, List, Mapping, Tuple
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.backends import registry
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
from cli_agent_orchestrator.clients.database import (
    Base,
    close_delegation_edge,
    count_open_edges_by_caller,
    create_delegation_edge,
    list_open_edges_into,
    list_open_edges_out_of,
    mark_assign_edges_delivered,
    mark_reply_in_flight,
)
from cli_agent_orchestrator.models.delegation import EdgeSource, EdgeState
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import delegation_callback_service as svc
from cli_agent_orchestrator.services.inbox_service import InboxService

MOD = "cli_agent_orchestrator.services.delegation_callback_service"


class RecordingBackend:
    """Stands in for the registered backend: records each ``set_waiting_counts`` call.

    The service writes on a background thread, so reading ``calls`` first waits
    for the writes that were queued.
    """

    def __init__(self) -> None:
        self._calls: List[Tuple[Dict[str, int], bool]] = []
        self.error: Exception | None = None

    @property
    def calls(self) -> List[Tuple[Dict[str, int], bool]]:
        svc.flush_waiting_labels()
        return self._calls

    def set_waiting_counts(self, counts: Mapping[str, int], *, complete: bool = False) -> None:
        if self.error is not None:
            raise self.error
        self._calls.append((dict(counts), complete))


@pytest.fixture(autouse=True)
def publisher(monkeypatch):
    """A fresh background writer for each test, stopped at its end."""
    fresh = svc._WaitingLabelPublisher()
    monkeypatch.setattr(svc, "_publisher", fresh)
    yield fresh
    fresh.stop()


@pytest.fixture
def db():
    # One connection shared by every thread: the label worker reads the database too.
    engine = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)
    with patch("cli_agent_orchestrator.clients.database.SessionLocal", session):
        yield


@pytest.fixture
def backend():
    recording = RecordingBackend()
    registry.set_backend(recording)  # type: ignore[arg-type]
    return recording


@pytest.fixture
def env():
    """The I/O an evaluation touches; the edge claim stays real."""
    sent = []

    def claim_and_record(edge_id, state, sender_id, receiver_id, message, **_kwargs):
        if not close_delegation_edge(edge_id, state):
            return False
        sent.append((sender_id, receiver_id, message))
        return True

    with (
        patch(f"{MOD}.status_monitor") as sm,
        patch(f"{MOD}.terminal_service") as ts,
        patch(f"{MOD}.get_pending_messages", return_value=[]),
        patch(f"{MOD}.get_terminal_metadata", return_value={"agent_profile": "developer"}),
        patch(f"{MOD}.close_delegation_edge_with_message", side_effect=claim_and_record),
        patch(f"{MOD}.inbox_service"),
    ):
        sm.get_status.return_value = TerminalStatus.IDLE
        sm.started_on_last_input.return_value = True
        ts.get_output.return_value = "final answer"
        yield sent


def _armed(caller, worker):
    create_delegation_edge(caller, worker, EdgeSource.ASSIGN)
    mark_assign_edges_delivered(worker)


def _reply_status(status):
    """The stored reply message reads ``status`` when the edge rule looks it up."""
    return patch(f"{MOD}.get_inbox_message", return_value=SimpleNamespace(status=status))


# --- the registry and the database give the service its two inputs ---


class TestPeekBackend:
    def test_returns_none_and_creates_nothing_when_no_backend_was_set(self):
        assert registry._backend is None

        assert registry.peek_backend() is None
        assert registry._backend is None

    def test_returns_the_backend_that_was_set(self, backend):
        assert registry.peek_backend() is backend


class TestCountOpenEdgesByCaller:
    def test_counts_open_edges_per_caller_and_ignores_closed_ones(self, db):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        create_delegation_edge("sup", "w2", EdgeSource.ASSIGN)
        create_delegation_edge("w1", "w3", EdgeSource.SEND_MESSAGE)
        closed = create_delegation_edge("other", "w4", EdgeSource.ASSIGN)
        close_delegation_edge(closed.id, EdgeState.REPLIED)

        assert count_open_edges_by_caller() == {"sup": 2, "w1": 1}

    def test_is_empty_without_edges(self, db):
        assert count_open_edges_by_caller() == {}


# --- refresh_waiting_labels ---


class TestRefresh:
    def test_passes_the_open_outbound_count_of_each_caller(self, db, backend):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        create_delegation_edge("sup", "w2", EdgeSource.ASSIGN)
        create_delegation_edge("other", "w3", EdgeSource.ASSIGN)

        svc.refresh_waiting_labels(["sup", "other"])

        assert backend.calls == [({"sup": 2, "other": 1}, False)]

    def test_a_caller_with_no_open_edge_gets_a_zero(self, db, backend):
        svc.refresh_waiting_labels(["sup"])

        assert backend.calls == [({"sup": 0}, False)]

    def test_counts_each_caller_once_however_often_it_is_named(self, db, backend):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)

        svc.refresh_waiting_labels(iter(["sup", "sup", ""]))

        assert backend.calls == [({"sup": 1}, False)]

    def test_no_callers_makes_no_call(self, db, backend):
        svc.refresh_waiting_labels([])

        assert backend.calls == []

    def test_no_backend_is_not_an_error(self, db):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)

        svc.refresh_waiting_labels(["sup"])  # nothing registered: must not raise

    def test_a_failing_backend_is_logged_and_swallowed(self, db, backend, caplog):
        backend.error = RuntimeError("core went away")

        with caplog.at_level(logging.WARNING, logger=MOD):
            svc.refresh_waiting_labels(["sup"])
            svc.flush_waiting_labels()

        assert any("core went away" in r.getMessage() for r in caplog.records)

    def test_a_failing_database_is_logged_and_swallowed(self, backend, caplog):
        with (
            patch(f"{MOD}.list_open_edges_out_of", side_effect=RuntimeError("db locked")),
            caplog.at_level(logging.WARNING, logger=MOD),
        ):
            svc.refresh_waiting_labels(["sup"])
            svc.flush_waiting_labels()

        assert backend.calls == []
        assert any("db locked" in r.getMessage() for r in caplog.records)


# --- each way an edge opens or closes refreshes its caller ---


class TestEdgesOpenAndClose:
    @patch(f"{MOD}.get_terminal_metadata", return_value={"caller_id": "sup"})
    def test_send_message_to_a_child_opens_an_edge_and_publishes_one(self, _meta, db, backend):
        svc.on_send_message(sender_id="sup", receiver_id="w1", message_id=7, expect_reply=False)

        assert backend.calls == [({"sup": 1}, False)]

    @patch(f"{MOD}.get_terminal_metadata", return_value={"caller_id": "someone-else"})
    def test_expect_reply_to_another_terminal_publishes_for_the_sender(self, _meta, db, backend):
        svc.on_send_message(sender_id="w1", receiver_id="w3", message_id=9, expect_reply=True)

        assert backend.calls == [({"w1": 1}, False)]

    def test_a_reply_still_in_flight_leaves_the_edge_and_the_count_alone(self, db, backend):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)

        with _reply_status(MessageStatus.PENDING):
            svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=1, expect_reply=False)

        assert [e.worker_id for e in list_open_edges_out_of("sup")] == ["w1"]
        assert backend.calls == []

    def test_a_delivered_reply_closes_the_edge_and_publishes_the_lower_count(self, db, backend):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        create_delegation_edge("sup", "w2", EdgeSource.ASSIGN)

        with _reply_status(MessageStatus.DELIVERED):
            svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=1, expect_reply=False)

        assert backend.calls == [({"sup": 1}, False)]

    def test_the_last_delivered_reply_publishes_zero(self, db, backend):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)

        with _reply_status(MessageStatus.DELIVERED):
            svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=1, expect_reply=False)

        assert backend.calls == [({"sup": 0}, False)]

    def test_the_inbox_settling_a_delivered_reply_publishes_the_callers_count(self, db, backend):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        create_delegation_edge("sup", "w2", EdgeSource.ASSIGN)
        mark_reply_in_flight(edge.id, 5)

        InboxService._settle_reply_edges("sup", [SimpleNamespace(id=5)])  # type: ignore[list-item]

        assert backend.calls == [({"sup": 1}, False)]

    def test_a_reply_the_inbox_did_not_settle_publishes_nothing(self, db, backend):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        mark_reply_in_flight(edge.id, 5)

        InboxService._settle_reply_edges("sup", [SimpleNamespace(id=6)])  # type: ignore[list-item]

        assert backend.calls == []

    @patch(f"{MOD}.get_terminal_metadata", return_value={"caller_id": "someone-else"})
    def test_a_message_that_opens_no_edge_publishes_nothing(self, _meta, db, backend):
        svc.on_send_message(sender_id="w1", receiver_id="w3", message_id=9, expect_reply=False)

        assert backend.calls == []

    @patch(f"{MOD}.get_terminal_metadata", return_value={"caller_id": "sup"})
    def test_a_follow_up_on_an_open_edge_publishes_nothing(self, _meta, db, backend):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)

        svc.on_send_message(sender_id="sup", receiver_id="w1", message_id=8, expect_reply=False)

        assert backend.calls == []

    def test_an_auto_forward_publishes_for_the_caller(self, db, env, backend):
        _armed("sup", "w1")

        assert svc.evaluate_completion("w1") == 1

        assert backend.calls == [({"sup": 0}, False)]

    def test_an_auto_forward_to_two_callers_publishes_both_in_one_call(self, db, env, backend):
        _armed("sup", "w1")
        _armed("other", "w1")

        assert svc.evaluate_completion("w1") == 2

        assert backend.calls == [({"sup": 0, "other": 0}, False)]

    def test_nothing_forwarded_publishes_nothing(self, db, env, backend):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)  # not armed: no forward

        assert svc.evaluate_completion("w1") == 0

        assert backend.calls == []

    def test_a_worker_that_ended_publishes_for_the_caller_it_notified(self, db, env, backend):
        _armed("sup", "w1")

        svc.handle_worker_ended("w1", "was deleted")

        assert backend.calls == [({"sup": 0}, False)]

    def test_a_worker_that_ended_publishes_for_every_caller_waiting_on_it(self, db, env, backend):
        _armed("sup", "w1")
        _armed("other", "w1")
        create_delegation_edge("sup", "w2", EdgeSource.ASSIGN)

        svc.handle_worker_ended("w1", "was deleted")

        assert backend.calls == [({"sup": 1, "other": 0}, False)]

    def test_a_caller_that_ended_has_no_label_to_fix(self, db, env, backend):
        create_delegation_edge("w1", "w2", EdgeSource.ASSIGN)

        svc.handle_worker_ended("w1", "reached ERROR")

        assert backend.calls == []

    def test_a_failed_delivery_publishes_for_the_caller(self, db, env, backend):
        edge = create_delegation_edge("sup", "w1", EdgeSource.SEND_MESSAGE, message_id=5)
        assert edge.id

        svc.handle_delivery_failed("w1", [5], "terminal gone")

        assert backend.calls == [({"sup": 0}, False)]

    def test_a_task_that_was_never_delivered_publishes_for_the_caller(self, db, env, backend):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)  # unarmed

        assert svc.handle_task_never_delivered("w1") == 1

        assert backend.calls == [({"sup": 0}, False)]

    def test_a_failed_label_write_does_not_stop_the_reply_closing_the_edge(self, db, backend):
        backend.error = RuntimeError("core went away")
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)

        with _reply_status(MessageStatus.DELIVERED):
            svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=1, expect_reply=False)

        assert list_open_edges_into("w1") == []


# --- the sweep repairs every terminal ---


class TestSweep:
    def test_the_sweep_publishes_every_callers_count_as_a_complete_pass(self, db, env, backend):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        create_delegation_edge("sup", "w2", EdgeSource.ASSIGN)
        create_delegation_edge("w1", "w3", EdgeSource.SEND_MESSAGE)

        svc.sweep()

        assert backend.calls == [({"sup": 2, "w1": 1}, True)]

    def test_the_sweep_with_no_edges_still_clears_stale_labels(self, db, env, backend):
        svc.sweep()

        assert backend.calls == [({}, True)]

    def test_a_forward_in_the_sweep_is_in_its_final_counts(self, db, env, backend):
        _armed("sup", "w1")

        svc.sweep()

        # The forward's own refresh and the repair may land in either order; both read
        # the database after the forward, so the repair's counts are final either way.
        assert ({}, True) in backend.calls

    def test_a_stale_reply_mark_the_sweep_settles_publishes_the_callers_count(
        self, db, env, backend
    ):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        mark_reply_in_flight(edge.id, 5)

        with _reply_status(MessageStatus.DELIVERED):
            svc.sweep()

        assert ({"sup": 0}, False) in backend.calls
        assert list_open_edges_into("w1") == []

    def test_a_failing_backend_does_not_stop_the_sweep(self, db, env, backend):
        backend.error = RuntimeError("core went away")
        _armed("sup", "w1")

        svc.sweep()  # must not raise

        assert list_open_edges_into("w1") == []

    def test_without_a_backend_the_sweep_still_forwards(self, db, env):
        _armed("sup", "w1")

        svc.sweep()

        assert list_open_edges_into("w1") == []


# --- review of the waiting label (round 1) ---


class GatedBackend:
    """Blocks its first ``set_waiting_counts`` until released (a hung herdr-core).

    Keeps the label each caller ends up with, so a test can compare it with the database.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.labels: Dict[str, int] = {}
        self.call_count = 0
        self._guard = threading.Lock()

    def set_waiting_counts(self, counts: Mapping[str, int], *, complete: bool = False) -> None:
        with self._guard:
            self.call_count += 1
            first = self.call_count == 1
        if first:
            self.entered.set()
            self.release.wait(10.0)
        for caller, count in counts.items():
            if count:
                self.labels[caller] = count
            else:
                self.labels.pop(caller, None)


@pytest.fixture
def gated():
    hung = GatedBackend()
    registry.set_backend(hung)  # type: ignore[arg-type]
    yield hung
    hung.release.set()


class TestNeverDelaysTheEdgeChange:
    """Spec: a label write "never fails or delays" the change that caused it (F1)."""

    @staticmethod
    def _send_message() -> None:
        svc.on_send_message(sender_id="sup", receiver_id="w1", message_id=7, expect_reply=True)

    @staticmethod
    def _worker_ended() -> None:
        _armed("sup", "w1")
        svc.handle_worker_ended("w1", "was deleted")

    @staticmethod
    def _delivery_failed() -> None:
        create_delegation_edge("sup", "w1", EdgeSource.SEND_MESSAGE, message_id=5)
        svc.handle_delivery_failed("w1", [5], "terminal gone")

    @pytest.mark.parametrize("change", [_send_message, _worker_ended, _delivery_failed])
    def test_a_hung_label_write_does_not_hold_the_change(self, db, env, gated, change):
        started = time.perf_counter()
        change.__func__()  # type: ignore[attr-defined]
        elapsed = time.perf_counter() - started

        assert elapsed < 1.0, f"the change waited {elapsed:.1f}s for the label write"
        assert gated.entered.wait(5.0), "the label write never started"

    def test_refreshes_that_pile_up_behind_a_hung_write_become_one_more_write(self, db, gated):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        svc.refresh_waiting_labels(["sup"])
        assert gated.entered.wait(5.0)

        for _ in range(5):
            svc.refresh_waiting_labels(["sup"])
        gated.release.set()
        svc.flush_waiting_labels()

        assert gated.call_count == 2
        assert gated.labels == {"sup": 1}


class TestOverlappingRefreshes:
    def test_the_last_write_carries_the_latest_count(self, db, gated):
        """A write that was slow must not put back a count the database no longer holds (F2)."""
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        create_delegation_edge("sup", "w2", EdgeSource.ASSIGN)
        first = threading.Thread(target=svc.refresh_waiting_labels, args=(["sup"],))
        first.start()
        try:
            assert gated.entered.wait(5.0)
            with _reply_status(MessageStatus.DELIVERED):
                svc.on_send_message(
                    sender_id="w1", receiver_id="sup", message_id=1, expect_reply=False
                )
        finally:
            gated.release.set()
        first.join(5.0)
        svc.flush_waiting_labels()

        assert gated.labels == {"sup": len(list_open_edges_out_of("sup"))} == {"sup": 1}


class TestTheEndedTerminalsOwnLabel:
    def test_a_caller_that_is_kept_after_error_has_its_label_cleared(self, db, env, backend):
        """Its edges close with it, so its own count is 0 whatever it showed (F3)."""
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        create_delegation_edge("sup", "w2", EdgeSource.ASSIGN)

        svc.handle_worker_ended("sup", "reached ERROR", may_be_running=True)

        assert backend.calls == [({"sup": 0}, False)]

    def test_a_worker_kept_after_error_clears_its_own_and_its_callers_labels(
        self, db, env, backend
    ):
        _armed("boss", "sup")
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)

        svc.handle_worker_ended("sup", "reached ERROR", may_be_running=True)

        assert backend.calls == [({"sup": 0, "boss": 0}, False)]

    def test_a_deleted_caller_is_not_written_to(self, db, env, backend):
        """Its label goes with the terminal; the sweep covers any that outlives it."""
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)

        svc.handle_worker_ended("sup", "was deleted")

        assert backend.calls == []


class TestAnEdgeOpenedAfterTheListing:
    def test_its_caller_is_refreshed_although_no_notice_was_sent(self, db, env, backend):
        """The edge ``close_open_edges_for_terminal`` closes without a notice still counts (F4)."""
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)

        with patch(f"{MOD}.list_open_edges_into", return_value=[]):
            svc.handle_worker_ended("w1", "was deleted")

        assert list_open_edges_into("w1") == []
        assert backend.calls == [({"sup": 0}, False)]


class TestTheSweepRepairsWhateverElseFails:
    @pytest.mark.parametrize("failing", ["_log_cycles", "list_armed_worker_ids"])
    def test_the_repair_runs_when_an_earlier_step_raises(self, db, env, backend, caplog, failing):
        with (
            patch(f"{MOD}.{failing}", side_effect=RuntimeError("step failed")),
            caplog.at_level(logging.WARNING, logger=MOD),
        ):
            svc.sweep()

        assert backend.calls == [({}, True)]
        assert any("step failed" in r.getMessage() for r in caplog.records)


class TestTheSweepKeepsGoingAfterOneStepFails:
    def test_a_failure_settling_reply_marks_is_warned_about_and_the_rest_still_runs(
        self, db, env, backend, caplog
    ):
        _armed("sup", "w1")

        with (
            patch(f"{MOD}.settle_stale_reply_marks", side_effect=RuntimeError("marks locked")),
            caplog.at_level(logging.WARNING, logger=MOD),
        ):
            svc.sweep()

        assert list_open_edges_into("w1") == []  # the armed worker was still evaluated
        # the forward's refresh is written by the background thread and the repair by the sweep
        # itself, so either may land first
        assert sorted(backend.calls, key=lambda call: call[1]) == [({"sup": 0}, False), ({}, True)]
        assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
            "delegation sweep could not settle reply marks: marks locked"
        ]

    def test_one_workers_failed_evaluation_does_not_stop_the_next_worker(
        self, db, env, backend, caplog
    ):
        _armed("sup", "w1")
        _armed("sup", "w2")
        real_evaluate = svc.evaluate_completion
        seen = []

        def evaluate(worker_id, registry=None):
            seen.append(worker_id)
            if worker_id == "w1":
                raise RuntimeError("w1 output unreadable")
            real_evaluate(worker_id, registry)

        with (
            patch(f"{MOD}.evaluate_completion", side_effect=evaluate),
            caplog.at_level(logging.DEBUG, logger=MOD),
        ):
            svc.sweep()

        assert sorted(seen) == ["w1", "w2"]
        assert [e.worker_id for e in list_open_edges_into("w1")] == ["w1"]  # w1 was left alone
        assert list_open_edges_into("w2") == []  # w2 was forwarded
        assert any(
            r.levelno == logging.DEBUG
            and r.getMessage() == "delegation sweep failed for w1: w1 output unreadable"
            for r in caplog.records
        )
        # the repair still ran as a complete pass, and w1's open edge is in its count
        assert ({"sup": 1}, True) in backend.calls


class TestAStoppedPublisherTakesNoMoreRequests:
    def test_a_request_after_stop_is_dropped(self, db, backend, publisher):
        publisher.stop()

        svc.refresh_waiting_labels(["sup"])

        assert backend.calls == []


class TestBackendsThatDoNotShowTheLabel:
    """tmux and the herdr TUI keep the no-op default, so no query is made for it (N1)."""

    @pytest.fixture
    def plain(self):
        plain = object.__new__(TmuxBackend)
        registry.set_backend(plain)
        return plain

    def test_an_edge_change_makes_no_count_query(self, db, plain):
        with patch(f"{MOD}.list_open_edges_out_of") as count:
            svc.refresh_waiting_labels(["sup"])
            svc.flush_waiting_labels()

        count.assert_not_called()

    def test_the_sweep_makes_no_count_query(self, db, env, plain):
        with patch(f"{MOD}.count_open_edges_by_caller") as count:
            svc.sweep()

        count.assert_not_called()


class TestFailuresAreWarnedAboutOncePerEpisode:
    def test_a_backend_that_keeps_failing_is_one_warning_not_one_per_call(
        self, db, backend, caplog
    ):
        backend.error = RuntimeError("core went away")

        def warnings():
            return [r for r in caplog.records if r.levelno == logging.WARNING]

        with caplog.at_level(logging.DEBUG, logger=MOD):
            for _ in range(3):
                svc.refresh_waiting_labels(["sup"])
                svc.flush_waiting_labels()
            assert len(warnings()) == 1

            backend.error = None  # the core is back: the episode ends
            svc.refresh_waiting_labels(["sup"])
            svc.flush_waiting_labels()
            backend.error = RuntimeError("went away again")
            svc.refresh_waiting_labels(["sup"])
            svc.flush_waiting_labels()

        assert len(warnings()) == 2
