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
    def test_an_arbitrary_label_is_refused_not_treated_as_unowned(self, client):
        """A label outside the closed operator set must not slip through as
        "attributed to no principal" — that was a way for a revoked agent to skip
        the delivery-time owner gate entirely (Copilot review on #802)."""
        with patch("cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=None):
            resp = client.post(
                "/terminals/abcdef12/inbox/messages",
                params={"sender_id": "forged", "message": "hi"},
            )
        assert resp.status_code == 404

    def test_an_id_shaped_sender_that_does_not_exist_is_rejected_with_404(self, client):
        with patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=None
        ) as meta:
            resp = client.post(
                "/terminals/abcdef12/inbox/messages",
                params={"sender_id": "deadbeef", "message": "hi"},
            )
        assert resp.status_code == 404
        assert "neither an existing terminal" in resp.json()["detail"]
        meta.assert_called_once_with("deadbeef")

    def test_an_operator_label_is_allowed_and_never_looked_up(self, client):
        """Operator surfaces post a LABEL, not an id — ``app_tools`` sends
        "operator", which has no terminal context at all. Requiring a row for it
        would 404 the whole operator path, and it cannot impersonate a terminal's
        owner: the owner lookup finds nothing, so the message is attributed to no
        principal (the pre-existing unowned behaviour)."""
        msg = SimpleNamespace(
            id=1, sender_id="operator", receiver_id="abcdef12", created_at=datetime.now()
        )
        with (
            patch("cli_agent_orchestrator.api.main.get_terminal_metadata") as meta,
            patch("cli_agent_orchestrator.api.main.create_inbox_message", return_value=msg),
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
        ):
            resp = client.post(
                "/terminals/abcdef12/inbox/messages",
                params={"sender_id": "operator", "message": "hi"},
            )
        assert resp.status_code == 200
        # A non-id label is never a terminal lookup.
        meta.assert_not_called()

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


class TestTheSenderIsBoundToTheAuthenticatedCaller:
    """Existence is not authorization (Copilot, raised repeatedly on #802).

    A caller naming ANOTHER live terminal made the delivery-time owner gate
    evaluate that terminal's principal. When an authenticated identity is
    available the two are now bound; with auth off there is no identity to bind
    to, and the broker gateway already overwrites sender_id with the lease
    identity before the request reaches here.
    """

    def test_a_foreign_sender_is_refused_when_authenticated(self, client):
        with (
            patch("cli_agent_orchestrator.api.main.is_auth_enabled", return_value=True),
            patch(
                "cli_agent_orchestrator.api.main.get_terminal_metadata",
                return_value={"id": "beefcafe", "owner": "auth0|someone-else"},
            ),
            patch("cli_agent_orchestrator.api.main.create_inbox_message") as create,
        ):
            resp = client.post(
                "/terminals/abcdef12/inbox/messages",
                params={"sender_id": "beefcafe", "message": "hi"},
            )
        assert resp.status_code == 403
        assert "owned by another principal" in resp.json()["detail"]
        create.assert_not_called()

    def test_a_sender_the_caller_owns_is_accepted(self, client):
        from cli_agent_orchestrator.security.principal import LOCAL_PRINCIPAL

        msg = SimpleNamespace(
            id=1, sender_id="beefcafe", receiver_id="abcdef12", created_at=datetime.now()
        )
        with (
            patch("cli_agent_orchestrator.api.main.is_auth_enabled", return_value=True),
            patch(
                "cli_agent_orchestrator.api.main.get_terminal_metadata",
                return_value={"id": "beefcafe", "owner": LOCAL_PRINCIPAL.id},
            ),
            patch("cli_agent_orchestrator.api.main.create_inbox_message", return_value=msg),
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
        ):
            resp = client.post(
                "/terminals/abcdef12/inbox/messages",
                params={"sender_id": "beefcafe", "message": "hi"},
            )
        assert resp.status_code == 200

    def test_an_unowned_sender_is_still_allowed(self, client):
        """A terminal with no recorded owner predates ownership or is operator
        created; refusing it would break those, and it grants no other identity."""
        msg = SimpleNamespace(
            id=1, sender_id="beefcafe", receiver_id="abcdef12", created_at=datetime.now()
        )
        with (
            patch("cli_agent_orchestrator.api.main.is_auth_enabled", return_value=True),
            patch(
                "cli_agent_orchestrator.api.main.get_terminal_metadata",
                return_value={"id": "beefcafe", "owner": None},
            ),
            patch("cli_agent_orchestrator.api.main.create_inbox_message", return_value=msg),
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
        ):
            resp = client.post(
                "/terminals/abcdef12/inbox/messages",
                params={"sender_id": "beefcafe", "message": "hi"},
            )
        assert resp.status_code == 200
