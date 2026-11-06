"""Tests for delegation_callback_service."""

import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    create_delegation_edge,
    find_open_edge,
    list_open_edges_into,
    mark_assign_edges_delivered,
)
from cli_agent_orchestrator.models.delegation import EdgeReason, EdgeSource
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import delegation_callback_service as svc

MOD = "cli_agent_orchestrator.services.delegation_callback_service"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)
    with patch("cli_agent_orchestrator.clients.database.SessionLocal", session):
        yield


class TestMessages:
    def test_auto_forward_header(self):
        text = svc.build_auto_forward_message("w1", "developer", EdgeReason.NO_CALLBACK, "result")
        assert text.startswith("[CAO auto-forward] Worker w1 (profile developer)")
        assert "Reason: did not call send_message" in text
        assert "may be partial" in text
        assert 'send_message(receiver_id="w1"' in text
        assert 'delete_terminal("w1")' in text
        assert text.endswith("---\nresult")

    def test_auto_forward_no_tool_reason(self):
        text = svc.build_auto_forward_message("w1", "p", EdgeReason.NO_SEND_MESSAGE_TOOL, "x")
        assert "Reason: has no send_message tool (cao-mcp-server is not in its profile)" in text

    def test_worker_ended_message(self):
        text = svc.build_worker_ended_message("w1", "developer", "was deleted", None)
        assert text.startswith("[CAO worker ended] Worker w1 (profile developer) was deleted")
        assert "(last response could not be read)" in text


class TestSendMessageRules:
    def test_reply_closes_owed_edge(self, db):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=1, expect_reply=False)
        assert list_open_edges_into("w1") == []

    @patch(f"{MOD}.get_terminal_metadata", return_value={"caller_id": "sup"})
    def test_caller_follow_up_to_own_child_opens_edge(self, _meta, db):
        svc.on_send_message(sender_id="sup", receiver_id="w1", message_id=7, expect_reply=False)
        edge = find_open_edge("sup", "w1")
        assert edge is not None and edge.message_id == 7 and edge.source == EdgeSource.SEND_MESSAGE

    @patch(f"{MOD}.get_terminal_metadata", return_value={"caller_id": "sup"})
    def test_follow_up_does_not_duplicate_open_edge(self, _meta, db):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        svc.on_send_message(sender_id="sup", receiver_id="w1", message_id=8, expect_reply=False)
        assert len(list_open_edges_into("w1")) == 1

    @patch(f"{MOD}.get_terminal_metadata", return_value={"caller_id": "someone-else"})
    def test_unrelated_terminal_without_expect_reply(self, _meta, db):
        svc.on_send_message(sender_id="w1", receiver_id="w3", message_id=9, expect_reply=False)
        assert find_open_edge("w1", "w3") is None

    @patch(f"{MOD}.get_terminal_metadata", return_value={"caller_id": "someone-else"})
    def test_unrelated_terminal_with_expect_reply(self, _meta, db):
        svc.on_send_message(sender_id="w1", receiver_id="w3", message_id=9, expect_reply=True)
        assert find_open_edge("w1", "w3") is not None


def _armed(caller, worker):
    create_delegation_edge(caller, worker, EdgeSource.ASSIGN)
    mark_assign_edges_delivered(worker)


@pytest.fixture
def env():
    """Patch the I/O the evaluator touches; return the inbox capture list."""
    sent = []
    with (
        patch(f"{MOD}.status_monitor") as sm,
        patch(f"{MOD}.terminal_service") as ts,
        patch(f"{MOD}.get_pending_messages", return_value=[]) as pending,
        patch(f"{MOD}.get_terminal_metadata", return_value={"agent_profile": "developer"}),
        patch(
            f"{MOD}.create_inbox_message",
            side_effect=lambda sender_id, receiver_id, message: sent.append(
                (sender_id, receiver_id, message)
            ),
        ),
        patch(f"{MOD}.inbox_service"),
    ):
        sm.get_status.return_value = TerminalStatus.IDLE
        ts.get_output.return_value = "final answer"
        yield {"sent": sent, "status": sm, "pending": pending, "ts": ts}


