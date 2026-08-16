"""Stop is a hard barrier, and Resume is the only thing that reopens it.

These are the concrete cooperative failure sequences the barrier exists for:
a worker whose effect was in flight when Stop landed, an ordinary delivery
that arrives after it, an old generation still holding a terminal id, and a
retry that must not replay bytes into a fleet that has been collected.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

SESSION = "cao-stop-barrier"
DIGEST = "e" * 64


@pytest.fixture(autouse=True)
def _db(isolated_memory_db, monkeypatch, tmp_path):
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    return isolated_memory_db


def _bind(*, suffix: str, role: str = roster.ROLE_WORKER):
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=str(uuid.uuid4()),
            session_name=SESSION,
            role=role,
            profile_family="supervisor" if role == roster.ROLE_SUPERVISOR else "developer",
            harness="claude_code",
            native_session_id=f"native-{suffix}",
            acquisition_method="chosen_session_id",
            terminal_id=f"term-{suffix}",
            generation=str(uuid.uuid4()),
            pane_id=f"%{suffix}",
            pane_pid=8000 + int(suffix),
            process_identity={"pid": 8000 + int(suffix), "start_marker": f"m-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _boundary():
    boundary = cohort.observe_boundary(SESSION)
    return {
        "lifecycle_epoch": boundary["lifecycle_epoch"],
        "lifecycle_observation": boundary["lifecycle_observation"],
        "roster_revision": boundary["roster_revision"],
        "member_snapshot_digest": boundary["member_snapshot_digest"],
    }


def _stop_to_terminal():
    operation = cohort.claim_operation(
        cohort.OperationRequest(
            operation_id=str(uuid.uuid4()),
            session_name=SESSION,
            operation_kind=cohort.KIND_STOP,
            requested_mode=cohort.MODE_FORCE,
            initiator_kind=cohort.INITIATOR_OPERATOR,
            initiated_by="colin",
            **_boundary(),
        )
    )
    teardown = cohort.begin_stop_teardown(
        cohort.StopTeardownRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=0,
            actor="colin",
        )
    )["operation"]
    for member in cohort.get_operation(operation["operation_id"])["members"]:
        if member["included"]:
            cohort.record_member_result(
                cohort.MemberResult(
                    operation_id=operation["operation_id"],
                    agent_id=member["agent_id"],
                    expected_result_revision=0,
                    final_state=cohort.FINAL_STOPPED,
                    background_command_loss_risk=cohort.LOSS_NONE,
                )
            )
    cohort.commit_terminal(
        cohort.TerminalCommitRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=int(teardown["state_epoch"]),
            actor="colin",
            receipt_digest=DIGEST,
        )
    )
    return operation


# ---------------------------------------------------------------------------
# no post-barrier effect
# ---------------------------------------------------------------------------


def test_every_effect_after_the_barrier_is_refused():
    _bind(suffix="1")
    _stop_to_terminal()

    with pytest.raises(cohort.SessionEffectRefused, match="Stop barrier is claimed"):
        with cohort.session_effect_admission(SESSION):
            raise AssertionError("no effect may run inside a stopped session")


def test_a_legacy_stop_with_no_cohort_barrier_still_refuses_every_effect():
    """The lifecycle row is the second gate, and it stands on its own.

    A session stopped through the pre-cohort route has no barrier row at all.
    Refusing on the barrier alone would admit effects into exactly the fleet
    whose panes that older path collected.
    """
    _bind(suffix="1")
    sl.declare(SESSION, sl.WORKING, declared_by="colin")
    sl.stop(SESSION, declared_by="colin")
    assert oj.get_session_barrier(SESSION) is None

    with pytest.raises(cohort.SessionEffectRefused, match="is stopped"):
        with cohort.session_effect_admission(SESSION):
            raise AssertionError("no effect may run inside a stopped session")


def test_an_effect_is_refused_the_moment_the_barrier_is_claimed(monkeypatch):
    """Refused on the barrier alone, before the lifecycle row is even written.

    This is the window a Stop actually spends tearing down, and it is exactly
    when a racing inbox delivery or deadman would otherwise land bytes in a
    pane that is about to be reaped.
    """
    _bind(suffix="1")
    oj.claim_session_barrier(SESSION, claimed_by=str(uuid.uuid4()), reason="Stop in progress")
    assert sl.describe(SESSION)["lifecycle"] == sl.WORKING

    with pytest.raises(cohort.SessionEffectRefused, match="Stop barrier is claimed"):
        with cohort.session_effect_admission(SESSION):
            raise AssertionError("no effect may run after the barrier is claimed")


def test_an_effect_already_inside_the_barrier_finishes_before_stop_claims():
    """The in-flight effect is admitted; the one behind it is not.

    Effects take the lifecycle claim *shared* so they do not queue behind each
    other, while Stop takes it exclusively — so Stop waits out whatever was
    already writing, and everything arriving later is refused.
    """
    _bind(suffix="1")
    inside = threading.Event()
    release = threading.Event()
    order = []

    def _effect():
        with cohort.session_effect_admission(SESSION):
            order.append("effect-enter")
            inside.set()
            release.wait(timeout=5)
            order.append("effect-exit")

    worker = threading.Thread(target=_effect)
    worker.start()
    assert inside.wait(timeout=5)

    def _stop():
        oj.claim_session_barrier(SESSION, claimed_by=str(uuid.uuid4()), reason="Stop")
        order.append("barrier")

    stopper = threading.Thread(target=_stop)
    stopper.start()
    release.set()
    worker.join(timeout=5)
    stopper.join(timeout=5)

    assert order == ["effect-enter", "effect-exit", "barrier"]
    with pytest.raises(cohort.SessionEffectRefused):
        with cohort.session_effect_admission(SESSION):
            pass


def test_an_unreadable_lifecycle_refuses_the_effect_rather_than_guessing(monkeypatch):
    _bind(suffix="1")
    monkeypatch.setattr(
        sl, "describe", lambda _name: {"lifecycle": sl.WORKING, "unreadable": "boom"}
    )

    with pytest.raises(cohort.SessionEffectRefused, match="unreadable"):
        with cohort.session_effect_admission(SESSION):
            pass


# ---------------------------------------------------------------------------
# no replay
# ---------------------------------------------------------------------------


def test_a_replayed_terminal_commit_adopts_and_does_not_re_stop():
    _bind(suffix="1")
    operation = _stop_to_terminal()
    record = cohort.get_operation(operation["operation_id"])
    commit = next(
        transition
        for transition in record["transitions"]
        if transition["to_state"] == cohort.STATE_STOPPED
    )
    epoch_before = sl.describe(SESSION)["epoch"]

    adopted = cohort.commit_terminal(
        cohort.TerminalCommitRequest(
            transition_id=commit["transition_id"],
            operation_id=operation["operation_id"],
            expected_state_epoch=commit["from_state_epoch"],
            actor="colin",
            receipt_digest=DIGEST,
        )
    )

    assert adopted["adopted"] is True
    # The lifecycle was not written a second time.
    assert sl.describe(SESSION)["epoch"] == epoch_before


def test_a_second_stop_cannot_claim_the_same_boundary():
    _bind(suffix="1")
    boundary = _boundary()
    first = cohort.claim_operation(
        cohort.OperationRequest(
            operation_id=str(uuid.uuid4()),
            session_name=SESSION,
            operation_kind=cohort.KIND_STOP,
            requested_mode=cohort.MODE_FORCE,
            initiator_kind=cohort.INITIATOR_OPERATOR,
            initiated_by="colin",
            **boundary,
        )
    )

    with pytest.raises(cohort.CohortJournalConflict, match="already claimed by winning operation"):
        cohort.claim_operation(
            cohort.OperationRequest(
                operation_id=str(uuid.uuid4()),
                session_name=SESSION,
                operation_kind=cohort.KIND_STOP,
                requested_mode=cohort.MODE_FORCE,
                initiator_kind=cohort.INITIATOR_OPERATOR,
                initiated_by="someone-else",
                **boundary,
            )
        )
    assert first["state"] == cohort.STATE_PREPARING


def test_concurrent_stop_teardowns_produce_exactly_one_barrier_owner(tmp_path, monkeypatch):
    """Two good-faith actors, one winner, and the loser learns the truth."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients import database

    engine = create_engine(f"sqlite:///{tmp_path / 'barrier.db'}")
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))

    _bind(suffix="1")
    operation = cohort.claim_operation(
        cohort.OperationRequest(
            operation_id=str(uuid.uuid4()),
            session_name=SESSION,
            operation_kind=cohort.KIND_STOP,
            requested_mode=cohort.MODE_FORCE,
            initiator_kind=cohort.INITIATOR_OPERATOR,
            initiated_by="colin",
            **_boundary(),
        )
    )
    results: list[object] = []
    barrier = threading.Barrier(2)

    def _teardown():
        barrier.wait(timeout=5)
        try:
            results.append(
                cohort.begin_stop_teardown(
                    cohort.StopTeardownRequest(
                        transition_id=str(uuid.uuid4()),
                        operation_id=operation["operation_id"],
                        expected_state_epoch=0,
                        actor="colin",
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 - the loser's refusal is the point
            results.append(exc)

    threads = [threading.Thread(target=_teardown) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    wins = [item for item in results if isinstance(item, dict)]
    losses = [item for item in results if isinstance(item, Exception)]
    assert len(wins) == 1, results
    assert len(losses) == 1, results
    assert isinstance(losses[0], cohort.CohortJournalError)
    assert oj.get_session_barrier(SESSION)["claimed_by"] == operation["operation_id"]
    engine.dispose()


# ---------------------------------------------------------------------------
# only an operator Resume reopens the barrier
# ---------------------------------------------------------------------------


def test_the_barrier_never_reopens_on_its_own():
    _bind(suffix="1")
    _stop_to_terminal()

    assert oj.get_session_barrier(SESSION)["state"] == oj.BARRIER_CLAIMED
    # Reading the journal, describing the session, or listing operations must
    # never be a way to clear it.
    cohort.get_operation(_stop_id())
    sl.describe(SESSION)
    cohort.list_operations(SESSION)
    assert oj.get_session_barrier(SESSION)["state"] == oj.BARRIER_CLAIMED


def _stop_id():
    return next(
        operation["operation_id"]
        for operation in cohort.list_operations(SESSION)
        if operation["operation_kind"] == cohort.KIND_STOP
    )


def test_a_supervisor_initiated_resume_is_refused():
    """Only an operator resumes a stopped campaign."""
    with pytest.raises(cohort.CohortJournalInvalid, match="only an operator"):
        cohort.OperationRequest(
            operation_id=str(uuid.uuid4()),
            session_name=SESSION,
            operation_kind=cohort.KIND_RESUME,
            requested_mode=cohort.MODE_SAFE,
            initiator_kind=cohort.INITIATOR_SUPERVISOR,
            initiated_by="supervisor",
            lifecycle_epoch=1,
            lifecycle_observation=sl.STOPPED,
            roster_revision="a" * 64,
            member_snapshot_digest="b" * 64,
            source_operation_id=str(uuid.uuid4()),
            resume_target=sl.WORKING,
        )


def test_resume_has_no_force_mode():
    with pytest.raises(cohort.CohortJournalInvalid, match="no force mode"):
        cohort.OperationRequest(
            operation_id=str(uuid.uuid4()),
            session_name=SESSION,
            operation_kind=cohort.KIND_RESUME,
            requested_mode=cohort.MODE_FORCE,
            initiator_kind=cohort.INITIATOR_OPERATOR,
            initiated_by="colin",
            lifecycle_epoch=1,
            lifecycle_observation=sl.STOPPED,
            roster_revision="a" * 64,
            member_snapshot_digest="b" * 64,
            source_operation_id=str(uuid.uuid4()),
            resume_target=sl.WORKING,
        )
