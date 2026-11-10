"""The client side of keeping finished children (#1225 phase 1).

The agent-side entry points (the ``delete_terminal`` MCP tool, ``cao agent
handoff``'s own post-success teardown) send ``keep_if_enabled``; the operator ones
(``cao agent cancel --delete``) never do, and with nothing kept every request and
message is the one sent before.
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import urlsplit

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.utils import orchestration
from cli_agent_orchestrator.utils.orchestration import (
    _cancel_impl,
    _delete_terminal_impl,
    _run_step_and_build_result,
)

_ORCH = "cli_agent_orchestrator.utils.orchestration"


def _response(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class TestDeleteTerminalImpl:
    def test_the_default_request_is_unchanged(self):
        with patch(f"{_ORCH}.requests.delete", return_value=_response({"success": True})) as d:
            result = _delete_terminal_impl("abcd1234", check_binding=False)
        assert "params" not in d.call_args.kwargs
        assert result == {"success": True, "message": "Terminal abcd1234 deleted successfully"}

    def test_the_agent_flag_is_sent_as_a_query_parameter(self):
        with patch(f"{_ORCH}.requests.delete", return_value=_response({"success": True})) as d:
            result = _delete_terminal_impl("abcd1234", check_binding=False, keep_if_enabled=True)
        assert d.call_args.kwargs["params"] == {"keep_if_enabled": "true"}
        # Setting off: the server deleted it, and the message is the usual one.
        assert result == {"success": True, "message": "Terminal abcd1234 deleted successfully"}

    def test_a_kept_answer_tells_the_agent_it_was_kept(self):
        payload = {
            "success": True,
            "kept": True,
            "kept_until": "2026-11-06T04:00:00+00:00",
            "keep_note": "Keeping finished agents is on for this machine.",
        }
        with patch(f"{_ORCH}.requests.delete", return_value=_response(payload)):
            result = _delete_terminal_impl("abcd1234", check_binding=False, keep_if_enabled=True)
        assert result["success"] is True and result["kept"] is True
        assert result["kept_until"] == "2026-11-06T04:00:00+00:00"
        assert result["message"] == (
            "Terminal abcd1234 was kept until 2026-11-06 04:00 UTC, not deleted. "
            "Keeping finished agents is on for this machine."
        )

    def test_a_delete_that_was_not_kept_says_why(self):
        payload = {"success": True, "keep_note": "Not kept: it was still working."}
        with patch(f"{_ORCH}.requests.delete", return_value=_response(payload)):
            result = _delete_terminal_impl("abcd1234", check_binding=False, keep_if_enabled=True)
        assert result["keep_note"] == "Not kept: it was still working."
        assert "deleted successfully. Not kept" in result["message"]
        assert "kept" not in result


class TestEntryPoints:
    def test_the_mcp_tool_asks_for_a_keep_on_a_local_delete(self):
        from cli_agent_orchestrator.mcp_server import server

        with patch.object(server, "_delete_terminal_impl", return_value={"success": True}) as impl:
            server.delete_terminal("abcd1234")
        impl.assert_called_once_with("abcd1234", target_host=None, keep_if_enabled=True)

    def test_the_mcp_tool_never_asks_for_a_remote_delete(self):
        from cli_agent_orchestrator.mcp_server import server

        with patch.object(server, "_delete_terminal_impl", return_value={"success": True}) as impl:
            server.delete_terminal("abcd1234", target_host="worker-1:9889")
        impl.assert_called_once_with("abcd1234", target_host="worker-1:9889", keep_if_enabled=False)

    def test_cao_agent_cancel_delete_is_an_operator_delete(self):
        with patch(f"{_ORCH}._delete_terminal_impl", return_value={"success": True}) as impl:
            _cancel_impl("abcd1234", delete=True)
        impl.assert_called_once_with("abcd1234")

    def test_cao_agent_cancel_delete_sends_no_flag_on_the_wire(self):
        with (
            patch(f"{_ORCH}._target_binding_denial", return_value=None),
            patch(f"{_ORCH}.requests.delete", return_value=_response({"success": True})) as d,
        ):
            _cancel_impl("abcd1234", delete=True)
        assert "params" not in d.call_args.kwargs


def _run_step(payload):
    with (
        patch(f"{_ORCH}.requests.post", return_value=_response(payload)),
        patch(f"{_ORCH}._get_cleanup_nudge", return_value=""),
    ):
        return asyncio.run(
            _run_step_and_build_result({"job_id": "0" * 32}, "developer", "claude_code", 60, 0.0)
        )


class TestHandoffResult:
    """#1215's teardown sentence on every path; on the kept path, "kept until" instead."""

    KEPT = {
        "terminal_id": "bbbbbbbb",
        "last_message": "done",
        "status": "completed",
        "torn_down": False,
        "kept_until": "2026-11-06T04:00:00Z",
        "keep_note": "Keeping finished agents is on for this machine.",
    }

    def test_a_kept_worker_says_kept_until_in_place_of_the_teardown_sentence(self):
        result = _run_step(self.KEPT)
        assert result.success is True and result.terminal_id == "bbbbbbbb"
        assert result.output == "done"
        assert result.message.endswith(
            "s. Worker terminal bbbbbbbb was kept until 2026-11-06 04:00 UTC. Keeping finished "
            "agents is on for this machine. Read its last answer with `cao agent result "
            "bbbbbbbb`, or open its pane to see what it did."
        )
        assert "torn down" not in result.message

    def test_a_keep_never_hides_a_teardown_that_happened(self):
        result = _run_step({**self.KEPT, "torn_down": True})
        assert "Worker terminal bbbbbbbb was torn down." in result.message
        assert "kept until" not in result.message

    @pytest.mark.parametrize(
        "torn_down, sentence", [(True, "was torn down."), (False, "was not torn down.")]
    )
    def test_with_nothing_kept_the_message_is_1215s(self, torn_down, sentence):
        result = _run_step(
            {
                "terminal_id": "bbbbbbbb",
                "last_message": "done",
                "status": "completed",
                "torn_down": torn_down,
            }
        )
        assert result.message.startswith("Successfully handed off to developer (claude_code) in ")
        assert result.message.endswith(f"s. Worker terminal bbbbbbbb {sentence}")
        assert "kept" not in result.message

    def test_a_worker_not_kept_gets_the_teardown_sentence_then_the_reason(self):
        result = _run_step(
            {
                "terminal_id": "bbbbbbbb",
                "last_message": "done",
                "status": "completed",
                "torn_down": True,
                "keep_note": "Not kept: this session already keeps 5 finished agents.",
            }
        )
        assert result.message.endswith(
            "s. Worker terminal bbbbbbbb was torn down. "
            "Not kept: this session already keeps 5 finished agents."
        )

    def _cli_handoff(self, cleanup):
        with (
            patch(f"{_ORCH}._resolve_handoff_provider") as provider,
            patch(f"{_ORCH}._create_handoff_terminal", return_value=("dev-t1", "claude_code")),
            patch(f"{_ORCH}._delete_terminal_impl", return_value=cleanup) as delete,
            patch(f"{_ORCH}._get_cleanup_nudge", return_value=" NUDGE"),
            patch(f"{_ORCH}.requests.post") as post,
        ):
            provider.return_value = orchestration.HandoffContext(
                provider="claude_code", session_name="s", caller_id="aaaaaaaa", allowed_tools=None
            )
            post.return_value = _response(
                {"terminal_id": "dev-t1", "last_message": "done", "status": "completed"}
            )
            result = asyncio.run(
                orchestration._handoff_impl("developer", "task", on_terminal_id=lambda _: None)
            )
        delete.assert_called_once_with("dev-t1", check_binding=False, keep_if_enabled=True)
        return result

    def test_cao_agent_handoff_reports_a_kept_worker_as_kept_not_torn_down(self):
        result = self._cli_handoff(
            {
                "success": True,
                "kept": True,
                "kept_until": "2026-11-06T04:00:00+00:00",
                "keep_note": "Keeping finished agents is on for this machine.",
                "message": "Terminal dev-t1 was kept until 2026-11-06 04:00 UTC, not deleted.",
            }
        )
        assert result.success is True
        assert "Worker terminal dev-t1 was kept until 2026-11-06 04:00 UTC." in result.message
        assert "torn down" not in result.message
        # Still there, so #1215's nudge still applies.
        assert result.message.endswith(" NUDGE")

    def test_cao_agent_handoff_with_nothing_kept_is_1215s(self):
        result = self._cli_handoff({"success": True, "message": "deleted"})
        assert result.message.endswith("s. Worker terminal dev-t1 was torn down.")

    def test_cao_agent_handoff_not_kept_says_the_teardown_then_why(self):
        result = self._cli_handoff(
            {"success": True, "message": "deleted", "keep_note": "Not kept: it was still working."}
        )
        assert result.message.endswith(
            "s. Worker terminal dev-t1 was torn down. Not kept: it was still working."
        )

    def test_a_424_replay_keeps_nothing_and_names_the_retained_terminal(self):
        """#1221: a failed original create has no finished child, so nothing is kept
        and nothing is deleted; the retained row stays named for its delete."""
        failed = orchestration._HandoffCreateFailed("provider init failed", "dev-t1")
        with (
            patch(f"{_ORCH}._resolve_handoff_provider") as provider,
            patch(f"{_ORCH}._create_handoff_terminal", side_effect=failed),
            patch(f"{_ORCH}._delete_terminal_impl") as delete,
            patch(f"{_ORCH}.requests.post") as post,
        ):
            provider.return_value = orchestration.HandoffContext(
                provider="claude_code", session_name="s", caller_id="aaaaaaaa", allowed_tools=None
            )
            result = asyncio.run(
                orchestration._handoff_impl("developer", "task", on_terminal_id=lambda _: None)
            )
        assert result.success is False and result.terminal_id == "dev-t1"
        assert result.message == "Handoff failed: provider init failed"
        delete.assert_not_called()
        post.assert_not_called()


