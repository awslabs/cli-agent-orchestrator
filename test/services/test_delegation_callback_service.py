"""Tests for delegation_callback_service."""

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    create_delegation_edge,
    find_open_edge,
    list_open_edges_into,
)
from cli_agent_orchestrator.models.delegation import EdgeReason, EdgeSource
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
