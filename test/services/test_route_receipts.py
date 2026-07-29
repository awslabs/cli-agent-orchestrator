"""Provider-generated authenticated route receipts (cond-0069 closure).

The bridge-published receipt is the capability surface's only route
authority provenance: HMAC-authenticated with the generation-private key,
immutably published at its content address, and validated against the
authority boundary's pinned route and journaled model-input digests.
Malformed, drifted, tampered, or unjournaled evidence exposes nothing.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from cli_agent_orchestrator.services import route_receipts
from cli_agent_orchestrator.services.delivery_journal import DeliveryJournal


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


@pytest.fixture
def harness(tmp_path):
    """A state dir, a live reservation's expectation, and its journal."""
    state_dir = tmp_path / "recovery"
    generation = str(uuid.uuid4())
    obligation = "obgen-7c2e4a1b"
    root = tmp_path / "managed-provider-sessions" / str(uuid.uuid4())
    journal = DeliveryJournal(root / "delivery-journal.db")
    command = {"op": "admit", "delivery_id": "delivery-1", "message": "run the task"}
    digest = hashlib.sha256(_canonical(command)).hexdigest()
    journal.open_intent(obligation, "delivery-1", digest)
    journal.mark_terminal_queued(obligation, "delivery-1")
    expectations = {"codex": {"generation": generation, "model": "gpt-5.6-sol", "effort": "xhigh"}}
    digests = {"codex": route_receipts.journaled_request_digests(root, obligation)}
    return {
        "state_dir": state_dir,
        "generation": generation,
        "root": root,
        "digest": digest,
        "expectations": expectations,
        "digests": digests,
    }


def _write(harness, **changes):
    args = {
        "state_dir": harness["state_dir"],
        "provider": "codex",
        "native_session_id": "thr_0192a7b4",
        "native_turn_id": "turn-1",
        "generation": harness["generation"],
        "terminal_id": "a1b2c3d4",
        "delivery_id": "delivery-1",
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "observed_model": "gpt-5.6-sol",
        "observed_effort": "xhigh",
        "protocol": "app-server/1",
        "event_sequence": 1,
        "model_input_digest": harness["digest"],
        "provider_version": "codex 0.146.0",
    }
    args.update(changes)
    return route_receipts.write_route_receipt(**args)


def _load(harness):
    return route_receipts.load_valid_route_proofs(
        state_dir=harness["state_dir"],
        expected_routes=harness["expectations"],
        expected_input_digests=harness["digests"],
    )


def test_written_receipt_loads_as_valid_proof(harness):
    receipt = _write(harness)
    proofs = _load(harness)
    assert proofs == {"codex": receipt}


def test_receipt_file_is_immutable_and_content_addressed(harness):
    receipt = _write(harness)
    published = list(harness["state_dir"].glob("route-receipt.*.json"))
    assert len(published) == 1
    raw = published[0].read_bytes()
    assert published[0].name == f"route-receipt.{hashlib.sha256(raw).hexdigest()[:16]}.json"
    assert json.loads(raw)["receipt_hmac"] == receipt["receipt_hmac"]


def test_writer_refuses_a_receipt_that_fails_its_own_contract(harness):
    with pytest.raises(route_receipts.RouteReceiptError):
        _write(harness, event_sequence=0)
    with pytest.raises(route_receipts.RouteReceiptError):
        _write(harness, observed_model="different-model")
    with pytest.raises(route_receipts.RouteReceiptError):
        _write(harness, model_input_digest="not-a-digest")
    assert list(harness["state_dir"].glob("route-receipt.*.json")) == []


def test_tampered_receipt_exposes_no_authority(harness):
    _write(harness)
    published = list(harness["state_dir"].glob("route-receipt.*.json"))[0]
    receipt = json.loads(published.read_bytes())
    # Drift the observed model after publication: HMAC no longer verifies.
    receipt["observed_model"] = "different-model"
    import os

    os.chmod(published, 0o600)  # published receipts are 0400 by design
    published.write_bytes(json.dumps(receipt, sort_keys=True).encode() + b"\n")
    assert _load(harness) == {}


def test_hand_authored_file_off_content_address_exposes_nothing(harness):
    harness["state_dir"].mkdir(parents=True, exist_ok=True)
    forged = {
        "schema": "cao-route-receipt-v1",
        "provider": "codex",
        "native_session_id": "thr_forge",
        "native_turn_id": "turn-forge",
        "generation": harness["generation"],
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "observed_model": "gpt-5.6-sol",
        "observed_effort": "xhigh",
        "protocol_version": "app-server/1",
        "event_sequence": 1,
        "model_input_digest": harness["digest"],
        "non_echo": True,
        "provider_version": "codex 0.146.0",
        "receipt_hmac": "0" * 64,
    }
    (harness["state_dir"] / "route-receipt.hand-authored.json").write_text(
        json.dumps(forged), encoding="utf-8"
    )
    assert _load(harness) == {}


def test_generation_mismatch_and_missing_key_expose_nothing(harness):
    _write(harness)
    # A receipt for a different generation than the live reservation's.
    other = dict(harness["expectations"])
    other["codex"] = {**other["codex"], "generation": str(uuid.uuid4())}
    assert (
        route_receipts.load_valid_route_proofs(
            state_dir=harness["state_dir"],
            expected_routes=other,
            expected_input_digests=harness["digests"],
        )
        == {}
    )
    # Without the generation key the HMAC can never verify.
    key_file = harness["state_dir"] / "route-keys" / f"{harness['generation']}.key"
    key_file.unlink()
    assert _load(harness) == {}


def test_version_drift_and_unjournaled_digest_expose_nothing(harness):
    _write(harness)
    drifted = dict(harness["expectations"])
    assert (
        route_receipts.load_valid_route_proofs(
            state_dir=harness["state_dir"],
            expected_routes=drifted,
            expected_input_digests={"codex": frozenset({"f" * 64})},
        )
        == {}
    )
    # Installed-version drift invalidates the receipt for the pinned binary.
    _write(harness, provider_version="codex 0.144.6", native_turn_id="turn-drifted")
    # A receipt written for a different (unjournaled) input digest never
    # gains authority even though the writer self-check passed.
    _write(harness, model_input_digest="a" * 64, native_turn_id="turn-2")
    proofs = _load(harness)
    assert set(proofs) == {"codex"}
    assert proofs["codex"]["native_turn_id"] == "turn-1"


def test_missing_evidence_exposes_nothing(harness):
    assert _load(harness) == {}
    assert (
        route_receipts.journaled_request_digests(harness["root"] / "absent", "obgen-7c2e4a1b")
        == frozenset()
    )
