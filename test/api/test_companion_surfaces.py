"""End-to-end API/service tests for the structured companion surfaces
(final conformance §20.2f P1-7/P1-10): exact terminal/process generation,
provider version/session/turn, per-turn route identity, redaction, response
loss, and stale/wrong-generation rejection — through the real HTTP surface,
never unit-injected sentinel boundaries.
"""

import pytest

from cli_agent_orchestrator.services import companion_receipts, terminal_service

GEN = "11111111-1111-4111-8111-111111111111"
OTHER_GEN = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def companion(tmp_path, monkeypatch):
    store = tmp_path / "companion"
    store.mkdir()
    monkeypatch.setattr(companion_receipts, "COMPANION_DIR", store)
    monkeypatch.setattr(
        terminal_service,
        "get_terminal_metadata",
        lambda tid: {"generation": GEN} if tid == "deadbeef" else None,
    )
    return store


def test_route_surface_exact_generation(client, companion):
    assert client.get("/terminals/deadbeef/route").status_code == 204
    companion_receipts.record_route_receipt(
        "deadbeef",
        GEN,
        provider="codex",
        model="gpt-5.6-sol",
        effort="max",
        receipt_id="thread-1",
        turn_id="turn-1",
        provider_version="0.146.0",
    )
    response = client.get("/terminals/deadbeef/route")
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "codex"
    assert body["model"] == "gpt-5.6-sol"
    assert body["effort"] == "max"
    assert body["receipt_id"] == "thread-1"
    assert body["turn_id"] == "turn-1"
    assert body["provider_version"] == "0.146.0"
    assert body["generation"] == GEN
    # unknown terminal: no observation, never an error
    assert client.get("/terminals/cafebabe/route").status_code == 204


def test_route_surface_rejects_stale_generation(client, companion, monkeypatch):
    companion_receipts.record_route_receipt(
        "deadbeef",
        GEN,
        provider="codex",
        model="m",
        effort="e",
        receipt_id="r",
        turn_id="t",
    )
    # the terminal id now names a replacement incarnation
    monkeypatch.setattr(
        terminal_service, "get_terminal_metadata", lambda tid: {"generation": OTHER_GEN}
    )
    assert client.get("/terminals/deadbeef/route").status_code == 204


def test_user_prompt_surface_lifecycle(client, companion):
    assert client.get("/terminals/deadbeef/user-prompt").status_code == 204
    companion_receipts.record_prompt(
        "deadbeef",
        GEN,
        prompt_id="p1",
        text="Trust this project?",
        choices=["Yes", "No"],
    )
    response = client.get("/terminals/deadbeef/user-prompt")
    assert response.status_code == 200
    assert response.json()["prompt_id"] == "p1"
    assert response.json()["choices"] == ["Yes", "No"]
    companion_receipts.clear_prompt("deadbeef", GEN, prompt_id="p1")
    assert client.get("/terminals/deadbeef/user-prompt").status_code == 204


def test_refusal_surface(client, companion):
    assert client.get("/terminals/deadbeef/refusal").status_code == 204
    companion_receipts.record_refusal(
        "deadbeef",
        GEN,
        refusal_id="r1",
        identity="This content cannot be shown",
        turn_id="turn-9",
    )
    response = client.get("/terminals/deadbeef/refusal")
    assert response.status_code == 200
    body = response.json()
    assert body["refusal_id"] == "r1"
    assert body["identity"] == "This content cannot be shown"
    assert body["turn_id"] == "turn-9"
    assert body["generation"] == GEN


def test_message_turn_receipt_surface_and_redaction(client, companion):
    url = "/terminals/deadbeef/inbox/messages/msg-1/turn-receipt"
    assert client.get(url).status_code == 204
    companion_receipts.record_message_ack(
        "deadbeef",
        GEN,
        message_id="msg-1",
        ack={
            "kind": "submitted",
            "message_id": "msg-1",
            "message_sha256": "a" * 64,
            "sender_id": "beeffeed",
            "receiver_id": "deadbeef",
            "receiver_generation": GEN,
            "provider": "codex",
            "provider_session_id": "thread-1",
            "provider_turn_id": "turn-1",
            "submitted_at": "2026-01-01T00:00:00Z",
        },
    )
    response = client.get(url)
    assert response.status_code == 200
    body = response.json()
    assert body["message_id"] == "msg-1"
    assert body["provider_turn_id"] == "turn-1"
    assert body["provider_session_id"] == "thread-1"
    assert body["receiver_generation"] == GEN
    # redaction: the message body is never carried by the ack surface
    assert "message" not in body
    assert client.get("/terminals/deadbeef/inbox/messages/msg-2/turn-receipt").status_code == 204


def test_turn_receipt_serves_a_terminal_wake_receipt_when_no_managed_ack(
    client, companion, tmp_path, monkeypatch
):
    from cli_agent_orchestrator.services import wake_receipts

    monkeypatch.setattr(wake_receipts, "WAKE_RECEIPT_DIR", tmp_path / "wake")
    url = "/terminals/deadbeef/inbox/messages/msg-w/turn-receipt"
    # No record yet: 204 (no false close).
    assert client.get(url).status_code == 204
    # watching: still 204 — an open obligation stays observable, not terminal.
    wake_receipts.ensure_watching(
        "deadbeef",
        "msg-w",
        native_session_id=None,
        delivered_at="2026-07-26T12:00:00+00:00",
        deadline_at="2026-07-26T12:00:45+00:00",
    )
    assert client.get(url).status_code == 204
    # terminal wake_confirmed: served, with status-transition provenance and
    # never a provider-native claim.
    wake_receipts.record_wake_confirmed("deadbeef", "msg-w", observed={"to_status": "processing"})
    body = client.get(url).json()
    assert body["message_id"] == "msg-w"
    assert body["state"] == "wake_confirmed"
    assert body["source"] == "status-transition"


def test_managed_ack_is_preferred_over_the_wake_sidecar(client, companion, tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import wake_receipts

    monkeypatch.setattr(wake_receipts, "WAKE_RECEIPT_DIR", tmp_path / "wake")
    # Both a wake sidecar and a managed provider-native ack exist for msg-1.
    wake_receipts.ensure_watching(
        "deadbeef",
        "msg-1",
        native_session_id=None,
        delivered_at="2026-07-26T12:00:00+00:00",
        deadline_at="2026-07-26T12:00:45+00:00",
    )
    wake_receipts.record_wake_confirmed("deadbeef", "msg-1", observed={"to_status": "processing"})
    companion_receipts.record_message_ack(
        "deadbeef",
        GEN,
        message_id="msg-1",
        ack={"kind": "submitted", "message_id": "msg-1", "provider_turn_id": "turn-1"},
    )
    body = client.get("/terminals/deadbeef/inbox/messages/msg-1/turn-receipt").json()
    # The managed provider-native acknowledgement outranks the wake sidecar.
    assert body.get("kind") == "submitted"
    assert body.get("provider_turn_id") == "turn-1"
    assert body.get("state") is None  # the wake record's field is absent


def test_corrupt_record_is_response_loss_fail_closed(client, companion):
    companion_receipts.record_route_receipt(
        "deadbeef",
        GEN,
        provider="p",
        model="m",
        effort="e",
        receipt_id="r",
        turn_id="t",
    )
    path = companion_receipts._record_path("deadbeef", GEN)
    path.write_text("{ corrupt")
    assert client.get("/terminals/deadbeef/route").status_code == 204
