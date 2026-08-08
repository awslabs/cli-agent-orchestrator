"""What a session is doing, declared rather than inferred.

The negatives carry this file. A session lifecycle exists to let the fire
marshal stay silent about sessions somebody deliberately quieted, and every
way of getting that wrong is silent: suppress too much and a genuinely
deadlocked campaign is never investigated; default undeclared sessions to
quiet and the marshal goes dark across the whole existing fleet the day it
ships.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import session_lifecycle as sl

SESSION = "cao-p1-closure"


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    return isolated_memory_db


class TestAbsenceMeansWorking:
    """Every session that has ever run predates this table.

    Defaulting an undeclared session to quiet would suppress the marshal
    across the entire existing fleet on the day this shipped.
    """

    def test_an_undeclared_session_is_working(self):
        record = sl.describe(SESSION)
        assert record["lifecycle"] == sl.WORKING
        assert record["declared"] is False

    def test_an_undeclared_session_does_not_suppress(self):
        suppressed, _ = sl.suppresses_marshal(SESSION)
        assert suppressed is False

    def test_an_unreadable_store_does_not_suppress(self, monkeypatch):
        """Silence is the failure nobody notices.

        The marshal is report-only, so a false alarm costs one
        investigation while a missed deadlock costs days. This is one
        `except` clause away from being wrong in the direction that
        produces silence.
        """

        def _broken():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(sl.database, "SessionLocal", _broken)
        suppressed, record = sl.suppresses_marshal(SESSION)
        assert suppressed is False
        assert record["unreadable"]
        assert record["lifecycle"] == sl.WORKING

    def test_an_absent_table_reads_as_working_rather_than_raising(self, tmp_path, monkeypatch):
        engine = create_engine(f"sqlite:///{tmp_path}/empty.db")
        monkeypatch.setattr(sl.database, "SessionLocal", sessionmaker(bind=engine))
        assert sl.describe(SESSION)["lifecycle"] == sl.WORKING
        assert sl.list_sessions() == []


class TestSuppression:
    @pytest.mark.parametrize("lifecycle", [sl.COMPLETE, sl.PAUSED])
    def test_a_declared_quiet_session_suppresses(self, lifecycle):
        sl.declare(SESSION, lifecycle, declared_by="supervisor")
        suppressed, _ = sl.suppresses_marshal(SESSION)
        assert suppressed is True

    def test_a_stopped_session_suppresses(self):
        sl.stop(SESSION, declared_by="colin")
        assert sl.suppresses_marshal(SESSION)[0] is True

    def test_pausing_does_not_suppress(self):
        """The load-bearing negative of the whole design.

        A supervisor that never settles a pause is exactly the
        unresponsive-supervisor case the marshal exists for. If `pausing`
        suppressed, a session could pause, its supervisor die mid-settle,
        and the marshal be silenced forever on a genuinely deadlocked
        campaign.
        """
        sl.request_pause(SESSION, requested_by="colin")
        suppressed, record = sl.suppresses_marshal(SESSION)
        assert record["lifecycle"] == sl.PAUSING
        assert suppressed is False

    def test_working_does_not_suppress(self):
        sl.declare(SESSION, sl.WORKING, declared_by="supervisor")
        assert sl.suppresses_marshal(SESSION)[0] is False

    def test_the_suppressing_set_is_exactly_three_states(self):
        """A pin, because adding `pausing` here is the tempting mistake."""
        assert sl.MARSHAL_SUPPRESSING == frozenset({sl.PAUSED, sl.COMPLETE, sl.STOPPED})


class TestThePauseProtocol:
    def test_a_request_does_not_grant_a_pause(self):
        record = sl.request_pause(SESSION, requested_by="colin")
        assert record["lifecycle"] == sl.PAUSING
        assert record["pause_deadline_at"]

    def test_only_the_supervisor_settles_it(self):
        sl.request_pause(SESSION, requested_by="colin")
        record = sl.settle_pause(SESSION, declared_by="supervisor")
        assert record["lifecycle"] == sl.PAUSED
        assert record["pause_deadline_at"] is None

    def test_settling_a_pause_nobody_asked_for_is_refused(self):
        sl.declare(SESSION, sl.WORKING, declared_by="supervisor")
        with pytest.raises(sl.SessionLifecycleConflict, match="only be settled after"):
            sl.settle_pause(SESSION, declared_by="supervisor")

    def test_a_complete_session_has_no_fleet_to_settle(self):
        sl.declare(SESSION, sl.COMPLETE, declared_by="supervisor")
        with pytest.raises(sl.SessionLifecycleConflict, match="no running fleet"):
            sl.request_pause(SESSION, requested_by="colin")

    def test_the_deadline_is_derived_at_read_time_not_scheduled(self):
        """Nothing in this system schedules anything.

        A stored deadline that is never derived would leave `pausing`
        indistinguishable from a fresh request forever. The predicate is
        pure so the marshal and the UI can ask the same question without
        either of them writing.
        """
        record = sl.request_pause(SESSION, requested_by="colin", deadline_seconds=60)
        assert sl.pause_is_overdue(record) is False
        later = datetime.now(timezone.utc) + timedelta(seconds=120)
        assert sl.pause_is_overdue(record, now=later) is True

    def test_only_a_pausing_session_can_be_overdue(self):
        record = sl.declare(SESSION, sl.WORKING, declared_by="s")
        far_future = datetime.now(timezone.utc) + timedelta(days=365)
        assert sl.pause_is_overdue(record, now=far_future) is False

    def test_an_overdue_pause_still_does_not_suppress(self):
        """Expiry changes nothing about suppression — there was none to withdraw."""
        sl.request_pause(SESSION, requested_by="colin", deadline_seconds=1)
        assert sl.suppresses_marshal(SESSION)[0] is False


class TestStopRecordsWhatAResumeWouldNeed:
    """No provider can be resumed today. The fact is recorded anyway.

    Stop is the only moment the pre-stop state is known, and discarding it
    would mean a session stopped this week cannot be brought back
    correctly once resume lands. One column against a permanent loss.
    """

    def test_stopping_a_working_session_restores_to_working(self):
        assert sl.stop(SESSION, declared_by="colin")["restore_to"] == sl.WORKING

    def test_stopping_a_complete_session_remembers_it_was_complete(self):
        sl.declare(SESSION, sl.COMPLETE, declared_by="supervisor")
        assert sl.stop(SESSION, declared_by="colin")["restore_to"] == sl.COMPLETE

    def test_stopping_a_paused_session_remembers_it_was_paused(self):
        sl.request_pause(SESSION, requested_by="colin")
        sl.settle_pause(SESSION, declared_by="supervisor")
        assert sl.stop(SESSION, declared_by="colin")["restore_to"] == sl.PAUSED

    def test_stopping_mid_settle_restores_to_working_not_pausing(self):
        """Resuming into `pausing` would restart a deadline nobody awaits."""
        sl.request_pause(SESSION, requested_by="colin")
        record = sl.stop(SESSION, declared_by="colin")
        assert record["restore_to"] == sl.WORKING
        assert record["pause_deadline_at"] is None

    def test_pausing_is_not_a_restore_target(self):
        assert sl.PAUSING not in sl.RESTORE_TARGETS

    def test_restopping_keeps_the_original_restore_target(self):
        sl.declare(SESSION, sl.COMPLETE, declared_by="supervisor")
        sl.stop(SESSION, declared_by="colin")
        assert sl.stop(SESSION, declared_by="colin")["restore_to"] == sl.COMPLETE

    def test_stopped_cannot_be_reached_by_a_bare_declaration(self):
        """It must capture what a resume would restore to."""
        with pytest.raises(sl.SessionLifecycleInvalid, match="use stop"):
            sl.declare(SESSION, sl.STOPPED, declared_by="colin")


class TestArchiving:
    def test_archiving_a_complete_session_does_not_lose_that_it_was_complete(self):
        """Why archived is a flag and not a fifth state."""
        sl.declare(SESSION, sl.COMPLETE, declared_by="supervisor")
        record = sl.set_archived(SESSION, True, declared_by="colin")
        assert record["archived"] is True
        assert record["lifecycle"] == sl.STOPPED
        assert record["restore_to"] == sl.COMPLETE

    def test_archived_sessions_are_hidden_by_default(self):
        sl.set_archived(SESSION, True, declared_by="colin")
        assert sl.list_sessions() == []
        assert len(sl.list_sessions(include_archived=True)) == 1

    def test_an_archived_session_still_suppresses(self):
        sl.set_archived(SESSION, True, declared_by="colin")
        assert sl.suppresses_marshal(SESSION)[0] is True


class TestKind:
    def test_a_service_session_is_declared_not_guessed(self):
        record = sl.set_kind(SESSION, sl.SERVICE, declared_by="colin")
        assert record["kind"] == sl.SERVICE

    def test_the_default_is_campaign(self):
        assert sl.describe(SESSION)["kind"] == sl.CAMPAIGN

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(sl.SessionLifecycleInvalid):
            sl.set_kind(SESSION, "daemon", declared_by="colin")


class TestConcurrency:
    def test_a_stale_epoch_is_refused(self):
        first = sl.declare(SESSION, sl.WORKING, declared_by="a")
        sl.declare(SESSION, sl.COMPLETE, declared_by="b")
        with pytest.raises(sl.SessionLifecycleConflict, match="moved to epoch"):
            sl.declare(SESSION, sl.WORKING, declared_by="c", expected_epoch=first["epoch"])

    def test_the_current_epoch_is_accepted(self):
        record = sl.declare(SESSION, sl.WORKING, declared_by="a")
        sl.declare(SESSION, sl.COMPLETE, declared_by="b", expected_epoch=record["epoch"])
        assert sl.describe(SESSION)["lifecycle"] == sl.COMPLETE

    def test_every_transition_bumps_the_epoch(self):
        before = sl.declare(SESSION, sl.WORKING, declared_by="a")["epoch"]
        after = sl.declare(SESSION, sl.COMPLETE, declared_by="a")["epoch"]
        assert after == before + 1

    def test_claiming_an_epoch_on_an_undeclared_session_is_refused(self):
        with pytest.raises(sl.SessionLifecycleConflict, match="no declared state"):
            sl.declare(SESSION, sl.COMPLETE, declared_by="a", expected_epoch=4)


class TestNameReuse:
    def test_forgetting_a_session_stops_a_later_one_inheriting_it(self):
        """A reserved, reused name is the doctor session's whole shape."""
        sl.stop(SESSION, declared_by="colin")
        assert sl.forget(SESSION) is True
        record = sl.describe(SESSION)
        assert record["lifecycle"] == sl.WORKING
        assert record["declared"] is False

    def test_forgetting_an_undeclared_session_is_not_an_error(self):
        assert sl.forget(SESSION) is False


