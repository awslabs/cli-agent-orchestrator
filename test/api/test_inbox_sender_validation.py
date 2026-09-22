"""The inbox enqueue endpoint validates the agent-supplied sender (#802).

``sender_id`` arrives as a query param, and the delivery-time owner gate (#745)
resolves whether a held message may be delivered from that sender's owner. Left
unvalidated, a revoked owner's agent could name any other live terminal as
sender and slip past the gate constraining it (guojing1217). The central server
has a row for every real terminal, so requiring one ties the attribution to
something the caller cannot invent.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestTheSenderMustBeARealTerminal:
    def test_a_forged_sender_is_rejected_with_404(self, client):
        with patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=None
        ) as meta:
            resp = client.post(
                "/terminals/abcdef12/inbox/messages",
                params={"sender_id": "ghost-sender", "message": "hi"},
            )
        assert resp.status_code == 404
        assert "Sender terminal" in resp.json()["detail"]
        meta.assert_called_once_with("ghost-sender")

    def test_a_real_sender_is_accepted_and_enqueued(self, client):
        msg = SimpleNamespace(
            id=1,
            sender_id="sender-1",
            receiver_id="abcdef12",
            created_at=datetime.now(),
        )
        with (
            patch(
                "cli_agent_orchestrator.api.main.get_terminal_metadata",
                return_value={"id": "sender-1"},
            ),
            patch(
                "cli_agent_orchestrator.api.main.create_inbox_message", return_value=msg
            ) as create,
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
        ):
            resp = client.post(
                "/terminals/abcdef12/inbox/messages",
                params={"sender_id": "sender-1", "message": "hi"},
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        create.assert_called_once()
