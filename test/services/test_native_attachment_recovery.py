"""Returning a provider session to circulation once its owner is gone.

The defect these cover: ``native_attachment.release`` was implemented,
tested, and never called, so every claim this system took stayed live
forever and every provider session on an install became unresumable. The
unit surface was already thorough — what was missing was anything that
used it, and a pin that anything does.

The negatives matter more than the positives here. A release on a false
no-survivor is the double-attach the whole store exists to prevent, and
the two ways to reach one are (a) reading "we could not look" as "nothing
is there" and (b) letting a start-marker comparison decide, which turns a
daylight-saving rollover into a mass release of live owners.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from cli_agent_orchestrator.services import native_attachment as na
from cli_agent_orchestrator.services import native_attachment_recovery as recovery

PROVIDER = "kimi_cli"
SESSION = "session_326c5026"
TERMINAL = "44dda40b"
GENERATION = "6e3d642e"
MARKER = "Fri Aug  7 12:51:19 2026"


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    return isolated_memory_db


def _intent() -> dict:
    return na.acquire_intent(
        acquisition_method=na.ACQUISITION_ACP_BOOTSTRAP,
        acquisition_receipt={"kind": "kimi-acp-session-new", "session_id": SESSION},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
        bootstrap_sent_no_turn=True,
        bootstrap_detached_before_launch=True,
    )


def _attach(
    *,
    pid: int,
    session: str = SESSION,
    terminal_id: str = TERMINAL,
    generation: str = GENERATION,
    marker: str = MARKER,
    pane_id: str | None = "%12",
) -> dict:
    """Walk one owner through declare → starting → attached."""
    owner = {
        "terminal_id": terminal_id,
        "generation": generation,
        "execution_mode": "native_tui",
    }
    na.declare(
        provider=PROVIDER,
        native_session_id=session,
        pane_id=pane_id,
        intent=_intent(),
        **owner,
    )
    na.mark_starting(provider=PROVIDER, native_session_id=session, **owner)
    return na.mark_attached(
        provider=PROVIDER,
        native_session_id=session,
        process_identity=na.process_identity(pid=pid, start_marker=marker),
        **owner,
    )


def _reaped_pid() -> int:
    """A pid that certainly does not exist: a child we started and waited on.

    Reusing an arbitrary large number risks colliding with a real process
    on a busy machine, and `ps` on this host refuses a pid above
    ``kern.maxproc`` with a different error entirely.
    """
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


class TestOwnerObservation:
    def test_a_dead_pid_is_an_empty_survivor_observation(self):
        record = _attach(pid=_reaped_pid())
        observed = recovery.observe_owner(record)
        assert observed["disposition"] == recovery.OWNER_GONE
        assert observed["survivors"] == []

    def test_a_live_pid_is_a_survivor_even_when_the_marker_matches_nothing(self):
        """The marker never decides. A live pid is a survivor either way.

        This is the daylight-saving pin. ``start_marker`` is ``ps -o lstart=``
        output — naive local wall-clock — so a live process re-renders
        differently after a timezone change, a DST rollover, or a locale
        change. A design that released on "alive but the marker differs"
        would read every one of those as a recycled pid and hand a live
        owner's session to a second attacher.
        """
        record = _attach(pid=os.getpid(), marker="Thu Jan  1 00:00:00 1970")
        observed = recovery.observe_owner(record)
        assert observed["disposition"] == recovery.OWNER_ALIVE
        assert observed["survivors"] != []
        assert observed["start_marker_verdict"] == "differs"

    def test_a_live_pid_with_a_matching_marker_is_a_survivor(self):
        record = _attach(pid=os.getpid())
        observed = recovery.observe_owner(record)
        assert observed["disposition"] == recovery.OWNER_ALIVE
        assert observed["survivors"] != []

    def test_an_unpublished_identity_is_never_an_absence(self):
        """A claim written before its process existed proves nothing.

        ``declare`` journals the claim *before* the provider launches, so a
        row with no published identity is either a launch in flight or a
        crash during one, and nothing observable tells those apart.
        """
        record, _ = na.declare(
            provider=PROVIDER,
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode="native_tui",
            intent=_intent(),
        )
        observed = recovery.observe_owner(record)
        assert observed["disposition"] == recovery.OWNER_UNPUBLISHED
        assert recovery.release_if_owner_gone(record)["action"] == "skipped"

    def test_an_unreadable_process_table_is_not_an_absence(self, monkeypatch):
        """ "We could not look" must never be recorded as "nothing is there".

        The pre-existing helper in this codebase collapses both into a
        single ``None``; routing the decision through it would report every
        row on a host without a readable process table as releasable.
        """
        monkeypatch.setattr(recovery, "_pid_state", lambda pid: recovery.OWNER_UNOBSERVABLE)
        record = _attach(pid=os.getpid())
        observed = recovery.observe_owner(record)
        assert observed["disposition"] == recovery.OWNER_UNOBSERVABLE
        assert observed["survivors"] != []
        assert recovery.release_if_owner_gone(record)["action"] == "skipped"

    def test_the_marker_is_an_opaque_token_and_survives_bsd_double_spacing(self):
        """``ps -o lstart=`` pads a single-digit day with two spaces.

        Any normalisation — parsing to a datetime, collapsing whitespace —
        would make a stored marker unequal to the same process's live one.
        """
        record = _attach(pid=os.getpid(), marker=MARKER)
        assert record["owner"]["process_identity"]["start_marker"] == "Fri Aug  7 12:51:19 2026"


class TestReleaseOnProvenAbsence:
    def test_a_gone_owner_is_released_with_a_no_survivor_proof(self):
        record = _attach(pid=_reaped_pid())
        outcome = recovery.release_if_owner_gone(record)
        assert outcome["action"] == "released"
        stored = na.get(PROVIDER, SESSION)
        assert stored["state"] == na.DETACHED
        assert stored["release_proof"]["schema"] == na.NO_SURVIVOR_PROOF_SCHEMA
        assert stored["release_proof"]["survivors"] == []

    def test_a_released_session_can_be_claimed_again(self):
        """The point of the fix: the session comes back into circulation."""
        recovery.release_if_owner_gone(_attach(pid=_reaped_pid()))
        record, acquired = na.declare(
            provider=PROVIDER,
            native_session_id=SESSION,
            terminal_id="ffffffff",
            generation="99999999",
            execution_mode="native_tui",
            intent=_intent(),
        )
        assert acquired is True
        assert record["owner"]["terminal_id"] == "ffffffff"

    def test_a_live_owner_is_never_released(self):
        record = _attach(pid=os.getpid())
        assert recovery.release_if_owner_gone(record)["action"] == "skipped"
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_a_frozen_row_is_not_touched(self):
        _attach(pid=_reaped_pid())
        na.mark_ambiguous(provider=PROVIDER, native_session_id=SESSION, reason="unreadable")
        record = na.get(PROVIDER, SESSION)
        assert recovery.release_if_owner_gone(record)["action"] == "skipped"
        assert na.get(PROVIDER, SESSION)["state"] == na.AMBIGUOUS

    def test_a_stale_record_loses_the_compare_and_swap_rather_than_forcing(self):
        """A row that moved under the observation is skipped, never forced."""
        record = _attach(pid=_reaped_pid())
        na.mark_draining(
            provider=PROVIDER,
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode="native_tui",
        )
        stale = dict(record)
        stale["owner"] = dict(record["owner"])
        stale["owner"]["generation"] = "not-the-owner"
        outcome = recovery.release_if_owner_gone(stale)
        assert outcome["action"] == "refused"
        assert na.get(PROVIDER, SESSION)["state"] == na.DRAINING


class TestSweep:
    def test_the_sweep_is_dry_run_by_default_and_changes_no_epoch(self):
        _attach(pid=_reaped_pid())
        before = na.get(PROVIDER, SESSION)
        report = recovery.sweep()
        assert report["applied"] is False
        assert report["outcomes"][0]["action"] == "would-release"
        after = na.get(PROVIDER, SESSION)
        assert after["epoch"] == before["epoch"]
        assert after["state"] == na.ATTACHED

    def test_the_sweep_releases_only_the_provably_gone(self):
        _attach(pid=_reaped_pid(), session="gone-session", terminal_id="t-gone")
        _attach(pid=os.getpid(), session="live-session", terminal_id="t-live")
        na.declare(
            provider=PROVIDER,
            native_session_id="unpublished-session",
            terminal_id="t-unpub",
            generation=GENERATION,
            execution_mode="native_tui",
            intent=_intent(),
        )
        _attach(pid=_reaped_pid(), session="frozen-session", terminal_id="t-frozen")
        na.mark_ambiguous(
            provider=PROVIDER, native_session_id="frozen-session", reason="unreadable"
        )

        report = recovery.sweep(apply=True)
        assert report["counts"]["released"] == 1
        assert na.get(PROVIDER, "gone-session")["state"] == na.DETACHED
        assert na.get(PROVIDER, "live-session")["state"] == na.ATTACHED
        assert na.get(PROVIDER, "unpublished-session")["state"] == na.DECLARED
        assert na.get(PROVIDER, "frozen-session")["state"] == na.AMBIGUOUS

    def test_the_sweep_does_not_examine_frozen_or_detached_rows(self):
        """Only live states hold a session; the rest are history or a human's."""
        _attach(pid=_reaped_pid(), session="frozen-session")
        na.mark_ambiguous(
            provider=PROVIDER, native_session_id="frozen-session", reason="unreadable"
        )
        assert recovery.sweep()["examined"] == 0

    def test_the_report_records_the_observation_environment(self):
        """The stored markers are rendered in some timezone; say which."""
        report = recovery.sweep()
        assert set(report["environment"]) == {"tz", "tz_env", "lc_all", "lc_time"}
        # The effective zone, not just the variable: TZ is unset on a default
        # install, so recording only the variable wrote nulls and said nothing.
        assert report["environment"]["tz"]

    def test_the_startup_sweep_never_raises(self, monkeypatch):
        def _boom(**_kwargs):
            raise RuntimeError("store unreadable")

        monkeypatch.setattr(recovery, "sweep", _boom)
        assert "error" in recovery.sweep_at_startup()


