"""Whose work a queued message is, at the moment it is delivered (#745, criterion 14).

A message sits in the queue between two different moments: one request enqueued
it, and a status event types it into a live pane later. The owner therefore has
to be resolved at delivery, from the server's own row for the SENDER -- not from
anything the sending agent presented, and not from the receiver, whose pane is
merely where the work lands.
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.security.principal import (
    REVOKED_ENV,
    Principal,
    revocation,
)
from cli_agent_orchestrator.services.inbox_service import InboxService

OWNER = Principal(subject="auth0|member", issuer="https://idp.example/")
OTHER = Principal(subject="auth0|colleague", issuer="https://idp.example/")


@pytest.fixture(autouse=True)
def _clean_revocations(monkeypatch):
    monkeypatch.delenv(REVOKED_ENV, raising=False)
    revocation.reset()
    yield
    revocation.reset()


def _message(id=1, sender_id="sender-1", message="hello"):
    return InboxMessage(
        id=id,
        sender_id=sender_id,
        receiver_id="term-1",
        message=message,
        status=MessageStatus.PENDING,
        created_at=datetime.now(),
    )


def _rows(mapping):
    """``get_terminal_metadata`` stub: terminal id -> recorded owner id."""

    def _get(terminal_id):
        if terminal_id not in mapping:
            return None
        return {"id": terminal_id, "owner": mapping[terminal_id]}

    return _get


@patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
@patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
@patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
@patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
@patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
def test_a_revoked_senders_message_is_held_not_typed(
    mock_get, mock_monitor, mock_term_svc, mock_update, mock_meta
):
    mock_get.return_value = [_message()]
    mock_monitor.get_status.return_value = TerminalStatus.IDLE
    mock_meta.side_effect = _rows({"sender-1": OWNER.id})
    revocation.revoke(OWNER)

    InboxService().deliver_pending("term-1")

    mock_term_svc.send_input.assert_not_called()


@patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
@patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
@patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
@patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
@patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
def test_a_held_message_keeps_its_pending_status(
    mock_get, mock_monitor, mock_term_svc, mock_update, mock_meta
):
    """Held, not failed.

    FAILED is a claim about the message -- that delivery was attempted and did
    not work. Nothing was attempted here, and a reinstated owner's message must
    still be deliverable, so the row is left exactly as it was.
    """
    mock_get.return_value = [_message()]
    mock_monitor.get_status.return_value = TerminalStatus.IDLE
    mock_meta.side_effect = _rows({"sender-1": OWNER.id})
    revocation.revoke(OWNER)

    InboxService().deliver_pending("term-1")

    mock_update.assert_not_called()


@patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
@patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
@patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
@patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
@patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
def test_reinstating_the_owner_delivers_the_same_message(
    mock_get, mock_monitor, mock_term_svc, mock_update, mock_meta
):
    """The point of holding rather than failing, demonstrated end to end."""
    mock_get.return_value = [_message()]
    mock_monitor.get_status.return_value = TerminalStatus.IDLE
    mock_meta.side_effect = _rows({"sender-1": OWNER.id})
    svc = InboxService()

    revocation.revoke(OWNER)
    svc.deliver_pending("term-1")
    revocation.reinstate(OWNER)
    svc.deliver_pending("term-1")

    mock_term_svc.send_input.assert_called_once_with("term-1", "hello")


@patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
@patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
@patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
@patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
@patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
def test_one_revoked_sender_does_not_hold_the_rest_of_the_batch(
    mock_get, mock_monitor, mock_term_svc, mock_update, mock_meta
):
    """A drain (``num_messages=0``) spans senders.

    Dropping the whole batch because one sender's owner was revoked would make
    one removed member's queued message stall every other member's.
    """
    mock_get.return_value = [
        _message(id=1, sender_id="revoked-term", message="from revoked"),
        _message(id=2, sender_id="ok-term", message="from colleague"),
    ]
    mock_monitor.get_status.return_value = TerminalStatus.IDLE
    mock_meta.side_effect = _rows({"revoked-term": OWNER.id, "ok-term": OTHER.id})
    revocation.revoke(OWNER)

    InboxService().deliver_pending("term-1", num_messages=0)

    mock_term_svc.send_input.assert_called_once_with("term-1", "from colleague")
    mock_update.assert_called_once_with(2, MessageStatus.DELIVERED)


@patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
@patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
@patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
@patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
@patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
def test_repeated_senders_in_one_batch_cost_one_lookup_each(
    mock_get, mock_monitor, mock_term_svc, mock_update, mock_meta
):
    mock_get.return_value = [_message(id=i, sender_id="sender-1") for i in (1, 2, 3)]
    mock_monitor.get_status.return_value = TerminalStatus.IDLE
    mock_meta.side_effect = _rows({"sender-1": OTHER.id})
    revocation.revoke(OWNER)

    InboxService().deliver_pending("term-1", num_messages=0)

    assert mock_meta.call_count == 1


@patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
@patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
@patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
@patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
@patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
def test_no_revocations_means_no_ownership_lookup_at_all(
    mock_get, mock_monitor, mock_term_svc, mock_update, mock_meta
):
    """The fast path, pinned.

    Delivery is on the hot path for every terminal that goes IDLE. A single-user
    installation with nothing revoked must not pay a DB read per delivery to
    learn something that cannot change the outcome.
    """
    mock_get.return_value = [_message()]
    mock_monitor.get_status.return_value = TerminalStatus.IDLE

    InboxService().deliver_pending("term-1")

    mock_meta.assert_not_called()
    mock_term_svc.send_input.assert_called_once_with("term-1", "hello")


@patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
@patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
@patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
@patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
@patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
def test_a_sender_with_no_recorded_owner_is_unknown_not_revoked(
    mock_get, mock_monitor, mock_term_svc, mock_update, mock_meta
):
    """Terminals created before the owner column existed keep working."""
    mock_get.return_value = [_message()]
    mock_monitor.get_status.return_value = TerminalStatus.IDLE
    mock_meta.side_effect = _rows({"sender-1": None})
    revocation.revoke(OWNER)

    InboxService().deliver_pending("term-1")

    mock_term_svc.send_input.assert_called_once_with("term-1", "hello")


@patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
@patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
@patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
@patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
@patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
def test_a_deleted_sender_terminal_does_not_block_delivery(
    mock_get, mock_monitor, mock_term_svc, mock_update, mock_meta
):
    """The sender row can be gone: flow recycling deletes terminals by session
    while their messages are still queued. That is not a revocation."""
    mock_get.return_value = [_message()]
    mock_monitor.get_status.return_value = TerminalStatus.IDLE
    mock_meta.side_effect = _rows({})
    revocation.revoke(OWNER)

    InboxService().deliver_pending("term-1")

    mock_term_svc.send_input.assert_called_once_with("term-1", "hello")


@patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
@patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
@patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
@patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
@patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
def test_an_unparseable_owner_is_logged_and_treated_as_unknown(
    mock_get, mock_monitor, mock_term_svc, mock_update, mock_meta, caplog
):
    """Delivery is not the place to fail closed.

    A schedule that refuses to fire can be retried; a message that raises here
    would take down the delivery of every other message in the same drain. The
    value is unusable either way, so it is reported and treated as unknown.
    """
    mock_get.return_value = [_message()]
    mock_monitor.get_status.return_value = TerminalStatus.IDLE
    mock_meta.side_effect = _rows({"sender-1": "malformed-no-separator"})
    revocation.revoke(OWNER)

    with caplog.at_level("WARNING"):
        InboxService().deliver_pending("term-1")

    mock_term_svc.send_input.assert_called_once_with("term-1", "hello")
    assert any("unreadable owner" in record.getMessage() for record in caplog.records)


@patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
@patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
@patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
@patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
@patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
def test_the_gate_reads_the_sender_not_the_receiver(
    mock_get, mock_monitor, mock_term_svc, mock_update, mock_meta
):
    """Revoking the RECEIVER's owner must not hold the message.

    The receiver's pane is where the work lands, not whose work it is. Reading
    the receiver here would let one revoked supervisor silence the reports of
    every worker still authorized to send them.
    """
    mock_get.return_value = [_message()]
    mock_monitor.get_status.return_value = TerminalStatus.IDLE
    mock_meta.side_effect = _rows({"sender-1": OTHER.id, "term-1": OWNER.id})
    revocation.revoke(OWNER)

    InboxService().deliver_pending("term-1")

    mock_term_svc.send_input.assert_called_once_with("term-1", "hello")
    assert [c.args[0] for c in mock_meta.call_args_list] == ["sender-1"]
