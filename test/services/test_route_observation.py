"""COND-0230 M10-C dark route-observation transaction core.

Every test here is about the durable journal only. Nothing observes a provider
surface, closes a modal, issues pane input, attaches a consumer, or wakes
anybody: the point of the slice is that the record — including the four ordered
provider-effect stage facts — is trustworthy *before* any effect lane is
allowed to act on it.
"""

from __future__ import annotations

import json
import uuid

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services import route_observation as ro
from cli_agent_orchestrator.services.canonical_json import canonical_sha256

ARTIFACT = "a" * 64

#: Stage facts are bounded canonical bindings — never raw pane text.
_PRE_PROBE = {"kind": "pre-probe-intent", "surface": "status-v1"}
_OBSERVATION = {"kind": "provider-surface", "observed_at": "2026-08-16T00:00:00Z"}
_PRE_CLOSE = {"kind": "pre-close-intent", "close": "escape"}
_CLOSE_PROOF = {"kind": "owned-close", "outcome": "closed", "closed_at": "2026-08-16T00:00:01Z"}

_EVENT = {"stated_by": "probe"}


def _request(
    operation_id=None,
    *,
    target_terminal_id="term-target",
    target_generation="gen-target",
    native_session_id="sess-1",
    provider="codex",
    provider_version="0.147.0",
    provider_artifact_sha256=ARTIFACT,
    requester_terminal_id="term-requester",
    requester_generation="gen-requester",
):
    return ro.RouteObservationRequest(
        operation_id=operation_id or str(uuid.uuid4()),
        target_terminal_id=target_terminal_id,
        target_generation=target_generation,
        native_session_id=native_session_id,
        provider=provider,
        provider_version=provider_version,
        provider_artifact_sha256=provider_artifact_sha256,
        requester_terminal_id=requester_terminal_id,
        requester_generation=requester_generation,
    )


def _stage(request, *, db=None, **overrides):
    """Walk the four ordered effect-stage gates to positive-completion ready."""
    ro.pre_probe(request, intent=overrides.get("pre_probe", _PRE_PROBE), db=db)
    ro.record_observation(request, observation=overrides.get("observation", _OBSERVATION), db=db)
    ro.pre_close(request, intent=overrides.get("pre_close", _PRE_CLOSE), db=db)
    ro.record_close_proof(request, proof=overrides.get("close_proof", _CLOSE_PROOF), db=db)


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    return isolated_memory_db


# ---------------------------------------------------------------------------
# the capability is off, and the vocabulary is closed
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_the_capability_is_statelessly_disabled_in_this_build(self):
        assert ro.enabled() is False

    def test_the_terminal_vocabulary_is_closed_at_three_values(self):
        assert ro.TERMINAL_RESULTS == {
            ro.RESULT_OBSERVED_CLOSED,
            ro.RESULT_ZERO_EFFECT_REFUSAL,
            ro.RESULT_AMBIGUOUS_AFTER_POSSIBLE_EFFECT,
        }
        #: one canonical nonterminal state
        assert ro.STATE_REQUESTED not in ro.TERMINAL_RESULTS

    def test_a_record_never_carries_a_dispatched_or_updated_state(self):
        record = ro.claim(_request())
        assert "dispatch_state" not in record
        assert "updated_at" in record  # only a stage write or terminal transition sets it
        for field in ro.STAGE_FACT_FIELDS:
            assert record[field] is None


# ---------------------------------------------------------------------------
# the deterministic canonical request binding
# ---------------------------------------------------------------------------