class TestTheBootSweepDoesNotMutateByDefault:
    """Entering this application's lifespan is something a test does.

    Two tests in this repository enter it without stubbing the recovery
    steps, so a boot sweep that applied by default would release rows from
    an operator's real database as a side effect of running pytest.
    """

    def test_the_boot_sweep_reports_and_changes_nothing(self, monkeypatch):
        monkeypatch.delenv(recovery.SWEEP_ON_BOOT_ENV, raising=False)
        _attach(pid=_reaped_pid())
        report = recovery.sweep_at_startup()
        assert report["applied"] is False
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_the_boot_sweep_names_the_command_that_would_fix_it(self, monkeypatch, caplog):
        monkeypatch.delenv(recovery.SWEEP_ON_BOOT_ENV, raising=False)
        _attach(pid=_reaped_pid())
        with caplog.at_level("WARNING"):
            recovery.sweep_at_startup()
        assert "cao attachment sweep --apply" in caplog.text

    def test_an_explicit_opt_in_applies(self, monkeypatch):
        monkeypatch.setenv(recovery.SWEEP_ON_BOOT_ENV, "apply")
        _attach(pid=_reaped_pid())
        report = recovery.sweep_at_startup()
        assert report["applied"] is True
        assert na.get(PROVIDER, SESSION)["state"] == na.DETACHED

    def test_any_other_value_is_not_an_opt_in(self, monkeypatch):
        monkeypatch.setenv(recovery.SWEEP_ON_BOOT_ENV, "true")
        _attach(pid=_reaped_pid())
        assert recovery.sweep_at_startup()["applied"] is False
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED


