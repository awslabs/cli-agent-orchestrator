"""Exclusive crash-safe ownership of one provider-native session.

Covers the acceptance matrix row for the native session lifecycle: CAS
ownership with journaled intent, resume binding a new owner before
admission, and the required negatives — native-vs-native and
ACP-vs-native concurrent attachment refused, release without a
no-survivor proof refused, ambiguity frozen, and the crash points before
and after process start and before and after attachment publication all
converging on one adjudicable outcome.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import native_attachment as na

PROVIDER = "kimi_cli"
SESSION = "session_326c5026"


def _intent(method: str = na.ACQUISITION_ACP_BOOTSTRAP) -> dict:
    if method == na.ACQUISITION_ACP_BOOTSTRAP:
        return na.acquire_intent(
            acquisition_method=method,
            acquisition_receipt={"kind": "kimi-acp-session-new", "session_id": SESSION},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
            bootstrap_sent_no_turn=True,
            bootstrap_detached_before_launch=True,
        )
    return na.acquire_intent(
        acquisition_method=method,
        acquisition_receipt={"kind": "kimi-session-resume", "session_id": SESSION},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
    )


def _declare(
    *,
    terminal_id: str = "44dda40b",
    generation: str = "6e3d642e",
    execution_mode: str = "native_tui",
    pane_id: str | None = "%12",
    intent: dict | None = None,
) -> tuple[dict, bool]:
    return na.declare(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
        pane_id=pane_id,
        intent=intent if intent is not None else _intent(),
    )


def _owner(record: dict) -> dict:
    return {
        "terminal_id": record["owner"]["terminal_id"],
        "generation": record["owner"]["generation"],
        "execution_mode": record["owner"]["execution_mode"],
    }


def _proof(record: dict, *, survivors: list | None = None) -> dict:
    return na.no_survivor_proof(
        provider=PROVIDER,
        native_session_id=SESSION,
        pane_id=record["owner"]["pane_id"],
        process_identity=record["owner"]["process_identity"],
        survivors=[] if survivors is None else survivors,
        observed_at="2026-07-24T00:00:00Z",
        observer="test",
        **_owner(record),
    )


def _advance_to_attached(**declare_kwargs) -> dict:
    """Walk one owner through declare → starting → attached."""
    record, _ = _declare(**declare_kwargs)
    owner = _owner(record)
    na.mark_starting(provider=PROVIDER, native_session_id=SESSION, **owner)
    return na.mark_attached(
        provider=PROVIDER,
        native_session_id=SESSION,
        process_identity=na.process_identity(pid=4242, start_marker="Jul24 00:00:00"),
        **owner,
    )


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    """Every test gets its own initialized attachment store."""
    return isolated_memory_db


class TestIntentObligations:
    """The intent journals obligations the caller must be able to assert."""

    def test_bootstrap_intent_records_the_no_turn_and_detach_assertions(self):
        intent = _intent()
        assert intent["acquisition_method"] == na.ACQUISITION_ACP_BOOTSTRAP
        assert intent["bootstrap_sent_no_turn"] is True
        assert intent["bootstrap_detached_before_launch"] is True
        assert intent["admits_only_new_instructions"] is True
        assert intent["replays_task_bytes"] is False

    def test_a_bootstrap_that_sent_a_turn_cannot_be_journaled(self):
        with pytest.raises(na.NativeAttachmentInvalid):
            na.acquire_intent(
                acquisition_method=na.ACQUISITION_ACP_BOOTSTRAP,
                acquisition_receipt={"kind": "kimi-acp-session-new"},
                admits_only_new_instructions=True,
                replays_task_bytes=False,
                bootstrap_sent_no_turn=False,
                bootstrap_detached_before_launch=True,
            )

    def test_a_bootstrap_still_attached_cannot_be_journaled(self):
        with pytest.raises(na.NativeAttachmentInvalid):
            na.acquire_intent(
                acquisition_method=na.ACQUISITION_ACP_BOOTSTRAP,
                acquisition_receipt={"kind": "kimi-acp-session-new"},
                admits_only_new_instructions=True,
                replays_task_bytes=False,
                bootstrap_sent_no_turn=True,
                bootstrap_detached_before_launch=False,
            )

    def test_replaying_task_bytes_cannot_be_journaled(self):
        with pytest.raises(na.NativeAttachmentInvalid):
            na.acquire_intent(
                acquisition_method=na.ACQUISITION_RESUME,
                acquisition_receipt={"kind": "kimi-session-resume"},
                admits_only_new_instructions=True,
                replays_task_bytes=True,
            )

    def test_re_admitting_old_instructions_cannot_be_journaled(self):
        with pytest.raises(na.NativeAttachmentInvalid):
            na.acquire_intent(
                acquisition_method=na.ACQUISITION_RESUME,
                acquisition_receipt={"kind": "kimi-session-resume"},
                admits_only_new_instructions=False,
                replays_task_bytes=False,
            )

    def test_bootstrap_assertions_are_refused_for_a_resume(self):
        """Unverifiable evidence is refused rather than stored as decoration."""
        with pytest.raises(na.NativeAttachmentInvalid):
            na.acquire_intent(
                acquisition_method=na.ACQUISITION_RESUME,
                acquisition_receipt={"kind": "kimi-session-resume"},
                admits_only_new_instructions=True,
                replays_task_bytes=False,
                bootstrap_sent_no_turn=True,
            )

    def test_an_unknown_acquisition_method_rejects(self):
        with pytest.raises(na.NativeAttachmentInvalid):
            na.acquire_intent(
                acquisition_method="tmux_paste",
                acquisition_receipt={"kind": "guess"},
                admits_only_new_instructions=True,
                replays_task_bytes=False,
            )

    def test_declare_refuses_an_intent_it_did_not_validate(self):
        with pytest.raises(na.NativeAttachmentInvalid):
            _declare(intent={"acquisition_method": na.ACQUISITION_RESUME})


class TestIntentPrecedesProviderLaunch:
    def test_declare_persists_the_claim_before_any_process_exists(self):
        record, acquired = _declare()
        assert acquired is True
        assert record["state"] == na.DECLARED
        assert record["owner"]["process_identity"] is None
        stored = na.get(PROVIDER, SESSION)
        assert stored["intent"]["schema"] == na.INTENT_SCHEMA
        assert stored["epoch"] == 0

    def test_lifecycle_reaches_attached_and_bumps_epoch_each_step(self):
        record, _ = _declare()
        owner = _owner(record)
        starting = na.mark_starting(provider=PROVIDER, native_session_id=SESSION, **owner)
        assert starting["state"] == na.STARTING
        attached = na.mark_attached(
            provider=PROVIDER,
            native_session_id=SESSION,
            process_identity=na.process_identity(pid=4242, start_marker="Jul24 00:00:00"),
            **owner,
        )
        assert attached["state"] == na.ATTACHED
        assert attached["owner"]["process_identity"] == {
            "pid": 4242,
            "start_marker": "Jul24 00:00:00",
        }
        assert [record["epoch"], starting["epoch"], attached["epoch"]] == [0, 1, 2]

    def test_a_bare_pid_is_not_a_process_identity(self):
        with pytest.raises(na.NativeAttachmentInvalid):
            na.process_identity(pid=4242, start_marker="")

    def test_publication_requires_a_start_marker(self):
        record, _ = _declare()
        owner = _owner(record)
        na.mark_starting(provider=PROVIDER, native_session_id=SESSION, **owner)
        with pytest.raises(na.NativeAttachmentInvalid):
            na.mark_attached(
                provider=PROVIDER,
                native_session_id=SESSION,
                process_identity={"pid": 4242},
                **owner,
            )


class TestConcurrentAttachmentRefused:
    def test_native_vs_native_concurrent_attachment_is_refused(self):
        _advance_to_attached()
        with pytest.raises(na.NativeAttachmentConflict) as exc:
            _declare(terminal_id="99999999", generation="aaaaaaaa")
        assert "already attached" in str(exc.value)

    def test_acp_vs_native_concurrent_attachment_is_refused(self):
        _advance_to_attached(execution_mode="native_tui")
        with pytest.raises(na.NativeAttachmentConflict) as exc:
            _declare(
                terminal_id="99999999",
                generation="aaaaaaaa",
                execution_mode="acp",
                intent=_intent(na.ACQUISITION_RESUME),
            )
        assert "never attach to one provider session" in str(exc.value)

    def test_native_vs_acp_concurrent_attachment_is_refused(self):
        _advance_to_attached(
            execution_mode="acp",
            intent=_intent(na.ACQUISITION_RESUME),
        )
        with pytest.raises(na.NativeAttachmentConflict) as exc:
            _declare(terminal_id="99999999", generation="aaaaaaaa", execution_mode="native_tui")
        assert "never attach to one provider session" in str(exc.value)

    @pytest.mark.parametrize("state_after", ["declared", "starting", "attached", "draining"])
    def test_every_live_state_refuses_a_second_acquirer(self, state_after):
        record, _ = _declare()
        owner = _owner(record)
        if state_after != "declared":
            na.mark_starting(provider=PROVIDER, native_session_id=SESSION, **owner)
        if state_after in {"attached", "draining"}:
            na.mark_attached(
                provider=PROVIDER,
                native_session_id=SESSION,
                process_identity=na.process_identity(pid=4242, start_marker="Jul24"),
                **owner,
            )
        if state_after == "draining":
            na.mark_draining(provider=PROVIDER, native_session_id=SESSION, **owner)
        with pytest.raises(na.NativeAttachmentConflict):
            _declare(terminal_id="99999999", generation="aaaaaaaa")

    def test_a_different_generation_of_the_same_terminal_is_still_a_stranger(self):
        """A relaunch is a new generation and must not inherit the claim."""
        _advance_to_attached()
        with pytest.raises(na.NativeAttachmentConflict):
            _declare(generation="ffffffff")

    def test_a_non_owner_cannot_advance_the_state_machine(self):
        _declare()
        with pytest.raises(na.NativeAttachmentConflict):
            na.mark_starting(
                provider=PROVIDER,
                native_session_id=SESSION,
                terminal_id="99999999",
                generation="aaaaaaaa",
                execution_mode="native_tui",
            )

    def test_an_acp_operation_cannot_advance_a_native_attachment(self):
        record, _ = _declare()
        with pytest.raises(na.NativeAttachmentConflict):
            na.mark_starting(
                provider=PROVIDER,
                native_session_id=SESSION,
                terminal_id=record["owner"]["terminal_id"],
                generation=record["owner"]["generation"],
                execution_mode="acp",
            )

    def test_a_second_session_id_is_an_independent_attachment(self):
        _advance_to_attached()
        other, acquired = na.declare(
            provider=PROVIDER,
            native_session_id="session_other",
            terminal_id="99999999",
            generation="aaaaaaaa",
            execution_mode="native_tui",
            intent=_intent(),
        )
        assert acquired is True
        assert other["state"] == na.DECLARED


class TestReleaseRequiresNoSurvivorProof:
    def test_release_with_an_exact_proof_detaches(self):
        attached = _advance_to_attached()
        na.mark_draining(provider=PROVIDER, native_session_id=SESSION, **_owner(attached))
        released = na.release(
            provider=PROVIDER,
            native_session_id=SESSION,
            proof=_proof(attached),
            **_owner(attached),
        )
        assert released["state"] == na.DETACHED
        assert released["release_proof"]["survivors"] == []
        assert na.is_held(PROVIDER, SESSION) is False

    def test_release_without_a_proof_is_refused(self):
        attached = _advance_to_attached()
        with pytest.raises(na.NativeAttachmentInvalid):
            na.release(
                provider=PROVIDER,
                native_session_id=SESSION,
                proof={"released": True},
                **_owner(attached),
            )
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_release_with_a_reported_survivor_is_refused(self):
        attached = _advance_to_attached()
        with pytest.raises(na.NativeAttachmentConflict) as exc:
            na.release(
                provider=PROVIDER,
                native_session_id=SESSION,
                proof=_proof(attached, survivors=[{"pid": 4242}]),
                **_owner(attached),
            )
        assert "still" in str(exc.value)
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_an_absent_survivor_observation_is_not_a_proof_of_absence(self):
        attached = _advance_to_attached()
        proof = _proof(attached)
        del proof["survivors"]
        with pytest.raises(na.NativeAttachmentInvalid) as exc:
            na.release(
                provider=PROVIDER,
                native_session_id=SESSION,
                proof=proof,
                **_owner(attached),
            )
        assert "never having looked" in str(exc.value)

    def test_a_proof_naming_the_wrong_process_identity_is_refused(self):
        attached = _advance_to_attached()
        proof = _proof(attached)
        proof["process_identity"] = {"pid": 4242, "start_marker": "Jul23 11:11:11"}
        with pytest.raises(na.NativeAttachmentConflict):
            na.release(
                provider=PROVIDER,
                native_session_id=SESSION,
                proof=proof,
                **_owner(attached),
            )

    def test_a_recycled_pid_alone_cannot_release(self):
        """Same pid, different start marker — a recycled pid, not the owner."""
        attached = _advance_to_attached()
        proof = _proof(attached)
        proof["process_identity"] = {
            "pid": attached["owner"]["process_identity"]["pid"],
            "start_marker": "Jul24 23:59:59",
        }
        with pytest.raises(na.NativeAttachmentConflict):
            na.release(
                provider=PROVIDER,
                native_session_id=SESSION,
                proof=proof,
                **_owner(attached),
            )

    def test_a_proof_for_another_pane_is_refused(self):
        attached = _advance_to_attached()
        proof = _proof(attached)
        proof["pane_id"] = "%99"
        with pytest.raises(na.NativeAttachmentConflict) as exc:
            na.release(
                provider=PROVIDER,
                native_session_id=SESSION,
                proof=proof,
                **_owner(attached),
            )
        assert "pane_id" in str(exc.value)

    def test_a_non_owner_cannot_release_even_with_a_wellformed_proof(self):
        attached = _advance_to_attached()
        proof = _proof(attached)
        with pytest.raises(na.NativeAttachmentConflict):
            na.release(
                provider=PROVIDER,
                native_session_id=SESSION,
                terminal_id="99999999",
                generation="aaaaaaaa",
                execution_mode="native_tui",
                proof=proof,
            )
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_release_is_idempotent_for_the_releasing_owner(self):
        attached = _advance_to_attached()
        first = na.release(
            provider=PROVIDER, native_session_id=SESSION, proof=_proof(attached), **_owner(attached)
        )
        second = na.release(
            provider=PROVIDER, native_session_id=SESSION, proof=_proof(attached), **_owner(attached)
        )
        assert first["epoch"] == second["epoch"]
        assert second["state"] == na.DETACHED


class TestAmbiguityFreezes:
    def test_ambiguity_preserves_the_owner(self):
        attached = _advance_to_attached()
        frozen = na.mark_ambiguous(
            provider=PROVIDER,
            native_session_id=SESSION,
            reason="pane process tree unreadable",
        )
        assert frozen["state"] == na.AMBIGUOUS
        assert frozen["owner"] == attached["owner"]
        assert frozen["ambiguity_reason"] == "pane process tree unreadable"

    def test_an_ambiguous_attachment_is_never_released(self):
        attached = _advance_to_attached()
        proof = _proof(attached)
        na.mark_ambiguous(provider=PROVIDER, native_session_id=SESSION, reason="unresolved")
        with pytest.raises(na.NativeAttachmentConflict) as exc:
            na.release(
                provider=PROVIDER, native_session_id=SESSION, proof=proof, **_owner(attached)
            )
        assert "frozen ambiguous" in str(exc.value)
        assert na.get(PROVIDER, SESSION)["state"] == na.AMBIGUOUS

    def test_an_ambiguous_attachment_blocks_every_future_claim(self):
        _advance_to_attached()
        na.mark_ambiguous(provider=PROVIDER, native_session_id=SESSION, reason="unresolved")
        with pytest.raises(na.NativeAttachmentConflict) as exc:
            _declare(terminal_id="99999999", generation="aaaaaaaa")
        # Asserted on the frozen reason specifically: a refusal that merely
        # fell out of a failed compare-and-swap would be an accident, and a
        # later refactor could silently turn it into a successful claim.
        assert "frozen ambiguous" in str(exc.value)

    def test_even_the_original_owner_cannot_reclaim_a_frozen_session(self):
        _advance_to_attached()
        na.mark_ambiguous(provider=PROVIDER, native_session_id=SESSION, reason="unresolved")
        with pytest.raises(na.NativeAttachmentConflict) as exc:
            _declare()
        assert "frozen ambiguous" in str(exc.value)

    def test_the_owner_cannot_advance_a_frozen_attachment(self):
        attached = _advance_to_attached()
        na.mark_ambiguous(provider=PROVIDER, native_session_id=SESSION, reason="unresolved")
        with pytest.raises(na.NativeAttachmentConflict) as exc:
            na.mark_draining(provider=PROVIDER, native_session_id=SESSION, **_owner(attached))
        assert "frozen ambiguous" in str(exc.value)

    def test_freezing_is_idempotent(self):
        _advance_to_attached()
        first = na.mark_ambiguous(provider=PROVIDER, native_session_id=SESSION, reason="one")
        second = na.mark_ambiguous(provider=PROVIDER, native_session_id=SESSION, reason="two")
        assert first["epoch"] == second["epoch"]
        assert second["ambiguity_reason"] == "one"

    def test_a_proven_detached_row_has_no_owner_to_freeze(self):
        attached = _advance_to_attached()
        na.release(
            provider=PROVIDER, native_session_id=SESSION, proof=_proof(attached), **_owner(attached)
        )
        with pytest.raises(na.NativeAttachmentConflict) as exc:
            na.mark_ambiguous(provider=PROVIDER, native_session_id=SESSION, reason="late doubt")
        assert "no owner to freeze" in str(exc.value)

    def test_a_frozen_attachment_still_counts_as_held(self):
        _advance_to_attached()
        na.mark_ambiguous(provider=PROVIDER, native_session_id=SESSION, reason="unresolved")
        assert na.is_held(PROVIDER, SESSION) is True


class TestCrashPointsConverge:
    """Each crash point leaves exactly one adjudicable durable state."""

    def test_crash_before_process_start_leaves_a_declared_claim(self):
        _declare()
        recovered = na.get(PROVIDER, SESSION)
        assert recovered["state"] == na.DECLARED
        assert recovered["owner"]["process_identity"] is None
        assert recovered["intent"]["acquisition_method"] == na.ACQUISITION_ACP_BOOTSTRAP

    def test_crash_after_start_before_publication_leaves_starting(self):
        record, _ = _declare()
        na.mark_starting(provider=PROVIDER, native_session_id=SESSION, **_owner(record))
        recovered = na.get(PROVIDER, SESSION)
        assert recovered["state"] == na.STARTING
        assert recovered["owner"]["process_identity"] is None

    def test_an_unpublished_owner_releases_only_on_a_null_identity_proof(self):
        """`starting` never published an identity, so the proof must say so."""
        record, _ = _declare()
        starting = na.mark_starting(provider=PROVIDER, native_session_id=SESSION, **_owner(record))
        released = na.release(
            provider=PROVIDER,
            native_session_id=SESSION,
            proof=_proof(starting),
            **_owner(starting),
        )
        assert released["state"] == na.DETACHED

    def test_an_unpublished_owner_cannot_be_released_by_inventing_an_identity(self):
        record, _ = _declare()
        starting = na.mark_starting(provider=PROVIDER, native_session_id=SESSION, **_owner(record))
        proof = _proof(starting)
        proof["process_identity"] = {"pid": 4242, "start_marker": "Jul24 00:00:00"}
        with pytest.raises(na.NativeAttachmentConflict):
            na.release(
                provider=PROVIDER, native_session_id=SESSION, proof=proof, **_owner(starting)
            )

    def test_crash_after_publication_leaves_the_exact_identity(self):
        _advance_to_attached()
        recovered = na.get(PROVIDER, SESSION)
        assert recovered["state"] == na.ATTACHED
        assert recovered["owner"]["process_identity"] == {
            "pid": 4242,
            "start_marker": "Jul24 00:00:00",
        }

    def test_replaying_declare_after_a_crash_does_not_regress_state(self):
        """The owner lost its memory, not its identity: no state regression."""
        attached = _advance_to_attached()
        replayed, acquired = _declare()
        assert acquired is False
        assert replayed["state"] == na.ATTACHED
        assert replayed["epoch"] == attached["epoch"]

    def test_replaying_mark_starting_after_a_crash_is_idempotent(self):
        record, _ = _declare()
        first = na.mark_starting(provider=PROVIDER, native_session_id=SESSION, **_owner(record))
        second = na.mark_starting(provider=PROVIDER, native_session_id=SESSION, **_owner(record))
        assert first["epoch"] == second["epoch"]

    def test_publication_cannot_skip_the_starting_state(self):
        record, _ = _declare()
        with pytest.raises(na.NativeAttachmentConflict):
            na.mark_attached(
                provider=PROVIDER,
                native_session_id=SESSION,
                process_identity=na.process_identity(pid=4242, start_marker="Jul24"),
                **_owner(record),
            )


class TestResumeBindsBeforeAdmission:
    def test_a_new_generation_claims_the_session_only_after_a_proven_release(self):
        attached = _advance_to_attached()
        na.release(
            provider=PROVIDER, native_session_id=SESSION, proof=_proof(attached), **_owner(attached)
        )
        resumed, acquired = _declare(
            terminal_id="1ad645c8",
            generation="bbbbbbbb",
            pane_id="%40",
            intent=_intent(na.ACQUISITION_RESUME),
        )
        assert acquired is True
        assert resumed["state"] == na.DECLARED
        assert resumed["owner"]["generation"] == "bbbbbbbb"
        assert resumed["intent"]["acquisition_method"] == na.ACQUISITION_RESUME
        assert resumed["intent"]["replays_task_bytes"] is False

    def test_a_resume_claim_supersedes_the_owner_but_keeps_the_release_evidence(self):
        attached = _advance_to_attached()
        na.release(
            provider=PROVIDER, native_session_id=SESSION, proof=_proof(attached), **_owner(attached)
        )
        resumed, _ = _declare(
            terminal_id="1ad645c8", generation="bbbbbbbb", intent=_intent(na.ACQUISITION_RESUME)
        )
        assert resumed["release_proof"]["terminal_id"] == attached["owner"]["terminal_id"]
        assert resumed["epoch"] > attached["epoch"]

    def test_a_resume_cannot_claim_a_session_that_was_never_released(self):
        _advance_to_attached()
        with pytest.raises(na.NativeAttachmentConflict):
            _declare(
                terminal_id="1ad645c8",
                generation="bbbbbbbb",
                intent=_intent(na.ACQUISITION_RESUME),
            )


class TestStoreFailsClosed:
    def test_operations_on_an_unclaimed_session_do_not_invent_one(self):
        assert na.get(PROVIDER, SESSION) is None
        assert na.is_held(PROVIDER, SESSION) is False
        with pytest.raises(na.NativeAttachmentNotFound):
            na.mark_starting(
                provider=PROVIDER,
                native_session_id=SESSION,
                terminal_id="44dda40b",
                generation="6e3d642e",
                execution_mode="native_tui",
            )

    def test_an_unreadable_store_fails_closed_rather_than_reporting_free(self, monkeypatch):
        def _broken():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(na.database, "SessionLocal", _broken)
        with pytest.raises(na.NativeAttachmentUnavailable):
            na.get(PROVIDER, SESSION)
        with pytest.raises(na.NativeAttachmentUnavailable):
            _declare()

    def test_an_invalid_execution_mode_never_reaches_the_store(self):
        with pytest.raises(em.ExecutionModeInvalid):
            _declare(execution_mode="native")
        assert na.get(PROVIDER, SESSION) is None


def _adjudication(**overrides) -> dict:
    fields = {
        "outcome": na.ADJUDICATION_OUTCOME_OWNER_GONE,
        "evidence_sha256": "a" * 64,
        "detail": "pane and process both gone for six days; scrollback archived",
        "operator": "colin",
        "observed_at": "2026-08-07T00:00:00Z",
        "observation": {"disposition": "unpublished"},
    }
    fields.update(overrides)
    return na.adjudication(**fields)


class TestOperatorAdjudicationIsTheOnlyWayOutOfFrozen:
    """The valve the module's own docstring promised and did not have.

    "A human resolves it" described no reachable code: `release` refuses a
    frozen row before it looks at a proof, `mark_ambiguous` has no inverse,
    and nothing exposed either one. So every frozen session on an install
    stayed unresumable, and the only remedy was editing the database.

    What must stay true is that this is not automation with a nicer name.
    It records who decided and what they saw, it refuses an owner still
    observably alive, and its evidence is stored under a schema no machine
    proof uses.
    """

    def _freeze(self) -> dict:
        _declare()
        return na.mark_ambiguous(
            provider=PROVIDER, native_session_id=SESSION, reason="pane_create_outcome_unknown"
        )

    def test_release_still_refuses_a_frozen_attachment(self):
        record = self._freeze()
        with pytest.raises(na.NativeAttachmentConflict):
            na.release(
                provider=PROVIDER,
                native_session_id=SESSION,
                proof=_proof(record),
                **_owner(record),
            )

    def test_adjudication_moves_a_frozen_row_to_detached(self):
        self._freeze()
        result = na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=_adjudication(),
            live_survivors=[],
            observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
        )
        assert result["state"] == na.DETACHED

    def test_an_adjudicated_session_can_be_claimed_again(self):
        self._freeze()
        na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=_adjudication(),
            live_survivors=[],
            observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
        )
        _, acquired = _declare(terminal_id="ffffffff", generation="99999999")
        assert acquired is True

    def test_a_still_living_owner_is_refused_even_by_an_operator(self):
        self._freeze()
        with pytest.raises(na.NativeAttachmentConflict):
            na.adjudicate(
                provider=PROVIDER,
                native_session_id=SESSION,
                record=_adjudication(),
                live_survivors=[{"pid": 4242, "start_marker": "Jul24 00:00:00"}],
                observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
            )
        assert na.get(PROVIDER, SESSION)["state"] == na.AMBIGUOUS

    def test_an_absent_survivor_observation_is_refused(self):
        """Omitting the observation is how the row froze in the first place."""
        self._freeze()
        with pytest.raises(na.NativeAttachmentInvalid):
            na.adjudicate(
                provider=PROVIDER,
                native_session_id=SESSION,
                record=_adjudication(),
                live_survivors=None,
                observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
            )

    def test_the_owner_and_the_freeze_reason_are_preserved(self):
        """The record of what happened outlives the decision to move past it."""
        frozen = self._freeze()
        result = na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=_adjudication(),
            live_survivors=[],
            observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
        )
        assert result["owner"] == frozen["owner"]
        assert result["ambiguity_reason"] == "pane_create_outcome_unknown"
        assert result["epoch"] == frozen["epoch"] + 1

    def test_the_stored_evidence_is_distinguishable_from_a_machine_proof(self):
        self._freeze()
        na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=_adjudication(),
            live_survivors=[],
            observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
        )
        stored = na.get(PROVIDER, SESSION)["release_proof"]
        assert stored["schema"] == na.ADJUDICATION_SCHEMA
        assert stored["schema"] != na.NO_SURVIVOR_PROOF_SCHEMA
        assert stored["operator"] == "colin"

    def test_an_identical_replay_is_idempotent(self):
        self._freeze()
        first = na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=_adjudication(),
            live_survivors=[],
            observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
        )
        second = na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=_adjudication(),
            live_survivors=[],
            observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
        )
        assert second == first

    def test_a_contradicting_replay_is_a_conflict(self):
        """Two people must not both believe their reasoning is on record."""
        self._freeze()
        na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=_adjudication(),
            live_survivors=[],
            observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
        )
        with pytest.raises(na.NativeAttachmentConflict):
            na.adjudicate(
                provider=PROVIDER,
                native_session_id=SESSION,
                record=_adjudication(operator="someone-else"),
                live_survivors=[],
                observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
            )

    def test_a_machine_released_row_has_nothing_to_adjudicate(self):
        record = _advance_to_attached()
        na.release(
            provider=PROVIDER, native_session_id=SESSION, proof=_proof(record), **_owner(record)
        )
        with pytest.raises(na.NativeAttachmentConflict):
            na.adjudicate(
                provider=PROVIDER,
                native_session_id=SESSION,
                record=_adjudication(),
                live_survivors=[],
                observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
            )

    @pytest.mark.parametrize("state", [na.DECLARED, na.STARTING, na.ATTACHED, na.DRAINING])
    def test_a_live_attachment_is_released_by_proof_not_by_adjudication(self, state):
        record, _ = _declare()
        owner = _owner(record)
        if state != na.DECLARED:
            na.mark_starting(provider=PROVIDER, native_session_id=SESSION, **owner)
        if state in (na.ATTACHED, na.DRAINING):
            na.mark_attached(
                provider=PROVIDER,
                native_session_id=SESSION,
                process_identity=na.process_identity(pid=4242, start_marker="Jul24 00:00:00"),
                **owner,
            )
        if state == na.DRAINING:
            na.mark_draining(provider=PROVIDER, native_session_id=SESSION, **owner)
        with pytest.raises(na.NativeAttachmentConflict):
            na.adjudicate(
                provider=PROVIDER,
                native_session_id=SESSION,
                record=_adjudication(),
                live_survivors=[],
                observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
            )

    def test_an_unvalidated_record_cannot_release_a_frozen_session(self):
        self._freeze()
        with pytest.raises(na.NativeAttachmentInvalid):
            na.adjudicate(
                provider=PROVIDER,
                native_session_id=SESSION,
                record={"outcome": "owner-proven-gone"},
                live_survivors=[],
                observed_epoch=na.get(PROVIDER, SESSION)["epoch"],
            )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"outcome": "probably-fine"},
            {"evidence_sha256": "not-a-digest"},
            {"evidence_sha256": "A" * 64},
            {"detail": ""},
            {"detail": "x" * 501},
            {"operator": ""},
        ],
    )
    def test_an_unattributable_adjudication_is_refused_before_the_store(self, overrides):
        with pytest.raises(na.NativeAttachmentInvalid):
            _adjudication(**overrides)


class TestListingTheHeldSessions:
    """Nothing could answer "what is still held?" — which is why a two-week
    leak of every claim on an install went unseen."""

    def test_listing_filters_by_state(self):
        _advance_to_attached()
        assert [r["state"] for r in na.list_attachments(states=frozenset({na.ATTACHED}))] == [
            na.ATTACHED
        ]
        assert na.list_attachments(states=frozenset({na.AMBIGUOUS})) == []

    def test_listing_filters_by_owner_terminal(self):
        _advance_to_attached()
        assert len(na.list_attachments(owner_terminal_id="44dda40b")) == 1
        assert na.list_attachments(owner_terminal_id="somebody-else") == []

    def test_an_unknown_state_is_refused_rather_than_matching_nothing(self):
        """A typo that returns an empty list reads as "nothing is held"."""
        with pytest.raises(na.NativeAttachmentInvalid):
            na.list_attachments(states=frozenset({"detatched"}))


class TestAnAbsentTableIsNoClaims:
    def test_listing_a_store_no_server_has_created_is_empty(self, tmp_path, monkeypatch):
        """Not a relaxation of fail-closed: the migration ladder creates this
        table at server start, so its absence proves no claim was ever taken."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(f"sqlite:///{tmp_path}/empty.db")
        monkeypatch.setattr(na.database, "SessionLocal", sessionmaker(bind=engine))
        assert na.list_attachments() == []

    def test_a_table_that_exists_but_cannot_be_read_still_raises(self, monkeypatch):
        """A different fact, and it must not be reported as "nothing is held"."""

        def _broken():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(na.database, "SessionLocal", _broken)
        with pytest.raises(na.NativeAttachmentUnavailable):
            na.list_attachments()