class TestRequestBinding:
    def test_the_same_request_always_digests_the_same(self):
        operation = str(uuid.uuid4())
        assert _request(operation).request_digest() == _request(operation).request_digest()
        assert len(_request(operation).request_digest()) == 64

    def test_equivalent_construction_digests_the_same(self):
        """Re-identifying the same binding byte-for-byte must not move a digest."""
        first = _request()
        second = _request(first.operation_id)
        assert first.request_digest() == second.request_digest()

    def test_every_binding_component_changes_the_digest(self, monkeypatch):
        operation = str(uuid.uuid4())
        base = _request(operation)
        for overrides in (
            {"target_terminal_id": "t-other"},
            {"target_generation": "g-other"},
            {"requester_terminal_id": "r-other"},
            {"requester_generation": "r-other-gen"},
            {"native_session_id": "sess-other"},
            {"provider": "claude_code"},
            {"provider_version": "0.148.0"},
            {"provider_artifact_sha256": "b" * 64},
        ):
            changed = _request(operation, **overrides)
            assert changed.request_digest() != base.request_digest(), overrides

    def test_a_request_canonical_field_set_is_closed(self):
        assert set(_request().canonical()) == {
            "schema_version",
            "operation_id",
            "target_terminal_id",
            "target_generation",
            "native_session_id",
            "provider",
            "provider_version",
            "provider_artifact_sha256",
            "requester_terminal_id",
            "requester_generation",
        }

    def test_a_malformed_operation_id_and_bad_artifact_digest_are_refused(self):
        with pytest.raises(ro.RouteObservationInvalid):
            _request("not-a-uuid")
        with pytest.raises(ro.RouteObservationInvalid):
            _request(native_session_id="not an id")


# ---------------------------------------------------------------------------
# claim: exact replay and changed-request conflict
# ---------------------------------------------------------------------------


class TestClaim:
    def test_a_new_operation_claims_the_tuple_as_requested(self):
        record = ro.claim(_request())
        assert record["state"] == ro.STATE_REQUESTED
        assert record["terminal"] is False
        assert record["replayed"] is False
        assert record["receipt_digest"] is None
        assert record["inbox_message_id"] is None

    def test_an_exact_replay_under_the_same_operation_replays(self):
        request = _request()
        first = ro.claim(request)
        second = ro.claim(request)
        assert second["replayed"] is True
        assert second["operation_id"] == first["operation_id"]
        assert second["request_digest"] == first["request_digest"]
        assert second["created_at"] == first["created_at"]
        assert len(ro.list_operations()) == 1

    def test_a_changed_request_under_the_same_operation_id_conflicts_before_mutation(self):
        request = _request()
        ro.claim(request)
        divergent = _request(request.operation_id, target_generation="g-other")
        with pytest.raises(ro.RouteObservationConflict):
            ro.claim(divergent)
        stored = ro.get(request.operation_id)
        assert stored["state"] == ro.STATE_REQUESTED
        assert stored["request_digest"] == request.request_digest()

    def test_a_replay_after_terminalization_still_replays_the_terminal_row(self):
        request = _request()
        ro.claim(request)
        _stage(request)
        ro.complete(request, result=ro.RESULT_OBSERVED_CLOSED, final_event=_EVENT)
        replay = ro.claim(request)
        assert replay["replayed"] is True
        assert replay["state"] == ro.RESULT_OBSERVED_CLOSED


# ---------------------------------------------------------------------------
# one nonterminal owner per exact target tuple
# ---------------------------------------------------------------------------


class TestSingleActiveOwner:
    def test_a_different_operation_for_an_owned_tuple_becomes_a_refusal(self):
        winner = _request()
        ro.claim(winner)
        loser = _request(target_terminal_id=winner.target_terminal_id)
        refused = ro.claim(loser)
        assert refused["state"] == ro.RESULT_ZERO_EFFECT_REFUSAL
        assert refused["terminal"] is True
        assert refused["receipt_digest"] is None
        # the winning id is kept only as non-authoritative detail
        assert refused["detail"] == winner.operation_id
        # the winning row is untouched
        assert ro.get(winner.operation_id)["state"] == ro.STATE_REQUESTED

    def test_the_loser_is_immutable_and_replays_under_its_own_id(self):
        winner = _request()
        ro.claim(winner)
        loser = _request(target_terminal_id=winner.target_terminal_id)
        first = ro.claim(loser)
        assert first["state"] == ro.RESULT_ZERO_EFFECT_REFUSAL
        replay = ro.claim(loser)
        assert replay["replayed"] is True
        assert replay["state"] == ro.RESULT_ZERO_EFFECT_REFUSAL
        assert replay["receipt_digest"] is None
        assert replay["detail"] == first["detail"]
        assert replay["inbox_message_id"] == first["inbox_message_id"]

    def test_an_exact_tuple_is_freed_once_the_winner_terminates(self):
        winner = _request()
        ro.claim(winner)
        loser = _request(target_terminal_id=winner.target_terminal_id)
        refused = ro.claim(loser)
        assert refused["state"] == ro.RESULT_ZERO_EFFECT_REFUSAL
        _stage(winner)
        ro.complete(winner, result=ro.RESULT_OBSERVED_CLOSED, final_event=_EVENT)
        fresh = _request(target_terminal_id=winner.target_terminal_id)
        assert ro.claim(fresh)["state"] == ro.STATE_REQUESTED


