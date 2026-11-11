"""Tests for delegation edge database functions."""

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    close_delegation_edge,
    close_delegation_edge_with_message,
    close_open_edges_for_terminal,
    create_delegation_edge,
    create_terminal,
    delete_terminals_by_ids,
    find_any_edge,
    find_open_edge,
    get_inbox_messages,
    list_armed_worker_ids,
    list_open_edges_into,
    list_open_edges_out_of,
    list_open_message_edges,
    list_unarmed_assign_edges,
    mark_assign_edges_delivered,
    mark_message_edges_delivered,
)
from cli_agent_orchestrator.models.delegation import EdgeReason, EdgeSource, EdgeState
from cli_agent_orchestrator.models.inbox import MessageStatus


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)
    with patch("cli_agent_orchestrator.clients.database.SessionLocal", session):
        yield


def test_new_assign_edge_is_open_and_unarmed(db):
    edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
    assert edge.state == EdgeState.OPEN
    assert edge.delivered_at is None
    assert edge.reason == EdgeReason.NO_CALLBACK
    assert list_open_edges_into("w1", armed_only=True) == []
    assert [e.id for e in list_open_edges_into("w1")] == [edge.id]


def test_stored_reason_this_version_does_not_define_reads_as_default(db):
    from cli_agent_orchestrator.clients import database

    with database.SessionLocal() as session:
        session.add(
            database.DelegationEdgeModel(
                caller_id="sup",
                worker_id="w1",
                source=EdgeSource.ASSIGN.value,
                state=EdgeState.OPEN.value,
                reason="a_reason_this_version_does_not_define",
            )
        )
        session.commit()
    [edge] = list_open_edges_into("w1")
    assert edge.reason == EdgeReason.NO_CALLBACK


def test_mark_assign_edges_delivered_arms_them(db):
    edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
    mark_assign_edges_delivered("w1")
    armed = list_open_edges_into("w1", armed_only=True)
    assert [e.id for e in armed] == [edge.id]
    assert list_armed_worker_ids() == ["w1"]


def test_mark_message_edges_delivered_only_touches_matching_message(db):
    e1 = create_delegation_edge("sup", "w1", EdgeSource.SEND_MESSAGE, message_id=10)
    e2 = create_delegation_edge("sup", "w2", EdgeSource.SEND_MESSAGE, message_id=11)
    mark_message_edges_delivered([10])
    assert [e.id for e in list_open_edges_into("w1", armed_only=True)] == [e1.id]
    assert list_open_edges_into("w2", armed_only=True) == []
    assert e2.id


def test_close_is_one_shot(db):
    edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
    assert close_delegation_edge(edge.id, EdgeState.REPLIED) is True
    assert close_delegation_edge(edge.id, EdgeState.AUTO_FORWARDED) is False
    assert list_open_edges_into("w1") == []


def test_open_edges_out_of(db):
    create_delegation_edge("w1", "w2", EdgeSource.ASSIGN)
    create_delegation_edge("w1", "w3", EdgeSource.SEND_MESSAGE, message_id=5)
    assert sorted(e.worker_id for e in list_open_edges_out_of("w1")) == ["w2", "w3"]


def test_find_open_edge(db):
    edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
    assert find_open_edge("sup", "w1").id == edge.id
    assert find_open_edge("w1", "sup") is None


def test_close_open_edges_for_terminal_returns_inbound_only(db):
    inbound = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
    create_delegation_edge("w1", "w2", EdgeSource.ASSIGN)
    closed_inbound = close_open_edges_for_terminal("w1", EdgeState.WORKER_ENDED)
    assert [e.id for e in closed_inbound] == [inbound.id]
    assert list_open_edges_into("w1") == []
    assert list_open_edges_out_of("w1") == []


