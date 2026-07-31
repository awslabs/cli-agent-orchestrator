"""Store-level contract for the generation-bound companion receipts
(final conformance §20.2f P1-7/P1-10)."""

from datetime import datetime, timezone

import pytest

from cli_agent_orchestrator.services import (
    companion_receipts,
    model_turn_receipt_contract,
)

GEN = "11111111-1111-4111-8111-111111111111"
OTHER_GEN = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(companion_receipts, "COMPANION_DIR", tmp_path)
    return tmp_path


def test_route_receipt_generation_bound(store):
    companion_receipts.record_route_receipt(
        "term-1",
        GEN,
        provider="codex",
        model="gpt-5.6-sol",
        effort="max",
        receipt_id="thread-1",
        turn_id="turn-1",
        provider_version="0.146.0",
    )
    route = companion_receipts.get_route("term-1", GEN)
    assert route["provider"] == "codex"
    assert route["model"] == "gpt-5.6-sol"
    assert route["effort"] == "max"
    assert route["receipt_id"] == "thread-1"
    assert route["turn_id"] == "turn-1"
    # a stale/wrong generation is never served
    assert companion_receipts.get_route("term-1", OTHER_GEN) is None
    assert companion_receipts.get_route("term-1", None) is None
    assert companion_receipts.get_route("term-2", GEN) is None


def test_per_turn_route_identity_supersedes(store):
    for turn in ("turn-1", "turn-2"):
        companion_receipts.record_route_receipt(
            "term-1",
            GEN,
            provider="codex",
            model="m",
            effort="e",
            receipt_id=turn,
            turn_id=turn,
        )
    assert companion_receipts.get_route("term-1", GEN)["turn_id"] == "turn-2"


def test_prompt_lifecycle(store):
    companion_receipts.record_prompt(
        "term-1",
        GEN,
        prompt_id="p1",
        text="Trust this project?",
        choices=["Yes", "No"],
    )
    prompt = companion_receipts.get_prompt("term-1", GEN)
    assert prompt["prompt_id"] == "p1"
    assert prompt["text"] == "Trust this project?"
    assert prompt["choices"] == ["Yes", "No"]
    assert companion_receipts.get_prompt("term-1", OTHER_GEN) is None
    companion_receipts.clear_prompt("term-1", GEN, prompt_id="p1")
    assert companion_receipts.get_prompt("term-1", GEN) is None


def test_clear_prompt_only_closes_exact_prompt(store):
    companion_receipts.record_prompt("term-1", GEN, prompt_id="p1", text="t", choices=[])
    companion_receipts.clear_prompt("term-1", GEN, prompt_id="other")
    assert companion_receipts.get_prompt("term-1", GEN)["prompt_id"] == "p1"


def test_refusal_receipt_generation_bound(store):
    companion_receipts.record_refusal(
        "term-1",
        GEN,
        refusal_id="r1",
        identity="This content cannot be shown",
        turn_id="turn-9",
    )
    refusal = companion_receipts.get_refusal("term-1", GEN)
    assert refusal["refusal_id"] == "r1"
    assert refusal["identity"] == "This content cannot be shown"
    assert refusal["turn_id"] == "turn-9"
    assert companion_receipts.get_refusal("term-1", OTHER_GEN) is None


def test_message_ack_exactly_once_no_replay(store):
    ack = {
        "kind": "submitted",
        "message_id": "m1",
        "message_sha256": "a" * 64,
        "receiver_id": "term-1",
        "receiver_generation": GEN,
        "provider": "codex",
        "provider_session_id": "thread-1",
        "provider_turn_id": "turn-1",
        "submitted_at": "2026-01-01T00:00:00Z",
    }
    companion_receipts.record_message_ack("term-1", GEN, message_id="m1", ack=ack)
    # a replay (even a conflicting one) never overwrites the first exact ack
    companion_receipts.record_message_ack(
        "term-1", GEN, message_id="m1", ack={**ack, "provider_turn_id": "turn-X"}
    )
    recorded = companion_receipts.get_message_ack("term-1", GEN, "m1")
    assert recorded["provider_turn_id"] == "turn-1"
    assert companion_receipts.get_message_ack("term-1", OTHER_GEN, "m1") is None
    assert companion_receipts.get_message_ack("term-1", GEN, "m2") is None
    # redaction: no message body is ever persisted in an ack
    assert "message" not in recorded


def test_corrupt_record_fails_closed(store):
    companion_receipts.record_route_receipt(
        "term-1",
        GEN,
        provider="p",
        model="m",
        effort="e",
        receipt_id="r",
        turn_id="t",
    )
    path = companion_receipts._record_path("term-1", GEN)
    path.write_text("{ not json")
    assert companion_receipts.get_route("term-1", GEN) is None
    # and the store recovers on the next well-formed write
    companion_receipts.record_prompt("term-1", GEN, prompt_id="p", text="t", choices=[])
    assert companion_receipts.get_prompt("term-1", GEN)["prompt_id"] == "p"


def test_strict_receipt_producer_never_resets_corrupt_storage(store):
    receipt = model_turn_receipt_contract.build_receipt(
        message_id="m1",
        message_sha256="a" * 64,
        message_created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sender_id="supervisor",
        sender_generation="supervisor-generation",
        receiver_id="term-1",
        receiver_generation=GEN,
        provider="codex",
        provider_session_id="thread-1",
        provider_turn_id="turn-1",
        submitted_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
    )
    companion_receipts.record_message_ack(
        "term-1",
        GEN,
        message_id="m1",
        ack=receipt,
    )
    path = companion_receipts._record_path("term-1", GEN)
    corrupt = b'{"schema_version":4,"terminal_id":"other"}'
    path.write_bytes(corrupt)

    with pytest.raises(companion_receipts.CompanionReceiptInvalid):
        companion_receipts.record_message_ack(
            "term-1",
            GEN,
            message_id="m2",
            ack={**receipt, "message_id": "m2"},
        )
    assert path.read_bytes() == corrupt