class TestWritesNeverDegrade:
    def test_a_failed_write_raises_rather_than_reporting_success(self, monkeypatch):
        """Reads degrade to `working`; writes do not degrade at all.

        Silently failing to record a stop leaves an operator believing a
        fleet was collected when it is still running.
        """

        def _broken():
            raise RuntimeError("disk full")

        monkeypatch.setattr(sl.database, "SessionLocal", _broken)
        with pytest.raises(sl.SessionLifecycleUnavailable):
            sl.declare(SESSION, sl.COMPLETE, declared_by="colin")

    def test_every_transition_records_who_made_it(self):
        record = sl.declare(SESSION, sl.COMPLETE, declared_by="supervisor-terra")
        assert record["declared_by"] == "supervisor-terra"

    @pytest.mark.parametrize("declared_by", ["", None])
    def test_an_unattributed_transition_is_refused(self, declared_by):
        with pytest.raises(sl.SessionLifecycleInvalid):
            sl.declare(SESSION, sl.COMPLETE, declared_by=declared_by)


class TestTheDefencesAgainstSilentLoss:
    """Two failures that are invisible at the moment they happen.

    Both were found by research rather than by anything going wrong, and
    both surface long after the cause, as something that looks unrelated.
    """

    def _capture_retention(self, monkeypatch) -> dict:
        """Run the boot reconcile against a tmux that says nothing exists."""
        from cli_agent_orchestrator.services import session_env, terminal_service

        seen: dict[str, bool] = {}

        def _fake_reconcile(predicate):
            seen["stopped"] = predicate(SESSION)
            seen["ordinary"] = predicate("cao-never-existed")
            return {"removed": [], "failed": []}

        class _Backend:
            @staticmethod
            def session_exists(_name: str) -> bool:
                return False

        monkeypatch.setattr(terminal_service, "get_backend", lambda: _Backend())
        monkeypatch.setattr(session_env, "reconcile_session_env", _fake_reconcile)
        terminal_service.reconcile_session_env()
        return seen

    def test_a_stopped_sessions_env_survives_the_boot_reconcile(self, monkeypatch):
        """Otherwise: "resume worked but every worker is on the wrong binary".

        `reconcile_session_env` drops rows whose tmux session is gone — and
        a stopped session's tmux session is gone *by definition*, so the
        bare existence probe deletes the forwarded env of every hibernated
        session at the next boot.
        """
        sl.stop(SESSION, declared_by="colin")
        seen = self._capture_retention(monkeypatch)
        assert seen["stopped"] is True, "a stopped session's env was deleted"
        assert seen["ordinary"] is False, "an ordinary vanished session was wrongly retained"

    def test_a_working_sessions_env_is_still_collected(self, monkeypatch):
        """The exemption must not become a blanket retention."""
        sl.declare(SESSION, sl.WORKING, declared_by="supervisor")
        assert self._capture_retention(monkeypatch)["stopped"] is False

    def test_an_unreadable_store_retains_rather_than_deletes(self, monkeypatch):
        """Losing env is irreversible; keeping a stale row is not."""

        def _broken():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(sl.database, "SessionLocal", _broken)
        assert self._capture_retention(monkeypatch)["stopped"] is True

    @pytest.mark.asyncio
    async def test_a_stopped_name_cannot_be_silently_reused(self):
        """A reserved, reused name is the repair session's whole shape.

        Taking a stopped name inherits what that session would restore to,
        and in time what to relaunch — so a later resume would bring back
        the wrong workers against the wrong provider sessions.
        """
        from cli_agent_orchestrator.services import session_service

        sl.stop(SESSION, declared_by="colin")
        with pytest.raises(ValueError, match="is stopped"):
            await session_service.create_session(
                provider="mock_cli", agent_profile="developer", session_name=SESSION
            )

    @pytest.mark.asyncio
    async def test_forgetting_the_name_releases_it(self):
        from cli_agent_orchestrator.services import session_service

        sl.stop(SESSION, declared_by="colin")
        sl.forget(SESSION)
        # No longer refused by the lifecycle guard; it fails later, on the
        # real launch path, which is a different and expected error.
        with pytest.raises(Exception) as excinfo:
            await session_service.create_session(
                provider="mock_cli", agent_profile="developer", session_name=SESSION
            )
        assert "is stopped" not in str(excinfo.value)


