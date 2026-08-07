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
        assert set(report["environment"]) == {"tz", "lc_all", "lc_time"}

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

    def test_a_claim_with_no_published_identity_can_be_declared_unresolvable(self):
        na.declare(
            provider=PROVIDER,
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode="native_tui",
            intent=_intent(),
        )
        result = recovery.freeze_for_adjudication(
            provider=PROVIDER,
            native_session_id=SESSION,
            operator="colin",
            detail="launch crashed before publishing an identity",
        )
        assert result["state"] == na.AMBIGUOUS

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
        )
        assert result["state"] == na.DETACHED