# ---------------------------------------------------------------------------
# immutable terminalization and jurisdiction checks
# ---------------------------------------------------------------------------


class TestTermination:
    def test_observed_closed_defers_the_positive_receipt_and_event(self):
        request = _request()
        ro.claim(request)
        _stage(request)
        terminal = ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
        )
        assert terminal["state"] == ro.RESULT_OBSERVED_CLOSED
        assert terminal["terminal"] is True
        assert terminal["receipt_digest"]
        assert terminal["final_event_digest"] is not None
        assert terminal["replayed"] is False

    def test_observed_closed_requires_all_four_ordered_facts(self):
        """Each missing stage fact refuses before the terminal transaction."""
        for missing_count in range(4):
            request = _request(target_terminal_id=f"term-missing-{missing_count}")
            ro.claim(request)
            if missing_count >= 1:
                ro.pre_probe(request, intent=_PRE_PROBE)
            if missing_count >= 2:
                ro.record_observation(request, observation=_OBSERVATION)
            if missing_count >= 3:
                ro.pre_close(request, intent=_PRE_CLOSE)
            with pytest.raises(ro.RouteObservationConflict):
                ro.complete(
                    request,
                    result=ro.RESULT_OBSERVED_CLOSED,
                    final_event=_EVENT,
                )
            #: nothing was written: the operation is still requested and no
            #: wake claim exists for it.
            stored = ro.get(request.operation_id)
            assert stored["state"] == ro.STATE_REQUESTED
            assert stored["receipt_json"] is None
            with database.SessionLocal() as session:
                assert session.query(database.InboxModel).count() == 0

    def test_ambiguous_is_stored_without_a_receipt(self):
        request = _request()
        ro.claim(request)
        terminal = ro.complete(
            request,
            result=ro.RESULT_AMBIGUOUS_AFTER_POSSIBLE_EFFECT,
            final_event={"probe": "inconclusive"},
        )
        assert terminal["state"] == ro.RESULT_AMBIGUOUS_AFTER_POSSIBLE_EFFECT
        assert terminal["receipt_digest"] is None

    def test_a_divergent_terminal_attempt_is_refused_when_already_terminal(self):
        request = _request()
        ro.claim(request)
        _stage(request)
        ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
        )
        with pytest.raises(ro.RouteObservationConflict):
            ro.complete(
                request,
                result=ro.RESULT_OBSERVED_CLOSED,
                final_event={"kind": "different"},
            )

    def test_an_identical_terminal_retry_replays_without_a_second_wake(self):
        request = _request()
        ro.claim(request)
        _stage(request)
        first = ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
        )
        second = ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
        )
        assert second["replayed"] is True
        assert second["inbox_message_id"] == first["inbox_message_id"]
        assert second["receipt_json"] == first["receipt_json"]
        assert second["receipt_digest"] == first["receipt_digest"]

    def test_a_completion_without_a_claimed_operation_conflicts(self):
        with pytest.raises(ro.RouteObservationConflict):
            ro.complete(
                _request(),
                result=ro.RESULT_ZERO_EFFECT_REFUSAL,
                final_event=_EVENT,
            )

    def test_a_terminal_operation_rejects_a_changed_result(self):
        """Same request, same event, no receipt: only the result differs."""
        request = _request()
        ro.claim(request)
        _stage(request)
        ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
        )
        with pytest.raises(ro.RouteObservationConflict):
            ro.complete(
                request,
                result=ro.RESULT_ZERO_EFFECT_REFUSAL,
                final_event=_EVENT,
            )

    def test_a_zero_effect_refusal_cannot_replay_as_ambiguous(self):
        winner = _request()
        ro.claim(winner)
        loser = _request(target_terminal_id=winner.target_terminal_id)
        refused = ro.claim(loser)
        assert refused["state"] == ro.RESULT_ZERO_EFFECT_REFUSAL
        with pytest.raises(ro.RouteObservationConflict):
            ro.complete(
                loser,
                result=ro.RESULT_AMBIGUOUS_AFTER_POSSIBLE_EFFECT,
                final_event=_EVENT,
            )