class TestTheKeyEveryEntryPointAgreesOn:
    """A session created as `repair` runs as `cao-repair`.

    The launch path prepends the prefix after the caller hands over a bare
    name, and every in-repo caller passes it unnormalised. Keying declared
    state on whatever was typed splits one session across two rows — and
    both defences in this subsystem then read the wrong one.
    """

    def test_a_bare_name_and_a_prefixed_name_are_one_session(self):
        sl.stop("p1-closure", declared_by="colin")
        assert sl.describe("cao-p1-closure")["lifecycle"] == sl.STOPPED
        assert len(sl.list_sessions()) == 1

    def test_a_bare_name_stop_is_seen_by_the_env_retention(self, monkeypatch):
        """Otherwise the hibernated session's env is collected anyway."""
        from cli_agent_orchestrator.services import session_env, terminal_service

        sl.stop("p1-closure", declared_by="colin")
        seen: dict[str, bool] = {}

        def _fake(predicate):
            seen["retained"] = predicate("cao-p1-closure")
            return {"removed": [], "failed": []}

        class _Backend:
            @staticmethod
            def session_exists(_n: str) -> bool:
                return False

        monkeypatch.setattr(terminal_service, "get_backend", lambda: _Backend())
        monkeypatch.setattr(session_env, "reconcile_session_env", _fake)
        terminal_service.reconcile_session_env()
        assert seen["retained"] is True

    def test_a_bare_name_stop_blocks_a_prefixed_recreate(self):
        sl.stop("p1-closure", declared_by="colin")
        assert sl.describe(SESSION)["lifecycle"] == sl.STOPPED


