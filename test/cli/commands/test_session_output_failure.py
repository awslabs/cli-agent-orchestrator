"""A completed status must not hide failure to retrieve the response."""

from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands import session


@pytest.mark.parametrize("interrupted", [False, True])
def test_output_request_failure_is_not_success(monkeypatch, interrupted):
    status = Mock()
    status.json.return_value = {"status": "completed"}
    output = Mock()
    output.raise_for_status.side_effect = session.api_http.exceptions.HTTPError("extraction failed")
    sent = Mock()
    sent.json.return_value = {"dispatch_boundary": 1}
    monkeypatch.setattr(session, "_resolve_conductor", lambda _: ({"id": "test"}, []))
    monkeypatch.setattr(session.api_http, "get", Mock(side_effect=[status, output]))
    monkeypatch.setattr(session.api_http, "post", Mock(return_value=sent))
    poll = Mock(side_effect=KeyboardInterrupt if interrupted else None)
    monkeypatch.setattr(session, "poll_until_done", poll)
    result = CliRunner().invoke(session.send, ["cao-test", "Question"])
    assert result.exit_code == (130 if interrupted else 1)
    if not interrupted:
        assert "Failed to retrieve the agent response" in result.output