class TestEvaluateCompletion:
    def test_forwards_idle_worker(self, db, env):
        _armed("sup", "w1")
        assert svc.evaluate_completion("w1") == 1
        sender, receiver, text = env["sent"][0]
        assert (sender, receiver) == ("w1", "sup")
        assert text.endswith("final answer")
        assert list_open_edges_into("w1") == []

    def test_unarmed_edge_not_forwarded(self, db, env):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        assert svc.evaluate_completion("w1") == 0

    def test_waiting_on_sub_worker_not_forwarded(self, db, env):
        _armed("sup", "w1")
        create_delegation_edge("w1", "w2", EdgeSource.ASSIGN)
        assert svc.evaluate_completion("w1") == 0
        assert env["sent"] == []

    def test_forwarded_after_sub_worker_replies(self, db, env):
        _armed("sup", "w1")
        create_delegation_edge("w1", "w2", EdgeSource.ASSIGN)
        svc.on_send_message(sender_id="w2", receiver_id="w1", message_id=3, expect_reply=False)
        assert svc.evaluate_completion("w1") == 1

    def test_pending_inbox_blocks(self, db, env):
        _armed("sup", "w1")
        env["pending"].return_value = [object()]
        assert svc.evaluate_completion("w1") == 0

    def test_processing_blocks(self, db, env):
        _armed("sup", "w1")
        env["status"].get_status.return_value = TerminalStatus.PROCESSING
        assert svc.evaluate_completion("w1") == 0

    def test_second_evaluation_does_not_double_forward(self, db, env):
        _armed("sup", "w1")
        svc.evaluate_completion("w1")
        assert svc.evaluate_completion("w1") == 0
        assert len(env["sent"]) == 1

    def test_unreadable_output_still_forwards(self, db, env):
        _armed("sup", "w1")
        env["ts"].get_output.side_effect = RuntimeError("pane gone")
        assert svc.evaluate_completion("w1") == 1
        assert "(last response could not be read)" in env["sent"][0][2]


class TestWorkerEnded:
    def test_deleted_worker_notifies_caller(self, db, env):
        _armed("sup", "w1")
        svc.handle_worker_ended("w1", "was deleted")
        sender, receiver, text = env["sent"][0]
        assert (sender, receiver) == ("w1", "sup")
        assert text.startswith("[CAO worker ended] Worker w1")
        assert list_open_edges_into("w1") == []

    def test_outbound_edges_closed_silently(self, db, env):
        create_delegation_edge("w1", "w2", EdgeSource.ASSIGN)
        svc.handle_worker_ended("w1", "reached ERROR")
        assert env["sent"] == []


class TestSweep:
    def test_sweep_forwards_missed_worker(self, db, env):
        _armed("sup", "w1")
        svc.sweep()
        assert len(env["sent"]) == 1

    def test_cycle_logged_not_forwarded(self, db, env, caplog):
        _armed("w1", "w2")
        _armed("w2", "w1")
        svc.sweep()
        assert env["sent"] == []
        assert "delegation cycle" in caplog.text


class TestConsumer:
    def test_idle_event_evaluates_and_error_event_ends(self):
        events = [
            {"topic": "terminal.w1.status", "data": {"status": "idle"}},
            {"topic": "terminal.w2.status", "data": {"status": "error"}},
        ]

        class FakeQueue:
            async def get(self):
                if events:
                    return events.pop(0)
                raise asyncio.CancelledError

        with (
            patch(f"{MOD}.bus") as bus,
            patch(f"{MOD}.evaluate_completion") as ev,
            patch(f"{MOD}.handle_worker_ended") as ended,
        ):
            bus.subscribe.return_value = FakeQueue()
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(svc.run())
        ev.assert_called_once_with("w1")
        ended.assert_called_once_with("w2", "reached ERROR")