@pytest.fixture
def status_cli(tmp_path, monkeypatch):
    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.models.terminal import TerminalStatus
    from cli_agent_orchestrator.services import session_service, terminal_service
    from cli_agent_orchestrator.utils import api_http

    engine = create_engine(f"sqlite:///{tmp_path / 'terminals.db'}")
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    backend = Mock()
    backend.session_exists_strict.return_value = True
    monkeypatch.setattr(session_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service.status_monitor, "get_status", lambda _tid: TerminalStatus.IDLE
    )
    monkeypatch.setattr(terminal_service, "get_output", lambda *args: "response")
    for terminal_id, caller in (
        ("f0000000", None),
        ("10000000", "f0000000"),
        ("20000000", "f0000000"),
    ):
        database.create_terminal(
            terminal_id,
            "cao-test",
            terminal_id,
            "kiro_cli",
            agent_profile="developer",
            caller_id=caller,
        )
    client = TestClient(app, base_url="http://localhost")
    monkeypatch.setattr(api_http, "get", lambda url, **kw: client.get(urlsplit(url).path, **kw))
    return database


def _status(*extra):
    from cli_agent_orchestrator.cli.commands.session import session

    result = CliRunner().invoke(session, ["status", "cao-test", "--workers", *extra])
    assert result.exit_code == 0, result.output
    return result.output


def test_cao_session_status_shows_no_kept_column_when_nothing_is_kept(status_cli):
    output = _status()
    header = next(line for line in output.splitlines() if line.startswith("ID"))
    assert header == (
        f"{'ID':<12} {'AGENT':<20} {'PROVIDER':<15} {'MODEL':<20} {'HONORED':<8} {'STATUS':<15}"
    )
    assert "KEPT" not in output
    assert all("kept_until" not in row for row in json.loads(_status("--json"))["workers"])


def test_cao_session_status_shows_kept_workers_and_their_expiry(status_cli):
    until = datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)
    assert status_cli.mark_terminal_kept("20000000", "cao-test", kept_until=until, reason="handoff")
    output = _status()
    assert "KEPT UNTIL" in output
    kept_row = next(line for line in output.splitlines() if line.startswith("20000000"))
    other_row = next(line for line in output.splitlines() if line.startswith("10000000"))
    assert "2026-11-06 04:00 UTC" in kept_row
    assert other_row.rstrip().endswith("-")
    workers = {row["id"]: row for row in json.loads(_status("--json"))["workers"]}
    assert workers["20000000"]["kept_until"].startswith("2026-11-06T04:00:00")
    assert "kept_until" not in workers["10000000"]