class TestADeletedSessionLeavesNoDeclaration:
    def test_deleting_forgets_the_declaration(self, monkeypatch):
        """A reused name must not inherit a stranger's suppressor."""
        from cli_agent_orchestrator.services import session_service

        sl.declare(SESSION, sl.COMPLETE, declared_by="supervisor")

        class _Backend:
            @staticmethod
            def session_exists(_n: str) -> bool:
                return False

            @staticmethod
            def kill_session(_n: str) -> None:
                return None

        monkeypatch.setattr(session_service, "get_backend", lambda: _Backend())
        monkeypatch.setattr(session_service, "list_terminals_by_session", lambda _n: [])
        monkeypatch.setattr(session_service, "clear_session_env", lambda _n: None)
        try:
            session_service.delete_session(SESSION)
        except Exception:
            pass  # the teardown path has other collaborators; the forget is what matters
        record = sl.describe(SESSION)
        assert record["declared"] is False
        assert sl.suppresses_marshal(SESSION)[0] is False


class TestTheDeadlineCannotBeDefeated:
    def test_a_repeated_request_keeps_the_original_deadline(self):
        """Otherwise the bound is defeatable by pressing the button again."""
        first = sl.request_pause(SESSION, requested_by="colin", deadline_seconds=60)
        again = sl.request_pause(SESSION, requested_by="colin", deadline_seconds=86400)
        assert again["pause_deadline_at"] == first["pause_deadline_at"]

    @pytest.mark.parametrize("deadline", [None, "", "not-a-timestamp"])
    def test_an_unreadable_deadline_reads_as_overdue(self, deadline):
        """The two ways to be wrong are not symmetric.

        Reporting not-overdue keeps the wedge flag muted forever, which is
        exactly what the deadline exists to bound.
        """
        assert sl.pause_is_overdue({"lifecycle": sl.PAUSING, "pause_deadline_at": deadline}) is True

    def test_a_settled_pause_clears_the_deadline(self):
        sl.request_pause(SESSION, requested_by="colin")
        settled = sl.settle_pause(SESSION, declared_by="supervisor")
        assert settled["pause_deadline_at"] is None
        assert sl.pause_is_overdue(settled) is False


