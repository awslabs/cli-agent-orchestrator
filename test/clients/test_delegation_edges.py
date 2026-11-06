"""Tests for delegation edge database functions."""

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    close_delegation_edge,
    close_open_edges_for_terminal,
    create_delegation_edge,
    find_open_edge,
    list_armed_worker_ids,
    list_open_edges_into,
    list_open_edges_out_of,
    mark_assign_edges_delivered,
    mark_message_edges_delivered,
)
from cli_agent_orchestrator.models.delegation import EdgeReason, EdgeSource, EdgeState


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