# ---------------------------------------------------------------------------
# the four ordered effect-stage gates
# ---------------------------------------------------------------------------


class TestEffectStageOrder:
    def test_a_stage_rejects_an_unclaimed_operation(self):
        request = _request()
        for attempt in (
            lambda: ro.pre_probe(request, intent=_PRE_PROBE),
            lambda: ro.record_observation(request, observation=_OBSERVATION),
            lambda: ro.pre_close(request, intent=_PRE_CLOSE),
            lambda: ro.record_close_proof(request, proof=_CLOSE_PROOF),
        ):
            with pytest.raises(ro.RouteObservationConflict):
                attempt()

    def test_observation_requires_the_pre_probe_intent(self):
        request = _request()
        ro.claim(request)
        with pytest.raises(ro.RouteObservationConflict):
            ro.record_observation(request, observation=_OBSERVATION)

    def test_pre_close_requires_the_persisted_observation(self):
        request = _request()
        ro.claim(request)
        ro.pre_probe(request, intent=_PRE_PROBE)
        with pytest.raises(ro.RouteObservationConflict):
            ro.pre_close(request, intent=_PRE_CLOSE)

    def test_close_proof_requires_the_pre_close_intent(self):
        request = _request()
        ro.claim(request)
        ro.pre_probe(request, intent=_PRE_PROBE)
        ro.record_observation(request, observation=_OBSERVATION)
        with pytest.raises(ro.RouteObservationConflict):
            ro.record_close_proof(request, proof=_CLOSE_PROOF)


class TestPreProbeAuthorization:
    def test_the_first_exact_cas_authorizes_exactly_one_probe(self):
        request = _request()
        ro.claim(request)
        first = ro.pre_probe(request, intent=_PRE_PROBE)
        assert first["authorized"] is True
        assert first["replayed"] is False
        assert json.loads(first["pre_probe_intent_json"]) == _PRE_PROBE
        second = ro.pre_probe(request, intent=_PRE_PROBE)
        assert second["authorized"] is False
        assert second["replayed"] is True
        assert second["pre_probe_intent_json"] == first["pre_probe_intent_json"]
        #: one operation id, one committed intent — never a second decision.
        assert len(ro.list_operations()) == 1

    def test_a_retry_after_response_loss_cannot_authorize_a_second_probe(self):
        request = _request()
        ro.claim(request)
        first = ro.pre_probe(request, intent=_PRE_PROBE)
        retry = ro.pre_probe(request, intent=_PRE_PROBE)
        assert retry["authorized"] is False
        assert retry["replayed"] is True
        assert retry["pre_probe_intent_json"] == first["pre_probe_intent_json"]

    def test_a_changed_pre_probe_intent_conflicts_and_writes_nothing(self):
        request = _request()
        ro.claim(request)
        first = ro.pre_probe(request, intent=_PRE_PROBE)
        with pytest.raises(ro.RouteObservationConflict):
            ro.pre_probe(request, intent={"kind": "pre-probe-intent", "surface": "status-v2"})
        assert (
            ro.get(request.operation_id)["pre_probe_intent_json"] == first["pre_probe_intent_json"]
        )