class TestAContradictedDeclarationIsVisiblyWrong:
    """`stop` records; it does not collect panes.

    So an operator can declare a session stopped while its fleet keeps
    running — marshal-suppressed and still burning quota. The declaration
    is trusted until changed, deliberately, and nothing here auto-corrects
    it. But trusted is not the same as silent: every surface that renders
    the state can say the two disagree.
    """

    def test_a_stopped_session_with_live_panes_is_flagged(self, monkeypatch):
        from cli_agent_orchestrator.services import terminal_projection

        monkeypatch.setattr(
            terminal_projection, "live_terminals", lambda name: [{"terminal_id": "a1"}]
        )
        record = sl.stop(SESSION, declared_by="colin")
        message = sl.divergence(record)
        assert message and "1 terminal(s) are still live" in message
        assert "fire marshal" in message

    def test_a_stopped_session_with_no_panes_is_not_flagged(self, monkeypatch):
        from cli_agent_orchestrator.services import terminal_projection

        monkeypatch.setattr(terminal_projection, "live_terminals", lambda name: [])
        assert sl.divergence(sl.stop(SESSION, declared_by="colin")) is None

    def test_a_working_session_with_live_panes_is_not_a_contradiction(self, monkeypatch):
        from cli_agent_orchestrator.services import terminal_projection

        monkeypatch.setattr(
            terminal_projection, "live_terminals", lambda name: [{"terminal_id": "a1"}]
        )
        assert sl.divergence(sl.declare(SESSION, sl.WORKING, declared_by="s")) is None

    def test_an_unreadable_fleet_is_not_reported_as_a_contradiction(self, monkeypatch):
        """We could not look is not evidence that the two disagree."""
        from cli_agent_orchestrator.services import terminal_projection

        def _boom(_name):
            raise RuntimeError("tmux unreachable")

        monkeypatch.setattr(terminal_projection, "live_terminals", _boom)
        assert sl.divergence(sl.stop(SESSION, declared_by="colin")) is None