class TestOperatorFreeze:
    """The bridge between "the sweep can never settle this" and adjudication.

    `adjudicate` only accepts a frozen row and teardown never freezes one,
    so without this a claim whose owner can never be observed would stay
    attached forever with no operator path at all.
    """

    def test_an_unobservable_owner_can_be_declared_unresolvable(self, monkeypatch):
        _attach(pid=os.getpid())
        monkeypatch.setattr(recovery, "_pid_state", lambda pid: recovery.OWNER_UNOBSERVABLE)
        result = recovery.freeze_for_adjudication(
            provider=PROVIDER,
            native_session_id=SESSION,
            operator="colin",
            detail="process table will not answer for this pid",
        )
        assert result["state"] == na.AMBIGUOUS
        assert result["ambiguity_reason"].startswith(recovery.OPERATOR_FREEZE_REASON)
        assert "colin" in result["ambiguity_reason"]

    def test_a_claim_with_no_published_identity_is_refused(self):
        """A cold-starting launch and a crashed one are the same row.

        `declare` writes the claim before the provider process exists, so
        both are `starting` with no pid, and on the operator's screen they
        are the same line. Freezing one blocks its own `mark_attached`, so
        the identity is never published, the process keeps running, and the
        adjudication that follows sees an empty survivor list and hands the
        session away underneath it. That is the double-attach this module
        exists to prevent, reached through the command meant to repair it.
        """
        na.declare(
            provider=PROVIDER,
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode="native_tui",
            intent=_intent(),
        )
        with pytest.raises(na.NativeAttachmentConflict, match="no published process identity"):
            recovery.freeze_for_adjudication(
                provider=PROVIDER,
                native_session_id=SESSION,
                operator="colin",
                detail="looks stuck",
            )
        assert na.get(PROVIDER, SESSION)["state"] == na.DECLARED

    def test_a_running_launch_cannot_be_walked_to_detached(self):
        """The whole two-step path, against a real process that is alive."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            na.declare(
                provider=PROVIDER,
                native_session_id=SESSION,
                terminal_id=TERMINAL,
                generation=GENERATION,
                execution_mode="native_tui",
                intent=_intent(),
            )
            na.mark_starting(
                provider=PROVIDER,
                native_session_id=SESSION,
                terminal_id=TERMINAL,
                generation=GENERATION,
                execution_mode="native_tui",
            )
            with pytest.raises(na.NativeAttachmentConflict):
                recovery.freeze_for_adjudication(
                    provider=PROVIDER,
                    native_session_id=SESSION,
                    operator="colin",
                    detail="listed as starting for a while",
                )
            # The launch can still finish, which is the point.
            published = na.mark_attached(
                provider=PROVIDER,
                native_session_id=SESSION,
                terminal_id=TERMINAL,
                generation=GENERATION,
                execution_mode="native_tui",
                process_identity=na.process_identity(pid=child.pid, start_marker=MARKER),
            )
            assert published["state"] == na.ATTACHED
        finally:
            child.kill()
            child.wait()

    def test_a_running_owner_is_refused(self):
        """A live process is an answered ownership question, not an open one."""
        _attach(pid=os.getpid())
        with pytest.raises(na.NativeAttachmentConflict):
            recovery.freeze_for_adjudication(
                provider=PROVIDER,
                native_session_id=SESSION,
                operator="colin",
                detail="I want it back",
            )
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_a_provably_gone_owner_is_refused_and_pointed_at_the_sweep(self):
        """Freezing here would invent a judgement call nobody needs to make."""
        _attach(pid=_reaped_pid())
        with pytest.raises(na.NativeAttachmentConflict, match="sweep --apply"):
            recovery.freeze_for_adjudication(
                provider=PROVIDER,
                native_session_id=SESSION,
                operator="colin",
                detail="looks dead to me",
            )
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_an_unclaimed_session_is_not_found(self):
        with pytest.raises(na.NativeAttachmentNotFound):
            recovery.freeze_for_adjudication(
                provider=PROVIDER,
                native_session_id="never-claimed",
                operator="colin",
                detail="x",
            )

    def test_the_frozen_row_is_then_adjudicable(self, monkeypatch):
        """The two steps compose: declare unresolvable, then decide."""
        _attach(pid=os.getpid())
        monkeypatch.setattr(recovery, "_pid_state", lambda pid: recovery.OWNER_UNOBSERVABLE)
        recovery.freeze_for_adjudication(
            provider=PROVIDER,
            native_session_id=SESSION,
            operator="colin",
            detail="process table will not answer",
        )
        monkeypatch.setattr(recovery, "_pid_state", lambda pid: recovery.OWNER_GONE)
        record = na.get(PROVIDER, SESSION)
        observation = recovery.observe_owner(record)
        result = na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=na.adjudication(
                outcome=na.ADJUDICATION_OUTCOME_OWNER_GONE,
                evidence_sha256="e" * 64,
                detail="host rebooted; nothing survived",
                operator="colin",
                observed_at=observation["observed_at"],
                observation=observation,
            ),
            live_survivors=observation["survivors"],
            observed_epoch=record["epoch"],
        )
        assert result["state"] == na.DETACHED


class TestOneBadRowDoesNotEndThePass:
    """A sweep is a batch operation over rows nobody validated on the way in.

    The store enforces its vocabulary at write time, but a build that
    changes that vocabulary — or a row written by an older one — turns a
    read into a raise. Letting that escape would give an operator a partial
    run and no report saying which rows were even examined.
    """

    def test_a_row_that_cannot_be_proved_is_recorded_not_raised(self, monkeypatch):
        _attach(pid=_reaped_pid())

        def _no_longer_valid(**_kwargs):
            raise RuntimeError("execution mode 'native_tui' is no longer recognised")

        monkeypatch.setattr(na, "no_survivor_proof", _no_longer_valid)
        outcome = recovery.release_if_owner_gone(na.get(PROVIDER, SESSION))
        assert outcome["action"] == "errored"
        assert outcome["reason"] == "RuntimeError"
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_the_sweep_finishes_and_reports_every_other_row(self, monkeypatch):
        _attach(pid=_reaped_pid(), session="bad-row", terminal_id="t-bad")
        _attach(pid=_reaped_pid(), session="good-row", terminal_id="t-good")

        real = na.no_survivor_proof

        def _selective(**kwargs):
            if kwargs["native_session_id"] == "bad-row":
                raise RuntimeError("unrecognised")
            return real(**kwargs)

        monkeypatch.setattr(na, "no_survivor_proof", _selective)
        report = recovery.sweep(apply=True)
        assert report["examined"] == 2
        assert report["counts"]["released"] == 1
        assert report["counts"]["errored"] == 1
        assert na.get(PROVIDER, "good-row")["state"] == na.DETACHED
        assert na.get(PROVIDER, "bad-row")["state"] == na.ATTACHED


class TestTheRecycledPidHasAValve:
    """The one row automation is designed never to resolve.

    A recycled pid is alive under a marker that differs from the recorded
    one. Automation may not act on that — a timezone change produces
    identical evidence — so without an operator path the session is
    stranded permanently, and every future pid collision strands another.
    The valve is a named human attesting to what they looked at.
    """

    def _recycled(self) -> dict:
        """A live pid whose recorded marker is not the one it reports."""
        _attach(pid=os.getpid(), marker="Thu Jan  1 00:00:00 1970")
        return na.get(PROVIDER, SESSION)

    def test_the_sweep_correctly_refuses_it(self):
        self._recycled()
        assert recovery.sweep(apply=True)["counts"].get("released", 0) == 0

    def test_freezing_it_needs_the_attestation(self):
        self._recycled()
        with pytest.raises(na.NativeAttachmentConflict, match="only a human can tell"):
            recovery.freeze_for_adjudication(
                provider=PROVIDER,
                native_session_id=SESSION,
                operator="colin",
                detail="pid looks recycled",
            )

    def test_the_attestation_records_both_markers(self):
        self._recycled()
        frozen = recovery.freeze_for_adjudication(
            provider=PROVIDER,
            native_session_id=SESSION,
            operator="colin",
            detail="pid 42599 is now an unrelated jupyter kernel",
            attest_live_pid_is_not_the_owner=True,
        )
        assert frozen["state"] == na.AMBIGUOUS
        assert frozen["ambiguity_reason"].startswith(na.RECYCLED_PID_ATTESTATION)
        assert "Thu Jan  1 00:00:00 1970" in frozen["ambiguity_reason"]

    def test_the_attested_row_then_adjudicates_despite_a_live_pid(self):
        """The second gate opens only for the survivor the attestation covers."""
        self._recycled()
        recovery.freeze_for_adjudication(
            provider=PROVIDER,
            native_session_id=SESSION,
            operator="colin",
            detail="verified unrelated process",
            attest_live_pid_is_not_the_owner=True,
        )
        record = na.get(PROVIDER, SESSION)
        observation = recovery.observe_owner(record)
        assert observation["survivors"], "the pid really is still alive"
        result = na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=na.adjudication(
                outcome=na.ADJUDICATION_OUTCOME_OWNER_GONE,
                evidence_sha256="a" * 64,
                detail="checked argv; unrelated process",
                operator="colin",
                observed_at=observation["observed_at"],
                observation=observation,
                attests_live_process_is_not_the_owner=True,
            ),
            live_survivors=observation["survivors"],
            observed_epoch=record["epoch"],
        )
        assert result["state"] == na.DETACHED

    def test_an_already_frozen_row_can_still_be_attested(self):
        """`mark_ambiguous` preserves the FIRST reason.

        With the attestation on the freeze, a row its launcher had already
        frozen for an unrelated reason silently dropped it and could then
        never be adjudicated while the stranger process lived. The
        attestation belongs on the decision, not the classification.
        """
        _attach(pid=os.getpid(), marker="Thu Jan  1 00:00:00 1970")
        na.mark_ambiguous(
            provider=PROVIDER, native_session_id=SESSION, reason="pane_render_mismatch"
        )
        record = na.get(PROVIDER, SESSION)
        assert record["ambiguity_reason"] == "pane_render_mismatch"
        observation = recovery.observe_owner(record)
        result = na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=na.adjudication(
                outcome=na.ADJUDICATION_OUTCOME_OWNER_GONE,
                evidence_sha256="a" * 64,
                detail="checked argv; unrelated process",
                operator="colin",
                observed_at=observation["observed_at"],
                observation=observation,
                attests_live_process_is_not_the_owner=True,
            ),
            live_survivors=recovery.survivors_blocking_adjudication(observation),
            observed_epoch=record["epoch"],
        )
        assert result["state"] == na.DETACHED

    def test_an_unreadable_live_marker_is_not_a_difference(self, monkeypatch):
        """`None` is unequal to anything, and that is not evidence."""
        _attach(pid=os.getpid(), marker="Thu Jan  1 00:00:00 1970")
        na.mark_ambiguous(provider=PROVIDER, native_session_id=SESSION, reason="render mismatch")
        monkeypatch.setattr(recovery, "_live_start_marker", lambda pid: None)
        record = na.get(PROVIDER, SESSION)
        observation = recovery.observe_owner(record)
        assert observation["start_marker_verdict"] == "unreadable"
        with pytest.raises(na.NativeAttachmentConflict, match="observably alive"):
            na.adjudicate(
                provider=PROVIDER,
                native_session_id=SESSION,
                record=na.adjudication(
                    outcome=na.ADJUDICATION_OUTCOME_OWNER_GONE,
                    evidence_sha256="a" * 64,
                    detail="x",
                    operator="colin",
                    observed_at=observation["observed_at"],
                    observation=observation,
                    attests_live_process_is_not_the_owner=True,
                ),
                live_survivors=recovery.survivors_blocking_adjudication(observation),
                observed_epoch=record["epoch"],
            )

    def test_an_owner_alive_under_its_own_marker_is_refused_even_attested(self):
        """No attestation makes the recorded owner, running, releasable.

        The recorded marker here is the *real* one this process reports, so
        the observation says "matches" — the recorded owner, alive.
        """
        from cli_agent_orchestrator.services import native_tui_launch

        real_marker = native_tui_launch._process_field(os.getpid(), "lstart=")
        assert real_marker, "could not read this process's own start marker"
        _attach(pid=os.getpid(), marker=real_marker)
        with pytest.raises(na.NativeAttachmentConflict, match="is running"):
            recovery.freeze_for_adjudication(
                provider=PROVIDER,
                native_session_id=SESSION,
                operator="colin",
                detail="I want it back",
                attest_live_pid_is_not_the_owner=True,
            )

    def test_an_attestation_does_not_cover_a_matching_survivor(self):
        """A row frozen for a recycled pid does not become a blanket permit."""
        _attach(pid=os.getpid(), marker="Thu Jan  1 00:00:00 1970")
        recovery.freeze_for_adjudication(
            provider=PROVIDER,
            native_session_id=SESSION,
            operator="colin",
            detail="recycled",
            attest_live_pid_is_not_the_owner=True,
        )
        record = na.get(PROVIDER, SESSION)
        observation = recovery.observe_owner(record)
        with pytest.raises(na.NativeAttachmentConflict, match="observably alive"):
            na.adjudicate(
                provider=PROVIDER,
                native_session_id=SESSION,
                record=na.adjudication(
                    outcome=na.ADJUDICATION_OUTCOME_OWNER_GONE,
                    evidence_sha256="a" * 64,
                    detail="x",
                    operator="colin",
                    observed_at=observation["observed_at"],
                    observation=observation,
                ),
                # A survivor bearing the RECORDED marker is the owner itself.
                live_survivors=[{"pid": os.getpid(), "start_marker": "Thu Jan  1 00:00:00 1970"}],
                observed_epoch=record["epoch"],
            )


class TestThePidStateDecision:
    """The one function that decides every release, tested directly.

    Two of its five branches were covered end to end — `ProcessLookupError`
    by a reaped pid, and the fall-through by this interpreter — so a
    mutation that turned `PermissionError` into "gone" left the whole suite
    green. That is the branch that matters in practice: a provider running
    under a different uid is a deployment, not a hypothetical, and reading
    it as absence releases a session out from under a running process.
    """

    def test_a_pid_owned_by_another_user_is_alive_not_absent(self):
        """`os.kill(1, 0)` raises PermissionError for a non-root caller."""
        assert os.geteuid() != 0, "this test is meaningless as root"
        assert recovery._pid_state(1) == recovery.OWNER_ALIVE

    def test_a_reaped_pid_is_gone(self):
        assert recovery._pid_state(_reaped_pid()) == recovery.OWNER_GONE

    def test_this_process_is_alive(self):
        assert recovery._pid_state(os.getpid()) == recovery.OWNER_ALIVE

    @pytest.mark.parametrize("pid", [0, -1, -12345])
    def test_a_pid_that_is_not_a_pid_is_unobservable_never_absent(self, pid):
        """`os.kill(0, 0)` signals the whole process group and succeeds.

        Reading either of those as "the owner is gone" would release on an
        argument that was never a process id.
        """
        assert recovery._pid_state(pid) == recovery.OWNER_UNOBSERVABLE

    def test_an_unexpected_oserror_is_unobservable_never_absent(self, monkeypatch):
        def _boom(_pid, _sig):
            raise OSError(999, "something else entirely")

        monkeypatch.setattr(recovery.os, "kill", _boom)
        assert recovery._pid_state(4242) == recovery.OWNER_UNOBSERVABLE


class TestTheValveWorksForTheCaseItWasBuiltFor:
    """Freeze accepts an unobservable owner; adjudicate must not then refuse it.

    `observe_owner` puts an entry in `survivors` for an owner it could not
    check — right for automation, since an unchecked owner is treated as
    alive. Applied to adjudication it broke the valve exactly where it was
    needed: an operator froze a claim *because* the process table would not
    answer, and was refused with "the frozen owner is still observably
    alive" about a process nobody observed.
    """

    def test_an_unobservable_owner_survives_the_whole_two_step_path(self, monkeypatch):
        _attach(pid=os.getpid())
        monkeypatch.setattr(recovery, "_pid_state", lambda pid: recovery.OWNER_UNOBSERVABLE)

        recovery.freeze_for_adjudication(
            provider=PROVIDER,
            native_session_id=SESSION,
            operator="colin",
            detail="process table will not answer for this pid",
        )
        record = na.get(PROVIDER, SESSION)
        observation = recovery.observe_owner(record)
        assert observation["disposition"] == recovery.OWNER_UNOBSERVABLE
        assert observation["survivors"], "the conservative placeholder is still there"

        result = na.adjudicate(
            provider=PROVIDER,
            native_session_id=SESSION,
            record=na.adjudication(
                outcome=na.ADJUDICATION_OUTCOME_OWNER_GONE,
                evidence_sha256="a" * 64,
                detail="host was rebuilt; the pid cannot exist",
                operator="colin",
                observed_at=observation["observed_at"],
                observation=observation,
            ),
            live_survivors=recovery.survivors_blocking_adjudication(observation),
            observed_epoch=record["epoch"],
        )
        assert result["state"] == na.DETACHED

    def test_a_seen_running_process_still_blocks(self):
        """The veto is on a process actually seen, and that must still fire."""
        from cli_agent_orchestrator.services import native_tui_launch

        marker = native_tui_launch._process_field(os.getpid(), "lstart=")
        _attach(pid=os.getpid(), marker=marker)
        na.mark_ambiguous(provider=PROVIDER, native_session_id=SESSION, reason="render mismatch")
        record = na.get(PROVIDER, SESSION)
        observation = recovery.observe_owner(record)
        assert recovery.survivors_blocking_adjudication(observation)
        with pytest.raises(na.NativeAttachmentConflict, match="observably alive"):
            na.adjudicate(
                provider=PROVIDER,
                native_session_id=SESSION,
                record=na.adjudication(
                    outcome=na.ADJUDICATION_OUTCOME_OWNER_GONE,
                    evidence_sha256="a" * 64,
                    detail="x",
                    operator="colin",
                    observed_at=observation["observed_at"],
                    observation=observation,
                ),
                live_survivors=recovery.survivors_blocking_adjudication(observation),
                observed_epoch=record["epoch"],
            )

    def test_an_unpublished_owner_never_blocks(self):
        """There is no process to have seen; that is why a human is here."""
        assert (
            recovery.survivors_blocking_adjudication(
                {"disposition": recovery.OWNER_UNPUBLISHED, "survivors": []}
            )
            == []
        )
        assert (
            recovery.survivors_blocking_adjudication(
                {"disposition": recovery.OWNER_UNOBSERVABLE, "survivors": [{"pid": 1}]}
            )
            == []
        )


class TestTheGraceLoopDoesNotForkPsFortyTimes:
    def test_polling_skips_the_marker_it_would_discard(self):
        """The marker decides nothing, and `gone` never reads one at all."""
        record = (
            na.get(PROVIDER, SESSION) if na.get(PROVIDER, SESSION) else _attach(pid=os.getpid())
        )
        cheap = recovery.observe_owner(record, read_marker=False)
        assert cheap["disposition"] == recovery.OWNER_ALIVE
        assert cheap["start_marker_verdict"] == "not-read"


class TestTheFreezeIsBoundToTheRowItObserved:
    """The same hole `adjudicate` had, on its sibling.

    `mark_ambiguous` takes no owner, so without an epoch a freeze decided
    against one row can land on whatever is there when it writes — and the
    dangerous landing is a launch that has claimed the session but not yet
    published an identity, which the freeze then blocks while its process
    keeps running.
    """

    def test_a_row_that_moved_since_the_observation_is_refused(self, monkeypatch):
        _attach(pid=os.getpid())
        monkeypatch.setattr(recovery, "_pid_state", lambda pid: recovery.OWNER_UNOBSERVABLE)
        stale_record = na.get(PROVIDER, SESSION)

        real_mark = na.mark_ambiguous

        def _race_then_freeze(**kwargs):
            # The observed owner dies, a sweep releases it, and a new launch
            # claims the session — all between the look and the write.
            monkeypatch.setattr(recovery, "_pid_state", lambda pid: recovery.OWNER_GONE)
            recovery.release_if_owner_gone(na.get(PROVIDER, SESSION))
            na.declare(
                provider=PROVIDER,
                native_session_id=SESSION,
                terminal_id="newowner",
                generation="newgen",
                execution_mode="native_tui",
                intent=_intent(),
            )
            return real_mark(**kwargs)

        monkeypatch.setattr(na, "mark_ambiguous", _race_then_freeze)
        with pytest.raises(na.NativeAttachmentConflict, match="moved to epoch"):
            recovery.freeze_for_adjudication(
                provider=PROVIDER,
                native_session_id=SESSION,
                operator="colin",
                detail="process table will not answer",
            )
        current = na.get(PROVIDER, SESSION)
        assert current["state"] == na.DECLARED
        assert current["owner"]["terminal_id"] == "newowner"

    def test_an_owner_freezing_its_own_row_stays_unbound(self):
        """A launch that could not verify its pane must freeze regardless."""
        import inspect

        assert inspect.signature(na.mark_ambiguous).parameters["expected_epoch"].default is None
        _attach(pid=os.getpid())
        assert (
            na.mark_ambiguous(
                provider=PROVIDER, native_session_id=SESSION, reason="pane unreadable"
            )["state"]
            == na.AMBIGUOUS
        )


class TestARetainedProofIsNotThisClaimsProof:
    """A re-acquire keeps the prior proof deliberately — it is evidence.

    The record shape is unchanged, because it is a contract several readers
    depend on. What changed is the rendering: on a live claim that proof
    describes a *different* owner, and printing it unlabelled reads as
    "this claim was released", which is the opposite of true.
    """

    def test_the_record_still_carries_the_prior_proof(self):
        recovery.release_if_owner_gone(_attach(pid=_reaped_pid()))
        na.declare(
            provider=PROVIDER,
            native_session_id=SESSION,
            terminal_id="ffffffff",
            generation="99999999",
            execution_mode="native_tui",
            intent=_intent(),
        )
        reclaimed = na.get(PROVIDER, SESSION)
        assert reclaimed["state"] == na.DECLARED
        assert reclaimed["release_proof"] is not None
        assert reclaimed["release_proof"]["terminal_id"] == TERMINAL


class TestTheGraceWaitDeclinesToBlockAnEventLoop:
    """Previous-round fix: the blocking sleep must not run on a loop.

    Removing the guard failed zero tests, which is the same shape as the
    defect this whole change is about.
    """

    def test_the_wait_is_skipped_when_a_loop_is_running(self):
        import asyncio
        import time as _time

        recovery.TEARDOWN_GRACE_SECONDS = 5.0
        try:
            _attach(pid=os.getpid())

            async def _run():
                started = _time.monotonic()
                outcomes = recovery.release_owned_by_terminal(TERMINAL)
                return _time.monotonic() - started, outcomes

            elapsed, outcomes = asyncio.run(_run())
        finally:
            recovery.TEARDOWN_GRACE_SECONDS = 2.0
        assert elapsed < 2.0, "the grace wait blocked the event loop"
        assert outcomes[0]["action"] == "skipped"

    def test_the_wait_does_run_off_a_loop(self):
        import time as _time

        recovery.TEARDOWN_GRACE_SECONDS = 0.4
        try:
            _attach(pid=os.getpid())
            started = _time.monotonic()
            recovery.release_owned_by_terminal(TERMINAL)
            elapsed = _time.monotonic() - started
        finally:
            recovery.TEARDOWN_GRACE_SECONDS = 2.0
        assert elapsed >= 0.4, "the grace wait did not happen at all"
