"""Producer-side tests for the Runs flow graph (GH #292).

The SSE relay is covered by test_events_runs.py; these tests pin the two places
that actually PUBLISH ``flow.message`` — the inbox endpoint (worker -> supervisor
replies) and terminal_service.send_input (supervisor -> worker delegation) — with
exactly the ``{sender_id, receiver_id, kind}`` payload the frontend store reads.
"""

from datetime import datetime
from unittest.mock import Mock, patch

from cli_agent_orchestrator.models.inbox import OrchestrationType


class TestInboxFlowProducer:
    """POST /terminals/{id}/inbox/messages announces a flow pulse."""

    def test_inbox_post_publishes_flow_message(self, client):
        fake_msg = Mock(
            id="m1",
            sender_id="aaaa1111",
            receiver_id="bbbb2222",
            created_at=datetime(2026, 1, 1, 0, 0, 0),
        )
        with (
            patch("cli_agent_orchestrator.api.main.create_inbox_message", return_value=fake_msg),
            patch("cli_agent_orchestrator.api.main.inbox_service"),
            patch("cli_agent_orchestrator.api.main.bus") as mock_bus,
        ):
            resp = client.post(
                "/terminals/bbbb2222/inbox/messages",
                params={"sender_id": "aaaa1111", "message": "task done"},
            )

        assert resp.status_code == 200
        mock_bus.publish.assert_any_call(
            "flow.message",
            {"sender_id": "aaaa1111", "receiver_id": "bbbb2222", "kind": "message"},
        )


class TestSendInputFlowProducer:
    """terminal_service.send_input announces handoff/assign delivery."""

    def test_send_input_publishes_flow_message_for_agent_to_agent(self):
        from cli_agent_orchestrator.services import terminal_service

        with (
            patch.object(
                terminal_service,
                "get_terminal_metadata",
                return_value={"tmux_session": "cao-x", "tmux_window": "w"},
            ),
            patch.object(terminal_service, "provider_manager") as pm,
            patch.object(terminal_service, "status_monitor"),
            patch.object(terminal_service, "get_backend"),
            patch.object(terminal_service, "update_last_active"),
            patch.object(terminal_service, "inject_memory_context", side_effect=lambda m, _t: m),
            patch("cli_agent_orchestrator.services.event_bus.bus") as mock_bus,
        ):
            pm.get_provider.return_value = None
            ok = terminal_service.send_input(
                "cccc3333",
                "go",
                sender_id="aaaa1111",
                orchestration_type=OrchestrationType.HANDOFF,
            )

        assert ok is True
        mock_bus.publish.assert_any_call(
            "flow.message",
            {"sender_id": "aaaa1111", "receiver_id": "cccc3333", "kind": "handoff"},
        )

    def test_send_input_without_sender_does_not_publish(self):
        """A plain (non-orchestrated) send must not emit a flow pulse."""
        from cli_agent_orchestrator.services import terminal_service

        with (
            patch.object(
                terminal_service,
                "get_terminal_metadata",
                return_value={"tmux_session": "cao-x", "tmux_window": "w"},
            ),
            patch.object(terminal_service, "provider_manager") as pm,
            patch.object(terminal_service, "status_monitor"),
            patch.object(terminal_service, "get_backend"),
            patch.object(terminal_service, "update_last_active"),
            patch.object(terminal_service, "inject_memory_context", side_effect=lambda m, _t: m),
            patch("cli_agent_orchestrator.services.event_bus.bus") as mock_bus,
        ):
            pm.get_provider.return_value = None
            terminal_service.send_input("cccc3333", "go")

        flow_calls = [
            c for c in mock_bus.publish.call_args_list if c.args and c.args[0] == "flow.message"
        ]
        assert flow_calls == []

    def test_send_input_message_delivery_does_not_double_publish(self):
        """A plain inbox message delivery (kind SEND_MESSAGE) must NOT publish a
        flow pulse here — the inbox endpoint already announced it, so a second
        publish would double-count on the graph."""
        from cli_agent_orchestrator.services import terminal_service

        with (
            patch.object(
                terminal_service,
                "get_terminal_metadata",
                return_value={"tmux_session": "cao-x", "tmux_window": "w"},
            ),
            patch.object(terminal_service, "provider_manager") as pm,
            patch.object(terminal_service, "status_monitor"),
            patch.object(terminal_service, "get_backend"),
            patch.object(terminal_service, "update_last_active"),
            patch.object(terminal_service, "inject_memory_context", side_effect=lambda m, _t: m),
            patch("cli_agent_orchestrator.services.event_bus.bus") as mock_bus,
        ):
            pm.get_provider.return_value = None
            terminal_service.send_input(
                "cccc3333",
                "reply",
                sender_id="aaaa1111",
                orchestration_type=OrchestrationType.SEND_MESSAGE,
            )

        flow_calls = [
            c for c in mock_bus.publish.call_args_list if c.args and c.args[0] == "flow.message"
        ]
        assert flow_calls == []
