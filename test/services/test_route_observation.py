"""COND-0230 M10-A dark route-observation transaction core.

Every test here is about the durable journal only. Nothing observes a provider
surface, closes a modal, issues pane input, attaches a consumer, or wakes
anybody: the point of the slice is that the record is trustworthy *before* any
effect lane is allowed to act on it.
"""

from __future__ import annotations

import json
import uuid

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services import route_observation as ro

ARTIFACT = "a" * 64


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


_EVENT = {"stated_by": "probe"}
_RECEIPT = {"schema": ro.RECEIPT_SCHEMA, "kind": "observed-closed"}


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

    def test_there_is_no_adopted_result_anywhere(self):
        """No ``adopted`` result token exists — not in the vocabulary, not in
        a record, and not behind a module attribute that a later lane could
        reach for."""
        import inspect

        source = inspect.getsource(ro)
        assert "adopted" not in source
        assert "adopted" not in ro.TERMINAL_RESULTS
        assert not hasattr(ro, "adopted")

    def test_a_record_never_carries_a_dispatched_or_updated_state(self):
        record = ro.claim(_request())
        assert "dispatch_state" not in record
        assert "updated_at" in record  # only the terminal transition sets it


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
        ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
            receipt=_RECEIPT,
        )
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
        ro.complete(
            winner,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
            receipt=_RECEIPT,
        )
        fresh = _request(target_terminal_id=winner.target_terminal_id)
        assert ro.claim(fresh)["state"] == ro.STATE_REQUESTED


# ---------------------------------------------------------------------------
# immutable terminalization and jurisdiction checks
# ---------------------------------------------------------------------------


class TestTermination:
    def test_observed_closed_defers_the_positive_receipt_and_event(self):
        request = _request()
        ro.claim(request)
        terminal = ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
            receipt=_RECEIPT,
        )
        assert terminal["state"] == ro.RESULT_OBSERVED_CLOSED
        assert terminal["terminal"] is True
        assert terminal["receipt_digest"]
        assert json.loads(terminal["receipt_json"]) == _RECEIPT
        assert terminal["final_event_digest"] is not None
        assert terminal["replayed"] is False

    def test_observed_closed_requires_the_positive_receipt(self):
        request = _request()
        ro.claim(request)
        with pytest.raises(ro.RouteObservationInvalid):
            ro.complete(
                request,
                result=ro.RESULT_OBSERVED_CLOSED,
                final_event=_EVENT,
            )

    def test_a_non_positive_result_rejects_a_positive_receipt(self):
        request = _request()
        ro.claim(request)
        with pytest.raises(ro.RouteObservationInvalid):
            ro.complete(
                request,
                result=ro.RESULT_AMBIGUOUS_AFTER_POSSIBLE_EFFECT,
                final_event=_EVENT,
                receipt=_RECEIPT,
            )

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
        ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
            receipt=_RECEIPT,
        )
        with pytest.raises(ro.RouteObservationConflict):
            ro.complete(
                request,
                result=ro.RESULT_OBSERVED_CLOSED,
                final_event={"kind": "different"},
                receipt=_RECEIPT,
            )

    def test_an_identical_terminal_retry_replays_without_a_second_wake(self):
        request = _request()
        ro.claim(request)
        first = ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
            receipt=_RECEIPT,
        )
        second = ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
            receipt=_RECEIPT,
        )
        assert second["replayed"] is True
        assert second["inbox_message_id"] == first["inbox_message_id"]
        assert second["receipt_digest"] == first["receipt_digest"]

    def test_a_completion_without_a_claimed_operation_conflicts(self):
        with pytest.raises(ro.RouteObservationConflict):
            ro.complete(
                _request(),
                result=ro.RESULT_ZERO_EFFECT_REFUSAL,
                final_event=_EVENT,
            )


# ---------------------------------------------------------------------------
# the atomic exact-requester wake claim
# ---------------------------------------------------------------------------


class TestWakeClaim:
    def test_the_wake_is_claimed_into_the_inbox_with_the_exact_requester(self):
        request = _request()
        ro.claim(request)
        terminal = ro.complete(
            request,
            result=ro.RESULT_OBSERVED_CLOSED,
            final_event=_EVENT,
            receipt=_RECEIPT,
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

    def test_the_callers_rollback_removes_the_wake_with_the_row(self):
        """Commit all or none: the wake is not a separate durable fact."""
        request = _request()
        with database.SessionLocal() as session:
            ro.claim(request, db=session)
            terminal = ro.complete(
                request,
                result=ro.RESULT_OBSERVED_CLOSED,
                final_event={"kind": "transient"},
                receipt={"schema": ro.RECEIPT_SCHEMA, "kind": "observed-closed"},
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
