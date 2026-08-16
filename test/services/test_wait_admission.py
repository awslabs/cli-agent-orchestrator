"""M7 Stage 2: dark exact-generation wait-message admission.

Every test here is about the durable contract only. Nothing in this module
delivers a message, steers a pane, attaches a consumer, or turns a capability
on: the point of the slice is that the record is trustworthy *before* anything
is allowed to act on it.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services import wait_admission as wa

SESSION = "cao-m7-wait"


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    return isolated_memory_db


#: Distinguishes "caller did not say" from an explicit ``None`` native id —
#: the difference this whole test class is about.
_UNSET = object()


def _bind(*, suffix, agent_id=None, native=_UNSET, role=roster.ROLE_WORKER):
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=agent_id or str(uuid.uuid4()),
            session_name=SESSION,
            role=role,
            profile_family="developer",
            harness="claude_code",
            native_session_id=f"native-{suffix}" if native is _UNSET else native,
            # The roster refuses to record *how* an id was obtained when there
            # is no id: identity_missing is a lineage with neither.
            acquisition_method=None if native is None else "chosen_session_id",
            terminal_id=f"term-{suffix}",
            generation=str(uuid.uuid4()),
            pane_id=f"%{suffix}",
            pane_pid=8000 + int(suffix),
            process_identity={"pid": 8000 + int(suffix), "start_marker": f"m-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _owner_from(bound, **overrides):
    incarnation = bound["incarnation"]
    lineage = bound["lineage"]
    fields = {
        "agent_id": incarnation["agent_id"],
        "incarnation_id": incarnation["incarnation_id"],
        "terminal_id": incarnation["terminal_id"],
        "generation": incarnation["generation"],
        "lineage_id": lineage["lineage_id"],
        "native_session_id": lineage["native_session_id"],
    }
    fields.update(overrides)
    return wa.WaitOwner(**fields)


def _message(kind=wa.KIND_WORKER_WAKE, **overrides):
    fields = {
        "message_id": str(uuid.uuid4()),
        "kind": kind,
        "reason_code": "stop-interrupted-wait",
    }
    fields.update(overrides)
    return wa.WaitMessage(**fields)


def _request(owner, message=None, operation_id=None):
    return wa.AdmissionRequest(
        operation_id=operation_id or str(uuid.uuid4()),
        session_name=SESSION,
        owner=owner,
        message=message or _message(),
    )


# ---------------------------------------------------------------------------
# the capability is visibly off
# ---------------------------------------------------------------------------


class TestDisabledCapability:
    def test_the_capability_block_says_disabled_in_every_field_that_matters(self):
        block = wa.capability()
        assert block["capability"] == wa.CAPABILITY_NAME
        assert block["enabled"] is False
        assert block["reason"]
        # The three things a reader would otherwise have to infer from absence.
        assert block["consumer_attached"] is False
        assert block["stop_interruptor_attached"] is False
        assert block["public_surface"] is False
        # Authority this slice explicitly does not hold.
        assert block["recovery_authority"] is False
        assert block["action_authority"] is False
        assert block["completion_authority"] is False
        # The M7 plan's C and D gates are untouched by Stage 2.
        assert block["stage_c_enabled"] is False
        assert block["stage_d_enabled"] is False

    def test_every_effect_verb_is_advertised_off(self):
        effects = wa.capability()["effects"]
        assert set(effects) == set(wa.EFFECT_OPERATIONS)
        assert not any(effects.values())

    @pytest.mark.parametrize("operation", sorted(wa.EFFECT_OPERATIONS))
    def test_requesting_any_effect_is_refused_by_name(self, operation):
        with pytest.raises(wa.WaitAdmissionDisabled) as excinfo:
            wa.require_capability(operation)
        assert operation in str(excinfo.value)
        assert wa.CAPABILITY_NAME in str(excinfo.value)

    def test_an_unknown_effect_verb_is_a_programming_error_not_a_silent_pass(self):
        with pytest.raises(wa.WaitAdmissionInvalid):
            wa.require_capability("teleport")

    def test_the_denial_vocabulary_is_published_and_closed(self):
        assert wa.capability()["denial_reasons"] == sorted(wa.DENIAL_REASONS)
        assert wa.DENIAL_REASONS == {
            wa.DENY_OWNER_UNKNOWN,
            wa.DENY_OWNER_RETIRED,
            wa.DENY_OWNER_AMBIGUOUS,
            wa.DENY_OWNER_REPLACED,
            wa.DENY_GENERATION_STALE,
            wa.DENY_IDENTITY_MISMATCH,
        }

    def test_the_capability_block_is_versioned(self):
        block = wa.capability()
        assert block["schema_version"] == wa.CAPABILITY_SCHEMA_VERSION
        assert block["contract_schema_version"] == wa.SCHEMA_VERSION
        assert block["message_schema_version"] == wa.MESSAGE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# the fixed, versioned message schema
# ---------------------------------------------------------------------------


class TestMessageSchema:
    def test_the_envelope_carries_its_version_and_the_full_owner_identity(self):
        bound = _bind(suffix="1")
        owner = _owner_from(bound)
        message = _message()
        envelope = wa.render_envelope(session_name=SESSION, owner=owner, message=message)
        assert envelope["message_version"] == wa.MESSAGE_SCHEMA_VERSION
        assert envelope["kind"] == wa.KIND_WORKER_WAKE
        assert envelope["session_name"] == SESSION
        # Nullable identity fields are present as explicit nulls, never absent:
        # an absent key and a null key must not hash the same.
        assert set(envelope["owner"]) == set(wa.OWNER_FIELDS)
        assert envelope["owner"]["restore_contract_id"] is None
        assert set(envelope["body"]) == set(wa.BODY_FIELDS)

    def test_the_same_message_always_encodes_to_the_same_bytes(self):
        bound = _bind(suffix="1")
        owner = _owner_from(bound)
        message = _message()
        first = wa.envelope_bytes(
            wa.render_envelope(session_name=SESSION, owner=owner, message=message)
        )
        second = wa.envelope_bytes(
            wa.render_envelope(session_name=SESSION, owner=owner, message=message)
        )
        assert first == second
        assert first.endswith(b"\n")

    def test_a_null_identity_field_does_not_encode_like_a_present_one(self):
        bound = _bind(suffix="1")
        with_id = _owner_from(bound)
        without_id = _owner_from(bound, native_session_id=None)
        message = _message()
        assert wa.envelope_bytes(
            wa.render_envelope(session_name=SESSION, owner=with_id, message=message)
        ) != wa.envelope_bytes(
            wa.render_envelope(session_name=SESSION, owner=without_id, message=message)
        )

    @pytest.mark.parametrize(
        "kind", [wa.KIND_EXPIRY, wa.KIND_WORKER_WAKE, wa.KIND_REPORT, wa.KIND_DECISION]
    )
    def test_the_four_named_kinds_are_the_admissible_ones(self, kind):
        assert kind in wa.MESSAGE_KINDS
        assert _message(kind=kind).kind == kind

    def test_an_unnamed_kind_is_refused(self):
        with pytest.raises(wa.WaitAdmissionInvalid):
            _message(kind="broadcast")

    def test_the_body_key_set_is_closed(self):
        assert wa.BODY_FIELDS == ("reason_code", "payload_digest", "source_operation_id", "text")
        with pytest.raises(TypeError):
            wa.WaitMessage(
                message_id=str(uuid.uuid4()),
                kind=wa.KIND_REPORT,
                reason_code="r",
                severity="high",
            )

    def test_a_payload_digest_must_be_a_sha256(self):
        with pytest.raises(wa.WaitAdmissionInvalid):
            _message(payload_digest="not-a-digest")


# ---------------------------------------------------------------------------
# exact owner identity and generation
# ---------------------------------------------------------------------------


class TestExactOwnership:
    def test_the_current_exact_generation_is_admitted(self):
        bound = _bind(suffix="1")
        record = wa.admit(_request(_owner_from(bound)))
        assert record["admission_state"] == wa.STATE_ADMITTED
        assert record["denial_reason"] is None
        assert record["dispatch_state"] == wa.DISPATCH_WITHHELD
        assert record["receipt_digest"]
        assert record["adopted"] is False
        assert record["owner"]["generation"] == bound["incarnation"]["generation"]

    def test_an_unknown_agent_is_denied(self):
        owner = wa.WaitOwner(
            agent_id=str(uuid.uuid4()),
            incarnation_id="inc-ghost",
            terminal_id="term-ghost",
            generation=str(uuid.uuid4()),
        )
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_OWNER_UNKNOWN

    def test_a_retired_owner_is_denied(self):
        bound = _bind(suffix="1")
        owner = _owner_from(bound)
        roster.retire_incarnation(
            terminal_id=owner.terminal_id, generation=owner.generation, reason="stopped"
        )
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_OWNER_RETIRED

    def test_a_replaced_incarnation_is_denied(self):
        """The agent came back on a new pane; the old wait is not its wait."""
        first = _bind(suffix="1")
        stale_owner = _owner_from(first)
        roster.retire_incarnation(
            terminal_id=stale_owner.terminal_id,
            generation=stale_owner.generation,
            reason="stopped",
        )
        _bind(
            suffix="2", agent_id=stale_owner.agent_id, native=first["lineage"]["native_session_id"]
        )
        record = wa.admit(_request(stale_owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_OWNER_REPLACED

    def test_a_stale_generation_under_the_live_incarnation_id_is_denied(self):
        bound = _bind(suffix="1")
        owner = _owner_from(bound, generation=str(uuid.uuid4()))
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_GENERATION_STALE

    def test_a_stale_terminal_under_the_live_incarnation_id_is_denied(self):
        bound = _bind(suffix="1")
        owner = _owner_from(bound, terminal_id="term-recycled")
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_GENERATION_STALE

    def test_two_live_incarnations_on_one_terminal_id_are_ambiguous_not_guessed(self):
        """Legacy/corrupt state must refuse, never pick a row.

        Picking would admit a message for whichever conversation happened to
        sort first — the exact silent misdelivery this contract exists to stop.
        """
        bound = _bind(suffix="1")
        owner = _owner_from(bound)
        with database.SessionLocal() as session:
            session.add(
                database.StableAgentIncarnationModel(
                    incarnation_id="inc-shadow",
                    agent_id=str(uuid.uuid4()),
                    lineage_id=None,
                    terminal_id=owner.terminal_id,
                    generation=str(uuid.uuid4()),
                    pane_id="%99",
                    pane_pid=9999,
                    process_identity_json=None,
                    execution_mode="native_tui",
                    disposition=roster.INCARNATION_ADMITTED,
                    created_at="2026-08-16T00:00:00Z",
                    updated_at="2026-08-16T00:00:00Z",
                )
            )
            session.commit()
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_OWNER_AMBIGUOUS


class TestNullableIdentityValues:
    def test_claiming_no_native_id_against_a_lineage_that_has_one_is_denied(self):
        bound = _bind(suffix="1")
        owner = _owner_from(bound, native_session_id=None)
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_IDENTITY_MISMATCH

    def test_claiming_a_native_id_against_an_identity_missing_lineage_is_denied(self):
        bound = _bind(suffix="1", native=None)
        assert bound["lineage"]["native_session_id"] is None
        owner = _owner_from(bound, native_session_id="native-invented")
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_IDENTITY_MISMATCH

    def test_a_truthfully_missing_native_id_on_both_sides_is_an_exact_match(self):
        bound = _bind(suffix="1", native=None)
        record = wa.admit(_request(_owner_from(bound)))
        assert record["admission_state"] == wa.STATE_ADMITTED
        assert record["owner"]["native_session_id"] is None

    def test_a_wrong_lineage_id_is_denied(self):
        bound = _bind(suffix="1")
        owner = _owner_from(bound, lineage_id=str(uuid.uuid4()))
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_IDENTITY_MISMATCH

    def test_claiming_no_lineage_against_a_bound_one_is_denied(self):
        bound = _bind(suffix="1")
        owner = _owner_from(bound, lineage_id=None, native_session_id=None)
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_IDENTITY_MISMATCH

    def test_an_unknown_restore_contract_is_denied(self):
        bound = _bind(suffix="1")
        owner = _owner_from(bound, restore_contract_id=str(uuid.uuid4()))
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_IDENTITY_MISMATCH

    def test_a_restore_digest_without_its_contract_id_is_malformed(self):
        bound = _bind(suffix="1")
        with pytest.raises(wa.WaitAdmissionInvalid):
            _owner_from(bound, restore_contract_digest="d" * 64)

    def test_a_restore_contract_for_another_incarnation_is_denied(self):
        first = _bind(suffix="1")
        second = _bind(suffix="2")
        contract_id = str(uuid.uuid4())
        with database.SessionLocal() as session:
            session.add(
                database.RestoreContractModel(
                    contract_id=contract_id,
                    contract_digest="e" * 64,
                    schema_version="cao-m3-restore-contract-v1",
                    agent_id=second["incarnation"]["agent_id"],
                    lineage_id=second["lineage"]["lineage_id"],
                    terminal_id=second["incarnation"]["terminal_id"],
                    generation=second["incarnation"]["generation"],
                    native_session_id=second["lineage"]["native_session_id"],
                    contract_json="{}",
                    created_at="2026-08-16T00:00:00Z",
                )
            )
            session.commit()
        owner = _owner_from(
            first, restore_contract_id=contract_id, restore_contract_digest="e" * 64
        )
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_IDENTITY_MISMATCH

    def test_a_matching_restore_contract_is_admitted(self):
        bound = _bind(suffix="1")
        contract_id = str(uuid.uuid4())
        with database.SessionLocal() as session:
            session.add(
                database.RestoreContractModel(
                    contract_id=contract_id,
                    contract_digest="f" * 64,
                    schema_version="cao-m3-restore-contract-v1",
                    agent_id=bound["incarnation"]["agent_id"],
                    lineage_id=bound["lineage"]["lineage_id"],
                    terminal_id=bound["incarnation"]["terminal_id"],
                    generation=bound["incarnation"]["generation"],
                    native_session_id=bound["lineage"]["native_session_id"],
                    contract_json="{}",
                    created_at="2026-08-16T00:00:00Z",
                )
            )
            session.commit()
        owner = _owner_from(
            bound, restore_contract_id=contract_id, restore_contract_digest="f" * 64
        )
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_ADMITTED

    def test_a_restore_digest_that_disagrees_with_the_stored_contract_is_denied(self):
        bound = _bind(suffix="1")
        contract_id = str(uuid.uuid4())
        with database.SessionLocal() as session:
            session.add(
                database.RestoreContractModel(
                    contract_id=contract_id,
                    contract_digest="f" * 64,
                    schema_version="cao-m3-restore-contract-v1",
                    agent_id=bound["incarnation"]["agent_id"],
                    lineage_id=bound["lineage"]["lineage_id"],
                    terminal_id=bound["incarnation"]["terminal_id"],
                    generation=bound["incarnation"]["generation"],
                    native_session_id=bound["lineage"]["native_session_id"],
                    contract_json="{}",
                    created_at="2026-08-16T00:00:00Z",
                )
            )
            session.commit()
        owner = _owner_from(
            bound, restore_contract_id=contract_id, restore_contract_digest="a" * 64
        )
        record = wa.admit(_request(owner))
        assert record["admission_state"] == wa.STATE_DENIED
        assert record["denial_reason"] == wa.DENY_IDENTITY_MISMATCH


class TestNoSystemOwner:
    @pytest.mark.parametrize("reserved", sorted(wa.RESERVED_OWNER_IDS))
    def test_a_reserved_pseudo_owner_is_refused_for_the_terminal(self, reserved):
        with pytest.raises(wa.WaitAdmissionInvalid):
            wa.WaitOwner(
                agent_id=str(uuid.uuid4()),
                incarnation_id="inc-1",
                terminal_id=reserved,
                generation=str(uuid.uuid4()),
            )

    @pytest.mark.parametrize("reserved", sorted(wa.RESERVED_OWNER_IDS))
    def test_a_reserved_pseudo_owner_is_refused_for_the_incarnation(self, reserved):
        with pytest.raises(wa.WaitAdmissionInvalid):
            wa.WaitOwner(
                agent_id=str(uuid.uuid4()),
                incarnation_id=reserved,
                terminal_id="term-1",
                generation=str(uuid.uuid4()),
            )

    def test_the_agent_must_be_a_real_roster_agent_id_not_a_word(self):
        with pytest.raises(wa.WaitAdmissionInvalid):
            wa.WaitOwner(
                agent_id="system",
                incarnation_id="inc-1",
                terminal_id="term-1",
                generation=str(uuid.uuid4()),
            )

    def test_there_is_no_owner_free_admission_entry_point(self):
        """No overload admits a message without naming an exact owner."""
        assert not hasattr(wa, "admit_system_message")
        assert not hasattr(wa, "SYSTEM_OWNER")
        with pytest.raises(TypeError):
            wa.AdmissionRequest(
                operation_id=str(uuid.uuid4()), session_name=SESSION, message=_message()
            )

    def test_a_generation_is_never_optional(self):
        with pytest.raises(wa.WaitAdmissionInvalid):
            wa.WaitOwner(
                agent_id=str(uuid.uuid4()),
                incarnation_id="inc-1",
                terminal_id="term-1",
                generation=None,
            )


# ---------------------------------------------------------------------------
# response-loss replay and divergence
# ---------------------------------------------------------------------------


class TestReplay:
    def test_an_identical_retry_adopts_the_stored_admission(self):
        bound = _bind(suffix="1")
        request = _request(_owner_from(bound))
        first = wa.admit(request)
        second = wa.admit(request)
        assert second["adopted"] is True
        assert second["admission_id"] == first["admission_id"]
        assert second["receipt_digest"] == first["receipt_digest"]
        assert second["message_json"] == first["message_json"]
        assert second["created_at"] == first["created_at"]
        assert len(wa.list_admissions(SESSION)) == 1

    def test_the_admission_id_is_derived_so_a_retry_cannot_mint_a_second_one(self):
        bound = _bind(suffix="1")
        request = _request(_owner_from(bound))
        record = wa.admit(request)
        assert record["admission_id"] == wa.admission_id_for(request.operation_id)

    def test_a_replay_after_the_owner_moved_still_returns_the_original_verdict(self):
        """Replay is a read of a durable fact, not a fresh evaluation.

        The interesting failure is the process that died after committing the
        admission and before its caller saw the answer. When it retries, the
        roster may already have moved on. Re-deciding would hand the same
        operation two different verdicts.
        """
        bound = _bind(suffix="1")
        request = _request(_owner_from(bound))
        first = wa.admit(request)
        assert first["admission_state"] == wa.STATE_ADMITTED
        roster.retire_incarnation(
            terminal_id=request.owner.terminal_id,
            generation=request.owner.generation,
            reason="stopped",
        )
        second = wa.admit(request)
        assert second["adopted"] is True
        assert second["admission_state"] == wa.STATE_ADMITTED
        assert second["receipt_digest"] == first["receipt_digest"]

    def test_a_denial_replays_as_the_same_denial(self):
        owner = wa.WaitOwner(
            agent_id=str(uuid.uuid4()),
            incarnation_id="inc-ghost",
            terminal_id="term-ghost",
            generation=str(uuid.uuid4()),
        )
        request = _request(owner)
        first = wa.admit(request)
        second = wa.admit(request)
        assert first["admission_state"] == wa.STATE_DENIED
        assert second["adopted"] is True
        assert second["denial_reason"] == first["denial_reason"]
        assert second["receipt_digest"] == first["receipt_digest"]

    def test_a_divergent_message_under_the_same_operation_is_refused(self):
        bound = _bind(suffix="1")
        owner = _owner_from(bound)
        operation_id = str(uuid.uuid4())
        wa.admit(_request(owner, operation_id=operation_id))
        with pytest.raises(wa.WaitAdmissionConflict) as excinfo:
            wa.admit(_request(owner, message=_message(), operation_id=operation_id))
        assert "diverg" in str(excinfo.value)

    def test_a_divergent_owner_under_the_same_operation_is_refused(self):
        first = _bind(suffix="1")
        second = _bind(suffix="2")
        message = _message()
        operation_id = str(uuid.uuid4())
        wa.admit(_request(_owner_from(first), message=message, operation_id=operation_id))
        with pytest.raises(wa.WaitAdmissionConflict):
            wa.admit(_request(_owner_from(second), message=message, operation_id=operation_id))

    def test_a_divergent_body_field_under_the_same_operation_is_refused(self):
        bound = _bind(suffix="1")
        owner = _owner_from(bound)
        operation_id = str(uuid.uuid4())
        message = _message()
        wa.admit(_request(owner, message=message, operation_id=operation_id))
        louder = wa.WaitMessage(
            message_id=message.message_id, kind=message.kind, reason_code="something-else"
        )
        with pytest.raises(wa.WaitAdmissionConflict):
            wa.admit(_request(owner, message=louder, operation_id=operation_id))

    def test_one_message_id_cannot_be_admitted_under_two_operations(self):
        bound = _bind(suffix="1")
        owner = _owner_from(bound)
        message = _message()
        wa.admit(_request(owner, message=message))
        with pytest.raises(wa.WaitAdmissionConflict) as excinfo:
            wa.admit(_request(owner, message=message))
        assert message.message_id in str(excinfo.value)

    def test_the_receipt_binds_the_operation_the_owner_and_the_message(self):
        bound = _bind(suffix="1")
        record = wa.admit(_request(_owner_from(bound)))
        assert record["receipt_digest"] == wa.receipt_digest_for(record)
        moved = dict(record)
        moved["message_digest"] = "0" * 64
        assert wa.receipt_digest_for(moved) != record["receipt_digest"]


class TestReads:
    def test_an_admission_is_readable_by_operation_and_by_message(self):
        bound = _bind(suffix="1")
        request = _request(_owner_from(bound))
        record = wa.admit(request)
        assert wa.get_admission(request.operation_id)["admission_id"] == record["admission_id"]
        assert (
            wa.get_admission_by_message(request.message.message_id)["admission_id"]
            == record["admission_id"]
        )

    def test_an_unknown_operation_reads_as_none_rather_than_raising(self):
        assert wa.get_admission(str(uuid.uuid4())) is None
        assert wa.get_admission_by_message(str(uuid.uuid4())) is None

    def test_listing_is_scoped_and_ordered(self):
        first = _bind(suffix="1")
        second = _bind(suffix="2")
        wa.admit(_request(_owner_from(first)))
        wa.admit(_request(_owner_from(second)))
        assert len(wa.list_admissions(SESSION)) == 2
        assert wa.list_admissions("cao-other") == []


# ---------------------------------------------------------------------------
# nothing is consumed, nothing is attached
# ---------------------------------------------------------------------------


class TestNoConsumerNoEffects:
    def test_the_m3c_stop_interruptor_is_still_the_empty_dark_seam(self):
        from cli_agent_orchestrator.services import cohort_effects

        bound = _bind(suffix="1")
        wa.admit(_request(_owner_from(bound)))
        assert cohort_effects._default_wait_interruptor(SESSION, str(uuid.uuid4())) == ()

    def test_no_other_module_imports_this_one_yet(self):
        """Stage 2 is a contract, not a wiring change.

        If a consumer appears, this assertion is the thing that has to be
        deliberately updated — which is the point.
        """
        root = _cao_source_root()
        scanned = list(root.rglob("*.py"))
        # Guard against a vacuous pass from a wrong root.
        assert (root / "services" / "wait_admission.py") in scanned
        offenders = [
            path.relative_to(root).as_posix()
            for path in scanned
            if path.name != "wait_admission.py" and "wait_admission" in path.read_text()
        ]
        assert offenders == []

    def test_admitting_writes_exactly_one_row_and_touches_no_other_table(self):
        bound = _bind(suffix="1")
        before = _table_counts()
        wa.admit(_request(_owner_from(bound)))
        after = _table_counts()
        changed = {
            name: (before[name], after[name]) for name in after if before[name] != after[name]
        }
        assert changed == {"wait_message_admissions": (0, 1)}

    def test_the_module_exposes_no_delivery_entry_point(self):
        for forbidden in (
            "deliver",
            "deliver_message",
            "consume",
            "suppress",
            "expire_waits",
            "attach_consumer",
            "register_wait",
            "activate",
        ):
            assert not hasattr(wa, forbidden), forbidden


def _cao_source_root():
    import cli_agent_orchestrator

    return pathlib.Path(cli_agent_orchestrator.__file__).parent


def _table_counts():
    from sqlalchemy import func, select

    counts = {}
    with database.SessionLocal() as session:
        for table in database.Base.metadata.sorted_tables:
            counts[table.name] = session.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
    return counts
