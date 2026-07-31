from datetime import datetime, timezone

import pytest

from cli_agent_orchestrator.services import model_turn_receipt_contract as contract


def _receipt():
    return contract.build_receipt(
        message_id=7,
        message_sha256="a" * 64,
        message_created_at=datetime(2026, 7, 30, 12, 0, 0, 123456, tzinfo=timezone.utc),
        sender_id="supervisor",
        sender_generation="supervisor-generation",
        receiver_id="worker",
        receiver_generation="worker-generation",
        provider="codex",
        provider_session_id="session",
        provider_turn_id="turn",
        submitted_at=datetime(2026, 7, 30, 12, 0, 1, 654321, tzinfo=timezone.utc),
    )


def test_builder_emits_exact_strict_v1_contract():
    receipt = _receipt()
    assert tuple(receipt) == contract.FIELDS
    assert receipt["schema"] == "cao-model-turn-receipt-v1"
    assert receipt["source"] == "provider-adapter"
    assert receipt["message_created_at"].endswith(".123456Z")
    assert receipt["submitted_at"].endswith(".654321Z")


def test_unknown_or_missing_fields_fail_closed():
    extra = {**_receipt(), "extension": "not-v1"}
    with pytest.raises(contract.ReceiptValidationError):
        contract.validate_receipt(extra)
    missing = _receipt()
    missing.pop("sender_generation")
    with pytest.raises(contract.ReceiptValidationError):
        contract.validate_receipt(missing)