class TestCloseWithMessage:
    """The claim and the message it sends are one commit (PR #1247 review, P2-1 path 3)."""

    def test_claim_queues_the_message_for_the_caller(self, db):
        create_terminal("sup", "cao-s", "sup", "claude_code")
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)

        assert close_delegation_edge_with_message(
            edge.id, EdgeState.AUTO_FORWARDED, "w1", "sup", "result"
        )

        assert list_open_edges_into("w1") == []
        (message,) = get_inbox_messages("sup")
        assert (message.sender_id, message.message, message.status) == (
            "w1",
            "result",
            MessageStatus.PENDING,
        )

    def test_lost_claim_queues_nothing(self, db):
        create_terminal("sup", "cao-s", "sup", "claude_code")
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        close_delegation_edge(edge.id, EdgeState.REPLIED)

        assert not close_delegation_edge_with_message(
            edge.id, EdgeState.AUTO_FORWARDED, "w1", "sup", "result"
        )

        assert get_inbox_messages("sup") == []

    def test_gone_caller_still_closes_the_edge(self, db):
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)

        assert close_delegation_edge_with_message(
            edge.id, EdgeState.WORKER_ENDED, "w1", "sup", "notice"
        )

        assert list_open_edges_into("w1") == []
        assert get_inbox_messages("sup") == []

    def test_failed_message_insert_leaves_the_edge_open(self, db):
        """Close and queue are one commit: if the queue fails, the claim is undone."""
        create_terminal("sup", "cao-s", "sup", "claude_code")
        edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)

        with patch(
            "cli_agent_orchestrator.clients.database.InboxModel",
            side_effect=RuntimeError("insert failed"),
        ):
            with pytest.raises(RuntimeError, match="insert failed"):
                close_delegation_edge_with_message(
                    edge.id, EdgeState.AUTO_FORWARDED, "w1", "sup", "result"
                )

        assert [e.id for e in list_open_edges_into("w1")] == [edge.id]
        assert get_inbox_messages("sup") == []


def test_bulk_row_deletion_closes_edges_in_both_directions(db):
    create_terminal("w1", "cao-s", "w1", "claude_code")
    create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
    mark_assign_edges_delivered("w1")
    create_delegation_edge("w1", "w2", EdgeSource.ASSIGN)
    create_delegation_edge("w2", "w1", EdgeSource.SEND_MESSAGE, message_id=4)
    untouched = create_delegation_edge("sup", "w3", EdgeSource.ASSIGN)
    # "gone" has no row (another owner deleted it); its edge closes all the same.
    create_delegation_edge("sup", "gone", EdgeSource.ASSIGN)

    assert delete_terminals_by_ids(["w1", "gone"], end_reason="session_teardown") == 1

    assert list_open_edges_into("gone") == []

    assert list_open_edges_into("w1") == []
    assert list_open_edges_out_of("w1") == []
    assert list_armed_worker_ids() == []
    assert [e.id for e in list_open_edges_into("w3")] == [untouched.id]
    assert get_inbox_messages("sup") == []


def test_list_open_message_edges_returns_open_edges_of_those_messages(db):
    failed = create_delegation_edge("sup", "w1", EdgeSource.SEND_MESSAGE, message_id=10)
    create_delegation_edge("sup", "w2", EdgeSource.SEND_MESSAGE, message_id=11)
    closed = create_delegation_edge("top", "w1", EdgeSource.SEND_MESSAGE, message_id=10)
    close_delegation_edge(closed.id, EdgeState.REPLIED)

    assert [e.id for e in list_open_message_edges([10, 99])] == [failed.id]
    assert list_open_message_edges([]) == []


def test_list_unarmed_assign_edges_skips_armed_and_message_edges(db):
    unarmed = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
    create_delegation_edge("top", "w1", EdgeSource.SEND_MESSAGE, message_id=3)
    create_delegation_edge("sup", "w2", EdgeSource.ASSIGN)
    mark_assign_edges_delivered("w2")

    assert [e.id for e in list_unarmed_assign_edges("w1")] == [unarmed.id]
    assert list_unarmed_assign_edges("w2") == []


def test_find_any_edge_returns_a_closed_edge(db):
    edge = create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
    close_delegation_edge(edge.id, EdgeState.REPLIED)

    assert find_open_edge("sup", "w1") is None
    found = find_any_edge("sup", "w1")
    assert (found.id, found.state) == (edge.id, EdgeState.REPLIED)
    assert find_any_edge("w1", "sup") is None