class TestAdjudicationIsBoundToTheRowItDescribes:
    """An adjudication names no owner. The epoch is the only thing tying it
    to the row it was written about.

    `release` is safe without an epoch because its proof names the exact
    owner and is re-validated against the stored row inside the writing
    transaction. An adjudication is a human's sentence — there is nothing
    in it to compare — so an observation taken before a confirmation prompt
    can otherwise be applied after the session was released, re-claimed by
    a new launch, and frozen again with a live process on it. The state
    still reads `ambiguous` and the stale survivor list still reads empty.
    """

    def _frozen(self) -> dict:
        _declare()
        return na.mark_ambiguous(
            provider=PROVIDER, native_session_id=SESSION, reason="pane_create_outcome_unknown"
        )

    def test_an_observation_of_an_older_row_version_is_refused(self):
        stale = self._frozen()["epoch"]

        # The session goes round the loop: adjudicated by someone else, re-claimed,
        # and frozen again — this time by a launch whose process is running.
        na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=_adjudication(operator="first-operator"),
            live_survivors=[],
            observed_epoch=stale,
        )
        _declare(terminal_id="ffffffff", generation="99999999")
        na.mark_ambiguous(
            provider=PROVIDER, native_session_id=SESSION, reason="pane_render_mismatch"
        )

        with pytest.raises(na.NativeAttachmentConflict, match="moved to epoch"):
            na.adjudicate(
                provider=PROVIDER,
                native_session_id=SESSION,
                record=_adjudication(operator="second-operator"),
                live_survivors=[],
                observed_epoch=stale,
            )
        current = na.get(PROVIDER, SESSION)
        assert current["state"] == na.AMBIGUOUS
        assert current["owner"]["terminal_id"] == "ffffffff"

    def test_the_current_epoch_is_accepted(self):
        frozen = self._frozen()
        result = na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=_adjudication(),
            live_survivors=[],
            observed_epoch=frozen["epoch"],
        )
        assert result["state"] == na.DETACHED

    def test_the_binding_is_required_rather_than_optional(self):
        """An optional gate leaves the hole open for the next caller."""
        import inspect

        signature = inspect.signature(na.adjudicate)
        assert signature.parameters["observed_epoch"].default is inspect.Parameter.empty
