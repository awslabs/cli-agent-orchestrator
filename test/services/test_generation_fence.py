"""Tests for the W13 generation fence (T-RP-7 fork side, cond-0054 fixture)."""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import generation_fence as gf


def _request(**changes):
    request = {
        "schema": gf.FENCE_REQUEST_SCHEMA,
        "terminal_generation": "gen-000042",
        "obligation_generation": "obgen-7c2e4a1b",
        "attempt_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "intent_id": "0f8fad5a-1c87-4d3e-9b96-1b6b2c8e5f10",
        "report_sha256": "a" * 64,
    }
    request.update(changes)
    return request


@pytest.fixture
def store(tmp_path):
    return tmp_path / "companion"


def test_install_then_already_fenced_idempotent(store):
    first = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    assert first["outcome"] == gf.OUTCOME_FENCED
    assert isinstance(first["fence_receipt_sha256"], str)
    second = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    assert second["outcome"] == gf.OUTCOME_ALREADY_FENCED
    # Crash-after-CAS-before-response reconciliation: identical receipt.
    assert second["fence_receipt_sha256"] == first["fence_receipt_sha256"]


def test_distinct_intent_single_use_violation_refused(store):
    gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    with pytest.raises(gf.FenceRequestError):
        gf.install_fence(
            store,
            terminal_id="a1b2c3d4",
            generation="gen-000042",
            vintage="v2",
            request=_request(intent_id="3d813cbb-47fb-42ba-91df-831e1593ac29"),
            fencing_token_id="token-1",
        )
    with pytest.raises(gf.FenceRequestError):
        gf.install_fence(
            store,
            terminal_id="a1b2c3d4",
            generation="gen-000042",
            vintage="v2",
            request=_request(report_sha256="b" * 64),
            fencing_token_id="token-1",
        )


def test_unknown_generation_outcome(store):
    response = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(terminal_generation="gen-999999"),
        fencing_token_id="token-1",
    )
    assert response["outcome"] == gf.OUTCOME_UNKNOWN_GENERATION
    assert response["fence_receipt_sha256"] is None


def test_vintage_mismatch_outcome(store):
    response = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v1",
        request=_request(),
        fencing_token_id="token-1",
    )
    assert response["outcome"] == gf.OUTCOME_VINTAGE_MISMATCH


def test_superseded_generation_outcome(store):
    response = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
        superseded=True,
    )
    assert response["outcome"] == gf.OUTCOME_SUPERSEDED


def test_fenced_generation_rejects_input_admission(store):
    gf.assert_admission_open(store, "a1b2c3d4", "gen-000042")  # open before fence
    gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    # The cond-0054 fixture: queued unsubmitted input is rejected at the
    # admission boundary, and a post-report same-turn tool call is prevented.
    with pytest.raises(gf.FencedError):
        gf.assert_admission_open(store, "a1b2c3d4", "gen-000042")


def test_verify_fence_freshness(store):
    response = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    assert gf.verify_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        expected_receipt_sha256=response["fence_receipt_sha256"],
    )
    assert not gf.verify_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        expected_receipt_sha256="0" * 64,
    )


def test_lost_fence_detectable_and_reinstall_idempotent(store):
    first = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    gf.fence_state_path(store, "a1b2c3d4", "gen-000042").unlink()  # simulated loss
    assert not gf.verify_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        expected_receipt_sha256=first["fence_receipt_sha256"],
    )
    second = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    assert second["outcome"] == gf.OUTCOME_FENCED
    # The receipt digest is stable because it covers the receipt object
    # only; installed_at is re-minted after a genuine loss, so the caller
    # re-records the new digest (final-verified freshness).
    assert gf.verify_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        expected_receipt_sha256=second["fence_receipt_sha256"],
    )


def test_request_validation(store):
    with pytest.raises(gf.FenceRequestError):
        gf.install_fence(
            store,
            terminal_id="a1b2c3d4",
            generation="gen-000042",
            vintage="v2",
            request=_request(schema="cao-w13-fence-req-v0"),
            fencing_token_id="token-1",
        )
    with pytest.raises(gf.FenceRequestError):
        gf.install_fence(
            store,
            terminal_id="a1b2c3d4",
            generation="gen-000042",
            vintage="v2",
            request=_request(report_sha256="not-hex"),
            fencing_token_id="token-1",
        )


def test_seal_intent_validation():
    gf.validate_seal_intent(
        {
            "schema": gf.SEAL_INTENT_SCHEMA,
            "project": "p",
            "task_id": "t",
            "run_id": "r",
            "terminal_generation": "gen-000042",
            "obligation_generation": "obgen-1",
            "attempt_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
            "report_sha256": "a" * 64,
            "intent_id": "0f8fad5a-1c87-4d3e-9b96-1b6b2c8e5f10",
            "at": "2026-07-23T12:00:00Z",
        }
    )
    with pytest.raises(gf.FenceRequestError):
        gf.validate_seal_intent({"schema": gf.SEAL_INTENT_SCHEMA})