class TestThePinsTheReviewersFoundMissing:
    def test_the_wedge_gate_respects_the_pause_deadline(self, monkeypatch):
        """Reverting the deadline check passed 141 tests before this."""
        from cli_agent_orchestrator.services import terminal_projection

        fresh = sl.request_pause(SESSION, requested_by="colin", deadline_seconds=3600)
        assert terminal_projection._is_pause_like(fresh) is True

        overdue = dict(fresh)
        overdue["pause_deadline_at"] = "2020-01-01T00:00:00Z"
        assert terminal_projection._is_pause_like(overdue) is False

    def test_a_paused_session_is_pause_like_without_any_deadline(self):
        from cli_agent_orchestrator.services import terminal_projection

        sl.request_pause(SESSION, requested_by="colin")
        settled = sl.settle_pause(SESSION, declared_by="supervisor")
        assert terminal_projection._is_pause_like(settled) is True


class TestAStoppedSessionCannotBeRevivedByADeclaration:
    """A stopped session's panes are collected; declaring it live is incoherent.

    This is the typed-confusal half of cond-0221's P1 fix: a declare that
    races (or follows) a stop cannot overwrite ``stopped`` with a live state,
    because that would leave a collected fleet declared working. Reviving a
    stopped session is resume's job (it relaunches the panes first), not a
    bare declaration's. ``expected_epoch`` semantics are untouched: a conflict
    stays a conflict, never blind last-write-wins.
    """

    def test_declare_working_on_a_stopped_session_is_refused(self):
        sl.stop(SESSION, declared_by="colin")
        with pytest.raises(sl.SessionLifecycleConflict, match="stopped"):
            sl.declare(SESSION, sl.WORKING, declared_by="racer")
        assert sl.describe(SESSION)["lifecycle"] == sl.STOPPED

    def test_declare_complete_on_a_stopped_session_is_refused(self):
        sl.stop(SESSION, declared_by="colin")
        with pytest.raises(sl.SessionLifecycleConflict, match="stopped"):
            sl.declare(SESSION, sl.COMPLETE, declared_by="racer")
        assert sl.describe(SESSION)["lifecycle"] == sl.STOPPED

    def test_a_stopped_session_can_still_be_restopped_or_archived(self):
        """Reviving is refused; the idempotent/visibility operations are not."""
        sl.declare(SESSION, sl.COMPLETE, declared_by="supervisor")
        sl.stop(SESSION, declared_by="colin")
        restopped = sl.stop(SESSION, declared_by="colin")
        assert restopped["lifecycle"] == sl.STOPPED
        assert restopped["restore_to"] == sl.COMPLETE
        archived = sl.set_archived(SESSION, True, declared_by="colin")
        assert archived["archived"] is True

    def test_a_working_session_can_still_be_declared_complete(self):
        """The refusal is scoped to a stopped source, not a new rule on declare."""
        sl.declare(SESSION, sl.WORKING, declared_by="supervisor")
        record = sl.declare(SESSION, sl.COMPLETE, declared_by="supervisor")
        assert record["lifecycle"] == sl.COMPLETE