class TestPreCloseAuthorization:
    def test_the_first_exact_cas_authorizes_the_one_close(self):
        request = _request()
        ro.claim(request)
        ro.pre_probe(request, intent=_PRE_PROBE)
        ro.record_observation(request, observation=_OBSERVATION)
        first = ro.pre_close(request, intent=_PRE_CLOSE)
        assert first["authorized"] is True
        second = ro.pre_close(request, intent=_PRE_CLOSE)
        assert second["authorized"] is False
        assert second["replayed"] is True

    def test_a_retry_after_response_loss_cannot_authorize_a_second_escape(self):
        request = _request()
        ro.claim(request)
        ro.pre_probe(request, intent=_PRE_PROBE)
        ro.record_observation(request, observation=_OBSERVATION)
        ro.pre_close(request, intent=_PRE_CLOSE)
        retry = ro.pre_close(request, intent=_PRE_CLOSE)
        assert retry["authorized"] is False
        assert retry["replayed"] is True


class TestStageFactReplay:
    def test_observation_replays_exactly_and_rejects_changed_bytes(self):
        request = _request()
        ro.claim(request)
        ro.pre_probe(request, intent=_PRE_PROBE)
        first = ro.record_observation(request, observation=_OBSERVATION)
        assert first["replayed"] is False
        replay = ro.record_observation(request, observation=_OBSERVATION)
        assert replay["replayed"] is True
        assert replay["observation_json"] == first["observation_json"]
        assert json.loads(replay["observation_json"]) == _OBSERVATION
        with pytest.raises(ro.RouteObservationConflict):
            ro.record_observation(
                request, observation={"kind": "provider-surface", "observed_at": "forged"}
            )

    def test_close_proof_replays_exactly_and_rejects_changed_bytes(self):
        request = _request()
        ro.claim(request)
        ro.pre_probe(request, intent=_PRE_PROBE)
        ro.record_observation(request, observation=_OBSERVATION)
        ro.pre_close(request, intent=_PRE_CLOSE)
        first = ro.record_close_proof(request, proof=_CLOSE_PROOF)
        assert first["replayed"] is False
        replay = ro.record_close_proof(request, proof=_CLOSE_PROOF)
        assert replay["replayed"] is True
        assert replay["close_proof_json"] == first["close_proof_json"]
        with pytest.raises(ro.RouteObservationConflict):
            ro.record_close_proof(
                request, proof={"kind": "owned-close", "outcome": "closed", "closed_at": "forged"}
            )

    def test_the_four_facts_derive_ordered_progress(self):
        request = _request()
        ro.claim(request)
        assert ro.get(request.operation_id)["pre_probe_intent_json"] is None
        ro.pre_probe(request, intent=_PRE_PROBE)
        after_probe = ro.get(request.operation_id)
        assert after_probe["pre_probe_intent_json"]
        assert after_probe["observation_json"] is None
        ro.record_observation(request, observation=_OBSERVATION)
        after_observation = ro.get(request.operation_id)
        assert after_observation["observation_json"]
        assert after_observation["pre_close_intent_json"] is None
        ro.pre_close(request, intent=_PRE_CLOSE)
        after_pre_close = ro.get(request.operation_id)
        assert after_pre_close["pre_close_intent_json"]
        assert after_pre_close["close_proof_json"] is None
        ro.record_close_proof(request, proof=_CLOSE_PROOF)
        assert ro.get(request.operation_id)["close_proof_json"]

    def test_a_stage_write_after_terminalization_is_refused(self):
        request = _request()
        ro.claim(request)
        _stage(request)
        ro.complete(request, result=ro.RESULT_OBSERVED_CLOSED, final_event=_EVENT)
        with pytest.raises(ro.RouteObservationConflict):
            ro.record_observation(request, observation=_OBSERVATION)
        with pytest.raises(ro.RouteObservationConflict):
            ro.pre_probe(request, intent=_PRE_PROBE)


# ---------------------------------------------------------------------------
# effect refusal and ambiguity after an effect intent
# ---------------------------------------------------------------------------


