"""Tests for delegation edge enums."""

from cli_agent_orchestrator.models.delegation import EdgeReason, EdgeSource, EdgeState


def test_enum_values_are_stable():
    assert [s.value for s in EdgeState] == ["open", "replied", "auto_forwarded", "worker_ended"]
    assert [s.value for s in EdgeSource] == ["assign", "send_message"]
    assert [r.value for r in EdgeReason] == ["no_callback"]
