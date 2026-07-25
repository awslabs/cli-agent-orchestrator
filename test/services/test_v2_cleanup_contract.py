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

STATUS: these encode the *reproduction*, not yet the agreed semantics. The
retained spec writer owes a short amendment naming the terminal state and
where the proof is durably kept; the assertions marked below are the ones
that must be reconciled with it before production changes.
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

#: The state a cleaned generation is expected to reach. v1 uses exactly
#: this string, and a consumer requiring it is why this issue was opened.
#: Named once so the amendment can move it in one place if it chooses a
#: different spelling.
CLEANED = "cleaned"


@pytest.fixture
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")


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

    def test_removing_an_already_absent_row_is_still_a_success(self, finalized):
        """Idempotent by *effect*, which is the one kind it does have."""
        v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4())))
        again = v2.cleanup(
            finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4()))
        )

        assert again["cleanup"]["terminal_record_removed"] is False

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

    def test_a_second_distinct_cleanup_id_does_not_mint_a_rival_proof(self, finalized):
        """Exactly one cleanup is authoritative for one generation.

        Accepting a second id produces two equally valid-looking proofs for
        the same generation, with no rule saying which one happened.

        NOTE: whether a rival id should be REFUSED or should converge on
        the stored proof is a semantic choice for the amendment. This
        asserts only that it must not mint an independent second proof --
        the part both options agree on.
        """
        first = v2.cleanup(
            finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4()))
        )["cleanup"]

        second = v2.cleanup(
            finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4()))
        )["cleanup"]

        assert second["cleanup_id"] == first["cleanup_id"]