class TestEffectRefusalAndUncertainty:
    def test_zero_effect_refusal_before_any_effect_intent_is_valid(self):
        request = _request()
        ro.claim(request)
        terminal = ro.complete(
            request,
            result=ro.RESULT_ZERO_EFFECT_REFUSAL,
            final_event=_EVENT,
        )
        assert terminal["state"] == ro.RESULT_ZERO_EFFECT_REFUSAL
        assert terminal["receipt_digest"] is None

    def test_zero_effect_refusal_is_impossible_after_a_probe_or_close_intent(self):
        request = _request()
        ro.claim(request)
        ro.pre_probe(request, intent=_PRE_PROBE)
        with pytest.raises(ro.RouteObservationConflict):
            ro.complete(request, result=ro.RESULT_ZERO_EFFECT_REFUSAL, final_event=_EVENT)
        #: even after the full ordered walk, refusal is impossible.
        staged = _request(target_terminal_id="term-closed")
        ro.claim(staged)
        _stage(staged)
        with pytest.raises(ro.RouteObservationConflict):
            ro.complete(staged, result=ro.RESULT_ZERO_EFFECT_REFUSAL, final_event=_EVENT)

    def test_ambiguity_after_an_effect_is_terminal_and_never_clears_facts(self):
        request = _request()
        ro.claim(request)
        _stage(request)
        terminal = ro.complete(
            request,
            result=ro.RESULT_AMBIGUOUS_AFTER_POSSIBLE_EFFECT,
            final_event={"probe": "inconclusive"},
        )
        assert terminal["state"] == ro.RESULT_AMBIGUOUS_AFTER_POSSIBLE_EFFECT
        assert terminal["receipt_digest"] is None
        stored = ro.get(request.operation_id)
        for field in ro.STAGE_FACT_FIELDS:
            assert stored[field] is not None
            assert stored[field] == terminal[field]
        # exact replay of the terminal ambiguity is immutable and single-wake.
        replay = ro.complete(
            request,
            result=ro.RESULT_AMBIGUOUS_AFTER_POSSIBLE_EFFECT,
            final_event={"probe": "inconclusive"},
        )
        assert replay["replayed"] is True
        assert replay["inbox_message_id"] == terminal["inbox_message_id"]


# ---------------------------------------------------------------------------
# the positive receipt is derived from persisted facts, never caller proofs
# ---------------------------------------------------------------------------


class TestDerivedPositiveReceipt:
    def test_the_receipt_binds_the_persisted_observation_and_close_proof_exactly(self):
        request = _request()
        ro.claim(request)
        _stage(request)
        terminal = ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
        )
        stored = ro.get(request.operation_id)
        receipt = json.loads(terminal["receipt_json"])
        assert receipt["schema"] == ro.RECEIPT_SCHEMA
        assert receipt["kind"] == ro.RESULT_OBSERVED_CLOSED
        assert receipt["operation_id"] == request.operation_id
        assert receipt["request_digest"] == request.request_digest()
        assert receipt["final_event_digest"] == canonical_sha256(_EVENT)
        # The two proof halves equal the stored canonical facts byte-for-byte.
        assert receipt["observation"] == _OBSERVATION
        assert receipt["close_proof"] == _CLOSE_PROOF
        assert json.loads(stored["observation_json"]) == _OBSERVATION
        assert json.loads(stored["close_proof_json"]) == _CLOSE_PROOF
        # The receipt is a deterministic function of those facts.
        assert json.loads(stored["receipt_json"]) == receipt
        assert terminal["receipt_digest"] == canonical_sha256(receipt)

    def test_a_caller_cannot_furnish_an_independent_receipt_copy(self):
        """``complete`` no longer accepts a proof parameter at all."""
        request = _request()
        ro.claim(request)
        _stage(request)
        with pytest.raises(TypeError):
            ro.complete(
                request,
                result=ro.RESULT_OBSERVED_CLOSED,
                final_event=_EVENT,
                receipt={"kind": "forged"},
            )

    def test_a_receipt_cannot_substitute_caller_assertions_for_facts(self):
        """No stored fact, no receipt: only the four committed facts count."""
        request = _request()
        ro.claim(request)
        ro.pre_probe(request, intent=_PRE_PROBE)
        ro.record_observation(request, observation=_OBSERVATION)
        with pytest.raises(ro.RouteObservationConflict):
            ro.complete(
                request,
                result=ro.RESULT_OBSERVED_CLOSED,
                final_event=_EVENT,
            )
        assert ro.get(request.operation_id)["receipt_json"] is None


