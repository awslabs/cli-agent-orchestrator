"""The v2 cleanup contract: state transition, durable proof, idempotency.

``managed_launch_v2.cleanup`` deletes the v2 terminal metadata row and
returns a well-formed ``cao-managed-launch-v2-cleanup-v1`` proof, and then
writes nothing at all. The reservation stays ``negative``, so a consumer
that requires ``cleaned`` never observes it, and the generation is
indistinguishable from one that was never cleaned.

The v1 verb in ``managed_launch.py`` does the whole thing: it persists the
proof keyed by ``cleanup_id``, returns the *stored* proof on replay, and
transitions the row to ``cleaned``. The v2 verb is a partial port of it --
it kept the delete and dropped the ledger.

Four separate facts follow, reproduced against a real reservation:

1. state stays ``negative`` after a successful cleanup
2. the proof is absent from ``get()``, so a lost response is unrecoverable
3. replaying the SAME ``cleanup_id`` mints a NEW ``cleaned_at``
4. a DIFFERENT ``cleanup_id`` is accepted just as readily

Both the service docstring and the request model docstring say "Idempotent
by ``cleanup_id``". Nothing reads or stores that field, so (3) and (4) are
the docstring being false rather than a subtle edge.

Reconciled to §24.12 / E(xi): the response PROJECTION says ``cleaned``,
derived from a durable cleanup record written once (first-writer-wins by
``cleanup_id``), while the durable finalization state stays ``negative``
and remains readable. Absence of the terminal row never projects
``cleaned``. A different ``cleanup_id`` is a conflict, not a second
cleanup.
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2CleanupRequest,
    ManagedLaunchV2NegativeRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services.managed_launch import ManagedLaunchConflict

#: The state a cleaned generation is expected to reach. v1 uses exactly
#: this string, and a consumer requiring it is why this issue was opened.
#: Named once so the amendment can move it in one place if it chooses a
#: different spelling.
CLEANED = "cleaned"


@pytest.fixture
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")


@pytest.fixture(autouse=True)
def _stub_native_teardown(monkeypatch):
    """The managed cleanup now drives the generation-bound terminal teardown.

    These contract tests assert the cleanup *record* — the proof,
    idempotency, conflict, and projection — not tmux process teardown, so
    the exact teardown is stubbed to a confirming no-op that records its
    identity arguments. The teardown itself is exercised in
    ``test_v2_cleanup_teardown.py``.
    """

    def _delete_terminal(
        terminal_id, *, registry=None, expected_generation=None, expected_session=None, **_
    ):
        return True

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.delete_terminal",
        _delete_terminal,
    )


@pytest.fixture
def worktree(tmp_path):
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def finalized(worktree, tmp_path, _companion, isolated_memory_db):
    """A real reservation driven to ``negative`` with its terminal row present.

    Driven through the actual verbs rather than assembled, so the cleanup
    under test receives the row the production path produces.
    """
    executable = tmp_path / "fake-kimi"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    record, _created = v2.reserve(
        ManagedLaunchV2ReserveRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            reservation_id=str(uuid.uuid4()),
            session_name="cao-chess-shakedown",
            provider="kimi_cli",
            agent_profile="reviewer",
            caller_id="deadbeef",
            working_directory=str(worktree),
            expected_model="kimi-code/kimi-for-coding",
            expected_effort="provider-default",
            provider_executable=str(executable),
            provider_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            obligation_generation="obgen-7c2e4a1b",
            task_id="self-heal-demo-task",
            run_id="run-0001",
            delivery_id=str(uuid.uuid4()),
            launch_nonce="n" * 40,
            execution_mode="native_tui",
            worker_class="persistent",
        )
    )
    database.create_terminal_v2(
        terminal_id=record["terminal_id"],
        tmux_session="cao-chess-shakedown",
        tmux_window="w",
        provider="kimi_cli",
        generation=record["generation"],
        pane_id="%30",
        window_id="@30",
        server_socket_path="/private/tmp/tmux-501/default",
        session_id="$7",
        pane_pid=54321,
    )
    with database.SessionLocal() as db:
        db.query(database.ManagedLaunchV2ReservationModel).filter(
            database.ManagedLaunchV2ReservationModel.reservation_id == record["reservation_id"]
        ).update({"state": "launching"}, synchronize_session=False)
        db.commit()
    v2.finalize_negative(
        record["reservation_id"],
        ManagedLaunchV2NegativeRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            finalize_id=str(uuid.uuid4()),
            terminal_id=record["terminal_id"],
            generation=record["generation"],
            obligation_generation=record["obligation_generation"],
            reason="launch never reached its bind",
        ),
    )
    return record


def _cleanup_request(record, cleanup_id: str) -> ManagedLaunchV2CleanupRequest:
    return ManagedLaunchV2CleanupRequest(
        protocol_version=PROTOCOL_VERSION_V2,
        cleanup_id=cleanup_id,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        obligation_generation=record["obligation_generation"],
    )


class TestWhatCleanupAlreadyDoesCorrectly:
    """Pinned first, so the repair is visibly additive.

    Whatever the amendment decides, none of this may regress.
    """

    def test_it_removes_the_v2_terminal_metadata_row(self, finalized):
        v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4())))

        assert database.get_terminal_metadata_v2(finalized["terminal_id"]) is None

    def test_it_returns_a_well_formed_proof(self, finalized):
        result = v2.cleanup(
            finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4()))
        )

        assert result["cleanup"]["schema"] == "cao-managed-launch-v2-cleanup-v1"
        assert result["cleanup"]["terminal_record_removed"] is True

    def test_a_retry_still_succeeds_and_no_longer_reports_a_false_removal(self, finalized):
        """Supersedes the old "idempotent by effect" behaviour.

        Before §24.12 a retry recomputed the proof, so the second call
        reported ``terminal_record_removed: false`` -- truthfully about
        *that* delete, and misleadingly about the cleanup. The removal is
        now attributed permanently to the call that performed it, so a
        retry replays ``true``.

        The retry uses the SAME cleanup_id, because that is what a retry
        is; a different id is a second cleanup and is refused elsewhere.
        """
        cleanup_id = str(uuid.uuid4())

        first = v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))
        again = v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))

        assert first["cleanup"]["terminal_record_removed"] is True
        assert again["cleanup"]["terminal_record_removed"] is True

    def test_it_still_refuses_a_generation_that_is_not_finalized(
        self, worktree, tmp_path, _companion, isolated_memory_db
    ):
        """The live-generation guard must survive the repair."""
        from cli_agent_orchestrator.services.managed_launch import ManagedLaunchConflict

        executable = tmp_path / "fake-kimi"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        record, _created = v2.reserve(
            ManagedLaunchV2ReserveRequest(
                protocol_version=PROTOCOL_VERSION_V2,
                reservation_id=str(uuid.uuid4()),
                session_name="cao-chess-shakedown",
                provider="kimi_cli",
                agent_profile="reviewer",
                caller_id="deadbeef",
                working_directory=str(worktree),
                expected_model="kimi-code/kimi-for-coding",
                expected_effort="provider-default",
                provider_executable=str(executable),
                provider_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
                obligation_generation="obgen-7c2e4a1b",
                task_id="t",
                run_id="run-0001",
                delivery_id=str(uuid.uuid4()),
                launch_nonce="n" * 40,
                execution_mode="native_tui",
                worker_class="persistent",
            )
        )

        with pytest.raises(ManagedLaunchConflict, match="negative"):
            v2.cleanup(record["reservation_id"], _cleanup_request(record, str(uuid.uuid4())))


class TestTheCrossContractGap:
    """FAILS TODAY. The consumer requires a state the producer never writes."""

    def test_a_cleaned_generation_reports_the_cleaned_state(self, finalized):
        """The reported issue, at its narrowest.

        Cleanup succeeds and returns a valid proof while top-level state
        stays ``negative``, so a consumer that gates on ``cleaned`` waits
        for a transition that is never written.
        """
        result = v2.cleanup(
            finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4()))
        )

        assert result["state"] == CLEANED

    def test_the_transition_is_durable_not_only_in_the_response(self, finalized):
        """A state only present in one response is not a state."""
        v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4())))

        assert v2.get(finalized["reservation_id"])["state"] == CLEANED

    def test_v1_already_does_this(self, finalized):
        """Cross-contract anchor: v2 is a partial port, not a new design.

        Stated as an assertion rather than a comment so that if v1's own
        transition ever disappears, this stops silently claiming a
        precedent that no longer exists.
        """
        import inspect

        from cli_agent_orchestrator.services import managed_launch as v1

        assert 'row.state = "cleaned"' in inspect.getsource(v1.cleanup_reserved)


class TestTheLostResponseGap:
    """FAILS TODAY. A cleanup whose response is lost cannot be reconciled."""

    def test_the_proof_survives_the_response(self, finalized):
        """The caller's only copy is the response it may never receive.

        Every other v2 verb journals its evidence so an interrupted call
        can be reconciled by exact id; cleanup keeps nothing, so the row
        after a lost response is byte-identical to one never cleaned.
        """
        issued = v2.cleanup(
            finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4()))
        )["cleanup"]

        recovered = v2.get(finalized["reservation_id"])

        assert recovered.get("cleanup") == issued

    def test_a_cleaned_generation_is_distinguishable_from_a_crashed_one(self, finalized):
        """What a reconciling caller actually has to decide.

        It must tell "already cleaned, stop" from "never cleaned, retry".
        The absence of the terminal row cannot answer that: a launch that
        died before writing one produces the same absence, so a caller
        reading only that would treat a never-cleaned generation as done.

        Naive versions of this test pass by accident, because deleting the
        row does change ``get()`` once. Asking it the way reconciliation
        asks -- against a control that was never cleaned, in the steady
        state where neither has a terminal row -- is what exposes that the
        two are identical.
        """
        # The control: same finalized generation, terminal row already gone,
        # cleanup never called. This is what a crashed launch leaves behind.
        database.delete_terminal_v2(finalized["terminal_id"])
        never_cleaned = v2.get(finalized["reservation_id"])

        v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4())))
        cleaned = v2.get(finalized["reservation_id"])

        assert cleaned != never_cleaned


class TestTheIdempotencyGap:
    """FAILS TODAY. Both docstrings promise idempotency by ``cleanup_id``.

    Nothing reads or stores that field, so the promise is not weakly held
    -- it is absent.
    """

    def test_replaying_one_cleanup_id_returns_the_same_proof(self, finalized):
        """A replay must return the STORED proof, not mint a second one.

        Two receipts for one logical cleanup, differing in ``cleaned_at``,
        means neither is authoritative -- and a caller comparing them
        cannot tell a replay from a genuinely new cleanup.
        """
        cleanup_id = str(uuid.uuid4())

        first = v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))
        replay = v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))

        assert replay["cleanup"] == first["cleanup"]

    def test_a_second_distinct_cleanup_id_is_a_conflict(self, finalized):
        """Exactly one cleanup is authoritative for one generation.

        §24.12 decided this direction: a different id is a conflict rather
        than converging on the stored proof, because it is a second cleanup
        of one generation, not a retry of the first.
        """
        first = v2.cleanup(
            finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4()))
        )["cleanup"]

        with pytest.raises(ManagedLaunchConflict, match="already cleaned"):
            v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4())))

        assert v2.get(finalized["reservation_id"])["cleanup"] == first


class TestHistoryIsNotRewritten:
    """The projection must not cost the finalization verdict.

    These are two facts about different things: the verdict says how the
    generation ended, cleanup says what happened to its resources.
    """

    def test_the_durable_finalization_state_is_unchanged(self, finalized):
        v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4())))

        with database.SessionLocal() as db:
            row = v2._query(db, finalized["reservation_id"])
            assert row.state == "negative"

    def test_the_finalization_outcome_is_still_readable_after_cleanup(self, finalized):
        before = v2.get(finalized["reservation_id"])["admission"]

        after = v2.cleanup(
            finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4()))
        )

        assert after["admission"] == before
        assert after["admission"]["finalized_from_state"] == "launching"
        assert after["durable_state"] == "negative"

    def test_the_negative_gate_still_passes_on_retry_unwidened(self, finalized):
        """Why not overwriting the state also keeps a safety gate narrow.

        The cleanup precondition is ``state == "negative"``. Because the
        durable state never changes, that gate keeps passing on retry
        exactly as written -- a durable transition to ``cleaned`` would
        have forced it to accept a second state for no gain.
        """
        import inspect

        assert 'row.state != "negative"' in inspect.getsource(v2.cleanup)

        cleanup_id = str(uuid.uuid4())
        v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))
        v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))


class TestTheProjectionDerivesFromTheRecordNotAbsence:
    """The test that distinguishes a real fix from a convenient one."""

    def test_a_missing_terminal_row_alone_never_projects_cleaned(self, finalized):
        """An absent row is not proof a cleanup happened.

        It can be missing for having never existed, or for having been
        removed by something else. Projecting from it would report a
        cleanup nobody performed -- and would make the proof a consumer
        persists unfalsifiable.
        """
        database.delete_terminal_v2(finalized["terminal_id"])

        projected = v2.get(finalized["reservation_id"])

        assert projected["state"] == "negative"
        assert projected["cleanup"] is None

    def test_the_cleanup_key_is_an_explicit_null_before_any_cleanup(self, finalized):
        """Always-present, so "not cleaned" differs from "cannot say"."""
        projected = v2.get(finalized["reservation_id"])

        assert "cleanup" in projected
        assert projected["cleanup"] is None


class TestConcurrentCleanupResolvesOneWayOnly:
    """First-writer-wins, enforced at the write and not merely checked.

    Two callers both read no durable record before either commits. The
    write condition is ``cleanup_json IS NULL``, so exactly one commits;
    the loser rolls back its delete with it, leaving the winner's
    ``terminal_record_removed`` as the only account of what happened.
    """

    def test_two_concurrent_cleanups_produce_one_record(self, finalized, monkeypatch):
        cleanup_id = str(uuid.uuid4())
        other_id = str(uuid.uuid4())
        real = database.record_v2_cleanup_first_writer
        fired = []

        def _interleaved(reservation_id, **kwargs):
            # A rival cleanup commits in the window after this caller read
            # no record and before it writes one.
            if not fired:
                fired.append(True)
                real(
                    reservation_id,
                    build_record=lambda removed: v2._canonical_json(
                        {
                            "schema": "cao-managed-launch-v2-cleanup-v1",
                            "cleanup_id": other_id,
                            "terminal_id": finalized["terminal_id"],
                            "generation": finalized["generation"],
                            "terminal_record_removed": removed,
                            "cleaned_at": "2026-07-25T00:00:00Z",
                        }
                    ),
                    terminal_id=kwargs["terminal_id"],
                    generation=kwargs["generation"],
                )
            return real(reservation_id, **kwargs)

        monkeypatch.setattr(database, "record_v2_cleanup_first_writer", _interleaved)

        # The loser must not silently succeed under its own id.
        with pytest.raises(ManagedLaunchConflict, match="already cleaned"):
            v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))

        stored = v2.get(finalized["reservation_id"])["cleanup"]
        assert stored["cleanup_id"] == other_id
        assert stored["terminal_record_removed"] is True

    def test_the_losers_delete_is_rolled_back_with_its_write(self, finalized):
        """The delete and the record share one transaction, both ways.

        A loser that committed its delete while its record rolled back
        would remove a row the winner's proof says it removed -- or worse,
        remove one after a winner that recorded no removal, leaving the
        durable proof describing a world that did not happen.

        Set up so the rollback is the only thing that can save the row: a
        record exists (so this caller loses) while the terminal row is
        still present (so its delete has something to remove).
        """
        rival = v2._canonical_json(
            {
                "schema": "cao-managed-launch-v2-cleanup-v1",
                "cleanup_id": str(uuid.uuid4()),
                "terminal_id": finalized["terminal_id"],
                "generation": finalized["generation"],
                "terminal_record_removed": False,
                "cleaned_at": "2026-07-25T00:00:00Z",
            }
        )
        with database.SessionLocal() as db:
            db.query(database.ManagedLaunchV2ReservationModel).filter(
                database.ManagedLaunchV2ReservationModel.reservation_id
                == finalized["reservation_id"]
            ).update({"cleanup_json": rival}, synchronize_session=False)
            db.commit()

        won = database.record_v2_cleanup_first_writer(
            finalized["reservation_id"],
            build_record=lambda removed: v2._canonical_json({"never": "stored"}),
            terminal_id=finalized["terminal_id"],
            generation=finalized["generation"],
        )

        assert won is False
        # The row the loser's delete had already matched must still be here.
        assert database.get_terminal_metadata_v2(finalized["terminal_id"]) is not None
        assert v2.get(finalized["reservation_id"])["cleanup"]["terminal_record_removed"] is False


class TestResponseLoss:
    """The answer must be idempotent, not only the effect.

    The answer is what crosses the boundary: a consumer that persists a
    different proof on a retry has persisted a different fact.
    """

    def test_the_retry_returns_a_byte_identical_proof(self, finalized):
        """Compared field by field, not merely both succeeding."""
        cleanup_id = str(uuid.uuid4())

        issued = v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))[
            "cleanup"
        ]
        # The response is lost; the caller retries the identical request.
        replayed = v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))[
            "cleanup"
        ]

        assert replayed == issued
        assert replayed["cleaned_at"] == issued["cleaned_at"]
        assert replayed["terminal_record_removed"] == issued["terminal_record_removed"]

    def test_the_projection_is_stable_across_the_retry(self, finalized):
        cleanup_id = str(uuid.uuid4())

        first = v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))
        second = v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))

        assert first["state"] == second["state"] == CLEANED
        assert first["durable_state"] == second["durable_state"] == "negative"


class TestIdentityIsStillAsserted:
    """Unchanged by this repair, and pinned so it stays that way."""

    def test_a_foreign_generation_is_refused_before_anything_is_written(self, finalized):
        bad = ManagedLaunchV2CleanupRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            cleanup_id=str(uuid.uuid4()),
            terminal_id=finalized["terminal_id"],
            generation=str(uuid.uuid4()),
            obligation_generation=finalized["obligation_generation"],
        )

        with pytest.raises(ManagedLaunchConflict):
            v2.cleanup(finalized["reservation_id"], bad)

        assert v2.get(finalized["reservation_id"])["cleanup"] is None
        assert database.get_terminal_metadata_v2(finalized["terminal_id"]) is not None

    def test_a_foreign_terminal_id_is_refused(self, finalized):
        bad = ManagedLaunchV2CleanupRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            cleanup_id=str(uuid.uuid4()),
            terminal_id="ffffffff",
            generation=finalized["generation"],
            obligation_generation=finalized["obligation_generation"],
        )

        with pytest.raises(ManagedLaunchConflict):
            v2.cleanup(finalized["reservation_id"], bad)

        assert v2.get(finalized["reservation_id"])["cleanup"] is None


class TestTheSchemaAdditionIsForwardOnly:
    def test_the_column_is_declared_additive(self):
        """An old binary must ignore the new column, not fail on it."""
        from cli_agent_orchestrator.services import vintage_migration as vm

        assert ("cleanup_json", "TEXT") in vm._V2_RESERVATIONS_ADDITIVE_COLUMNS

    def test_a_row_without_the_column_reads_as_not_cleaned(self, finalized):
        """A database predating the column projects negative, never cleaned."""

        class _OldRow:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                if name == "cleanup_json":
                    raise AttributeError(name)
                return getattr(self._real, name)

        with database.SessionLocal() as db:
            row = v2._query(db, finalized["reservation_id"])
            projected = v2._row_dict(_OldRow(row))

        assert projected["state"] == "negative"
        assert projected["cleanup"] is None
