"""Issue #1082: report_outcome's optional terminal_id joins the outcome to one
launch, bound to the caller's own assign/handoff tree (same ``_target_binding_denial``
helper #912 wired into delete_terminal/answer_user_prompt/send_message)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.mcp_server.server import report_outcome

LEARNING_TARGET = "cli_agent_orchestrator.services.settings_service.is_learning_enabled"
OWN_ID = "abc12345"


@pytest.fixture
def isolated_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'outcomes.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with patch("cli_agent_orchestrator.clients.database.SessionLocal", factory):
        yield engine


def _run(coro):
    return asyncio.run(coro)


def _report(**overrides):
    kwargs = dict(
        task_label="t",
        success=True,
        workflow_name=None,
        agent_profile=None,
        score=None,
        friction_notes="",
        terminal_id=None,
    )
    kwargs.update(overrides)
    return report_outcome(**kwargs)


def _seed_target_terminal(*, terminal_id: str, caller_id):
    """Register a real terminal row so GET /terminals/{terminal_id} -- which
    _target_binding_denial issues through the SAME ``requests`` module
    mcp_over_testclient already routes into the real FastAPI app -- resolves
    against real data instead of a hand-written stub that would also leak
    into the caller-identity lookup (same module-level ``requests.get``)."""
    from cli_agent_orchestrator.clients import database

    with database.SessionLocal() as db:
        db.add(
            database.TerminalModel(
                id=terminal_id,
                tmux_session="child-session",
                tmux_window="w",
                provider="kiro_cli",
                caller_id=caller_id,
            )
        )
        db.commit()


class TestReportOutcomeWithoutTerminalId:
    def test_default_omits_launch_terminal_id(self, mcp_over_testclient, isolated_db):
        """Unaffected default behavior: no terminal_id means no link attempt."""
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_report())
        assert result["success"] is True, result
        assert mcp_over_testclient.last.json["launch_terminal_id"] is None


class TestReportOutcomeSelfBinding:
    def test_self_report_is_allowed(self, mcp_over_testclient, isolated_db):
        """AC: the caller's own terminal is always allowed."""
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_report(terminal_id=OWN_ID))
        assert result["success"] is True, result
        assert mcp_over_testclient.last.json["launch_terminal_id"] == OWN_ID


class TestReportOutcomeChildBinding:
    def test_child_terminal_is_allowed_and_linked(self, mcp_over_testclient, isolated_db):
        """AC: report_outcome(terminal_id=<child>) from the child's creator links
        the outcome to that launch."""
        _seed_target_terminal(terminal_id="deadbeef", caller_id=OWN_ID)
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_report(terminal_id="deadbeef"))
        assert result["success"] is True, result
        assert mcp_over_testclient.last.json["launch_terminal_id"] == "deadbeef"

    def test_unrelated_terminal_is_refused_and_posts_nothing(
        self, mcp_over_testclient, isolated_db
    ):
        """AC: the same call from a caller outside the tree is refused -- no
        /outcomes request is ever issued for a denied link."""
        _seed_target_terminal(terminal_id="deadbeef", caller_id="someone-else")
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_report(terminal_id="deadbeef"))
        assert result["success"] is False
        assert "error" in result
        assert [r for r in mcp_over_testclient.requests if r.path == "/outcomes"] == []

    def test_refusal_mutation_guard_mentions_both_terminals(self, mcp_over_testclient, isolated_db):
        """Mutation guard against a refusal that silently drops the terminal_id
        check: the denial message names the target, so a blanket True/False
        swap or a dropped id argument is observable, not just 'False'."""
        _seed_target_terminal(terminal_id="deadbeef", caller_id="someone-else")
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_report(terminal_id="deadbeef"))
        assert "deadbeef" in result["error"]


class TestOutcomeEndpointLinksLaunchRecord:
    """The /outcomes route itself: launch_terminal_id joins the outcome to a
    launch record, best-effort, never failing the outcome write."""

    def test_link_is_called_on_success(self, mcp_over_testclient, isolated_db, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.link_launch_outcome",
            lambda terminal_id, success: calls.append((terminal_id, success)),
        )
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_report(terminal_id=OWN_ID, success=False))
        assert result["success"] is True, result
        assert calls == [(OWN_ID, False)]

    def test_link_failure_never_fails_the_outcome_write(
        self, mcp_over_testclient, isolated_db, monkeypatch
    ):
        """AC: a failure writing the record must never fail the launch it
        describes -- here, must never fail report_outcome's own success. The
        injected failure bypasses link_launch_outcome's OWN try/except
        entirely (it replaces the whole function), so this is a genuine test
        of the endpoint's defense-in-depth wrapper, not a tautology."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.link_launch_outcome",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db is down")),
        )
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_report(terminal_id=OWN_ID))
        assert result["success"] is True, result
        assert "outcome_id" in result
