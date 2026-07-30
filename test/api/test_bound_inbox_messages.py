from datetime import datetime

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus
from cli_agent_orchestrator.services import bound_inbox_message


def _body():
    return {
        "operation_id": "operation-1",
        "sender_id": "supervisor",
        "sender_generation": "supervisor-generation",
        "message": "ordinary update",
        "message_sha256": "a" * 64,
        "expected_receiver_generation": "worker-generation",
        "expected_provider_session_id": "provider-session",
        "expected_execution_mode": "acp",
    }


def _result():
    return bound_inbox_message.BoundInboxResult(
        message=InboxMessage(
            id=7,
            sender_id="supervisor",
            receiver_id="abcdef12",
            message="ordinary update",
            status=MessageStatus.PENDING,
            created_at=datetime(2026, 7, 30, 12, 0, 0),
            operation_id="operation-1",
            message_sha256=_body()["message_sha256"],
            sender_generation="supervisor-generation",
            expected_receiver_generation="worker-generation",
            expected_provider_session_id="provider-session",
            expected_execution_mode="acp",
        ),
        replayed=False,
    )


def test_bound_endpoint_returns_the_server_operation_receipt(client, monkeypatch):
    monkeypatch.setattr(bound_inbox_message, "enqueue", lambda *_: _result())
    monkeypatch.setattr(
        "cli_agent_orchestrator.api.main.inbox_service.deliver_pending",
        lambda *_args, **_kwargs: None,
    )
    response = client.post("/terminals/abcdef12/inbox/bound-messages", json=_body())
    assert response.status_code == 200
    assert response.json()["operation_id"] == "operation-1"
    assert response.json()["receiver_generation"] == "worker-generation"
    assert response.json()["provider_session_id"] == "provider-session"


def test_identity_conflict_is_typed_zero_byte_refusal(client, monkeypatch):
    def refuse(*_):
        raise bound_inbox_message.BoundInboxConflict("replacement")

    monkeypatch.setattr(bound_inbox_message, "enqueue", refuse)
    response = client.post("/terminals/abcdef12/inbox/bound-messages", json=_body())
    assert response.status_code == 409
    assert response.json()["outcome"] == "refused"
    assert response.json()["proven_zero_bytes"] is True
    assert response.json()["receiver_id"] == "abcdef12"