# ---------------------------------------------------------------------------
# the atomic exact-requester wake claim
# ---------------------------------------------------------------------------


class TestWakeClaim:
    def test_the_wake_is_claimed_into_the_inbox_with_the_exact_requester(self):
        request = _request()
        ro.claim(request)
        _stage(request)
        terminal = ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
        )
        with database.SessionLocal() as session:
            inbox = (
                session.query(database.InboxModel)
                .filter(database.InboxModel.id == terminal["inbox_message_id"])
                .one()
            )
        assert inbox.receiver_id == request.requester_terminal_id
        assert inbox.expected_receiver_generation == request.requester_generation
        assert inbox.sender_id == request.target_terminal_id
        assert inbox.sender_generation == request.target_generation
        assert inbox.status == MessageStatus.PENDING.value
        wake = json.loads(inbox.message)
        assert wake["operation_id"] == request.operation_id
        assert wake["result_kind"] == ro.RESULT_OBSERVED_CLOSED

    def test_the_loser_still_holds_its_own_exact_requester_wake(self):
        winner = _request()
        ro.claim(winner)
        loser = _request(
            target_terminal_id=winner.target_terminal_id,
            requester_terminal_id="term-loser-requester",
            requester_generation="gen-loser-requester",
        )
        refused = ro.claim(loser)
        assert refused["state"] == ro.RESULT_ZERO_EFFECT_REFUSAL
        with database.SessionLocal() as session:
            inbox = (
                session.query(database.InboxModel)
                .filter(database.InboxModel.id == refused["inbox_message_id"])
                .one()
            )
        assert inbox.receiver_id == "term-loser-requester"
        assert inbox.expected_receiver_generation == "gen-loser-requester"

    def test_a_rolled_back_stage_write_leaves_no_fact(self):
        """Stage facts are durable only in the same all-or-none commit."""
        request = _request()
        with database.SessionLocal() as session:
            ro.claim(request, db=session)
            ro.pre_probe(request, intent=_PRE_PROBE, db=session)
            _stage(request, db=session)
            session.rollback()
        assert ro.get(request.operation_id) is None

    def test_the_callers_rollback_removes_the_wake_with_the_row(self):
        """Commit all or none: the wake is not a separate durable fact."""
        request = _request()
        with database.SessionLocal() as session:
            ro.claim(request, db=session)
            _stage(request, db=session)
            terminal = ro.complete(
                request,
                result=ro.RESULT_OBSERVED_CLOSED,
                final_event={"kind": "transient"},
                db=session,
            )
            wake_id = terminal["inbox_message_id"]
            session.rollback()
        assert ro.get(request.operation_id) is None
        with database.SessionLocal() as session:
            assert (
                session.query(database.InboxModel).filter(database.InboxModel.id == wake_id).count()
                == 0
            )

    def test_replaying_never_issues_a_second_wake(self):
        request = _request()
        ro.claim(request)
        first = ro.complete(request, result=ro.RESULT_ZERO_EFFECT_REFUSAL, final_event=_EVENT)
        ro.complete(request, result=ro.RESULT_ZERO_EFFECT_REFUSAL, final_event=_EVENT)
        with database.SessionLocal() as session:
            count = session.query(database.InboxModel).count()
        assert count == 1
        assert first["inbox_message_id"] is not None


# ---------------------------------------------------------------------------
# reads and disabled state
# ---------------------------------------------------------------------------


class TestReads:
    def test_get_returns_none_for_an_unknown_operation(self):
        assert ro.get(str(uuid.uuid4())) is None

    def test_list_is_oldest_first_and_read_only(self):
        first = ro.claim(_request())
        second = ro.claim(_request())
        rows = ro.list_operations()
        assert [r["operation_id"] for r in rows] == [first["operation_id"], second["operation_id"]]