class TestLifecycleWritesAreSerialized:
    """Every lifecycle mutation shares one cross-process critical section.

    cond-0221 P1: a lifecycle write must not commit during a stop's
    declaration/collection critical section. The write claim is a file lock
    keyed by session name alone (the lifecycle module stays backend-free), it
    sorts after the physical session-workspace claim and before any
    terminal-generation claim, and ``_write``/``forget`` both take it.
    """

    def test_the_write_claim_excludes_a_concurrent_writer(self):
        """Two writers for one session cannot both hold the critical section."""
        import threading

        from cli_agent_orchestrator.services import callback_recovery

        holder_in = threading.Event()
        release_holder = threading.Event()
        contender_entered = threading.Event()

        def _holder():
            with callback_recovery.session_lifecycle_write_claim(SESSION):
                holder_in.set()
                release_holder.wait(timeout=10)

        def _contender():
            holder_in.wait(timeout=10)
            with callback_recovery.session_lifecycle_write_claim(SESSION):
                contender_entered.set()

        holder = threading.Thread(target=_holder)
        holder.start()
        assert holder_in.wait(timeout=10)

        contender = threading.Thread(target=_contender)
        contender.start()
        contender.join(timeout=2)
        # The claim is still held by `holder`, so the contender must be blocked.
        assert contender.is_alive(), "the write claim let a second writer in"
        assert not contender_entered.is_set()

        release_holder.set()
        holder.join(timeout=10)
        contender.join(timeout=10)
        assert contender_entered.is_set(), "the contender never entered after release"
        assert not holder.is_alive() and not contender.is_alive()

    def test_writes_for_different_sessions_do_not_block_each_other(self):
        """The claim is per session, not global — unrelated sessions are independent."""
        import threading

        from cli_agent_orchestrator.services import callback_recovery

        held = threading.Event()
        release = threading.Event()
        other_done = threading.Event()

        def _hold():
            with callback_recovery.session_lifecycle_write_claim(SESSION):
                held.set()
                release.wait(timeout=10)

        def _other():
            held.wait(timeout=10)
            with callback_recovery.session_lifecycle_write_claim("cao-unrelated"):
                other_done.set()

        a = threading.Thread(target=_hold)
        a.start()
        held.wait(timeout=10)
        b = threading.Thread(target=_other)
        b.start()
        b.join(timeout=10)
        assert other_done.is_set(), "an unrelated session was serialized"
        release.set()
        a.join(timeout=10)

    def test_the_canonical_order_physical_then_write_then_generation_is_accepted(self):
        """No inversion: session-workspace < session-workspace-write < terminal."""
        from cli_agent_orchestrator.services import callback_recovery

        with callback_recovery.session_lifecycle_claim("TmuxBackend", SESSION):
            with callback_recovery.session_lifecycle_write_claim(SESSION):
                with callback_recovery.generation_lifecycle_claims(
                    (("term-1", "model-generation", "g1"),)
                ):
                    pass  # accepted — canonical nesting

    def test_an_inverted_order_is_refused_rather_than_deadlocking(self):
        """A generation claim held first inverts the write claim and must raise.

        This is the deadlock guard: canonical order is enforced (RuntimeError)
        rather than waited on, so a buggy acquisition cannot wedge the process.
        """
        from cli_agent_orchestrator.services import callback_recovery

        with callback_recovery.generation_lifecycle_claims((("term-1", "model-generation", "g1"),)):
            with pytest.raises(RuntimeError, match="canonical order"):
                with callback_recovery.session_lifecycle_write_claim(SESSION):
                    pass


class TestLifecycleWriteClaimIsCrossProcess:
    """The lifecycle write claim must serialize across processes (a flock), not
    just threads. A mutation probe swapping it for a process-local mutex would
    pass the thread tests but must fail this one."""

    def test_a_child_cannot_enter_until_the_parent_releases(self):
        import os
        import subprocess
        import sys
        from queue import Empty, Queue
        from threading import Thread

        from cli_agent_orchestrator.services import callback_recovery

        session = f"cao-xprocess-oracle-{os.getpid()}"
        script = (
            "from cli_agent_orchestrator.services import callback_recovery\n"
            "import sys\n"
            "session = sys.argv[1]\n"
            "print('CHILD-STARTED', flush=True)\n"
            "with callback_recovery.session_lifecycle_write_claim(session):\n"
            "    print('CHILD-ACQUIRED', flush=True)\n"
            "print('CHILD-DONE', flush=True)\n"
        )

        parent = callback_recovery.session_lifecycle_write_claim(session)
        parent.__enter__()
        try:
            child = subprocess.Popen(
                [sys.executable, "-c", script, session],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            queue: Queue = Queue()

            def _pump(stream):
                for line in stream:
                    queue.put(line)

            reader = Thread(target=_pump, args=(child.stdout,), daemon=True)
            reader.start()

            started = queue.get(timeout=20)  # child imported + started
            assert "CHILD-STARTED" in started
            # Parent still holds the flock, so the child must be blocked: no
            # ACQUIRED line within a bounded window.
            with pytest.raises(Empty):
                queue.get(timeout=0.5)
            assert child.poll() is None, "child exited while the parent still held the claim"
        finally:
            parent.__exit__(None, None, None)

        # After release the child acquires and completes.
        acquired = queue.get(timeout=10)
        assert "CHILD-ACQUIRED" in acquired
        done = queue.get(timeout=10)
        assert "CHILD-DONE" in done
        child.wait(timeout=10)
