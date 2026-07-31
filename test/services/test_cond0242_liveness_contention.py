"""COND-0242: the FIFO pipe-pane liveness probe must not make the API unavailable.

Observed in production (``evidence/cao-server-fifo-libtmux-event-loop-
starvation-20260730.md`` and the process-bound log
``cao_2026-07-30_19-05-36.log``)::

    fifo_reader._watchdog_loop
      -> fifo_reader._check_pipe_liveness
      -> terminal_service._probe_pane
      -> tmux_backend.get_history
      -> libtmux ... fetch_objs / parse_output
    ValueError: zip() argument 2 is shorter than argument 1

repeating every watchdog tick, per enrolled terminal, while the listener stayed
alive and ``/health``, terminal status, the sentinel loopback and the dashboard
all timed out.

Two independent defects combine into that control-plane unavailability, and
both are reproduced here:

1. **Breadth.** Resolving ``session:window`` through libtmux issues a
   SERVER-WIDE ``list-sessions`` and then a whole-session ``list-windows``,
   each rendered with a 136-field format that libtmux parses with a strict
   field-count ``zip``. One malformed row anywhere on the server fails the
   whole observation, and every such observation contends for the single tmux
   server and for the CAO API's shared blocking-call capacity — the same
   resources ordinary requests need. A liveness check only ever wants ONE
   pane's tail, so the breadth is pure, avoidable contention.

2. **Unboundedness.** ``_watchdog_loop`` catches the failure, logs a full
   traceback, and re-issues the identical broad observation on the very next
   tick with no backoff and no log rate limit. A persistent malformed listing
   therefore becomes a permanent storm rather than one bounded, isolated error.

The correction is narrow, not general: liveness/history *observation* resolves
its target with a bounded, single-window tmux call and captures the exact pane.
Write and control paths keep their existing identity guarantees untouched.
"""

import asyncio
import contextlib
import logging
import subprocess
import threading
import time
from unittest.mock import MagicMock

import pytest

import cli_agent_orchestrator.services.fifo_reader as fr
from cli_agent_orchestrator.services.fifo_reader import FifoManager

# The exact libtmux failure recorded in the process log.
LIBTMUX_PARSE_ERROR = "zip() argument 2 is shorter than argument 1"

# How long one server-wide libtmux observation occupies the shared tmux
# server / blocking-call capacity in this reproduction. Real ones were slow
# enough to time out 5s bounded requests; 0.2s keeps the test quick while
# staying far above the budget a trivial request is held to.
BROAD_OBSERVATION_HOLD_S = 0.2

# What a single-pane capture costs on the same shared resource.
NARROW_OBSERVATION_HOLD_S = 0.002


class _SharedTmuxResource:
    """The one resource a liveness probe and an ordinary API request share.

    A tmux server serialises the commands sent to it, and CAO runs every
    blocking tmux call from its API on shared executor capacity. Modelling
    that as a single mutex is what makes "a liveness observation starved the
    control plane" reproducible without a live server.
    """

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self.broad_observations = 0
        self.narrow_observations = 0
        self.api_requests = 0

    @contextlib.contextmanager
    def _held(self, seconds: float):
        with self._mutex:
            time.sleep(seconds)
            yield

    def server_wide_listing(self):
        """A libtmux ``fetch_objs()`` over every session/window on the server.

        Holds the shared resource for its whole duration and then fails the
        way production failed: one malformed 136-field row anywhere.
        """
        with self._held(BROAD_OBSERVATION_HOLD_S):
            self.broad_observations += 1
        raise ValueError(LIBTMUX_PARSE_ERROR)

    def single_pane_observation(self, stdout: str = "%0") -> str:
        with self._held(NARROW_OBSERVATION_HOLD_S):
            self.narrow_observations += 1
        return stdout

    def trivial_api_request(self) -> str:
        """What ``/health``-class work costs on the same shared resource."""
        with self._held(NARROW_OBSERVATION_HOLD_S):
            self.api_requests += 1
        return "ok"


@pytest.fixture
def shared_tmux():
    return _SharedTmuxResource()


@pytest.fixture
def probing_client(shared_tmux, monkeypatch):
    """A real ``TmuxClient`` whose every route to tmux runs through the shared
    resource, so the probe's *breadth* is what decides the contention.

    ``server`` is the libtmux object (broad, server-wide, strict-zip parsed).
    ``subprocess.run`` is the direct, bounded, single-window route. Whichever
    one ``get_history`` chooses is what this reproduction measures.
    """
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    client = TmuxClient.__new__(TmuxClient)

    server = MagicMock()
    server.sessions.get.side_effect = lambda **_: shared_tmux.server_wide_listing()
    client.server = server

    def fake_run(cmd, *args, **kwargs):
        argv = list(cmd)
        if "list-panes" in argv:
            out = shared_tmux.single_pane_observation("%0\n")
        elif "capture-pane" in argv:
            out = shared_tmux.single_pane_observation("pane tail line\n")
        else:  # pragma: no cover - the probe must not reach any other command
            raise AssertionError(f"unexpected tmux command in a liveness probe: {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr("cli_agent_orchestrator.clients.tmux.subprocess.run", fake_run)
    # Pinned so the reproduction never depends on tmux being installed.
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.tmux.tmux_binary", lambda: "/usr/bin/tmux"
    )
    return client


def _probe(client):
    """Exactly what ``terminal_service._probe_pane`` does per liveness check."""
    return lambda: client.get_history("cao-p1-closure", "managed-0d618fcf", tail_lines=80)


class TestLivenessProbeBreadth:
    def test_liveness_probe_issues_no_server_wide_observation(self, shared_tmux, probing_client):
        """A liveness/history read wants one pane's tail. Reaching it through a
        server-wide ``list-sessions`` + whole-session ``list-windows`` is what
        let one malformed row anywhere on the server fail the observation and
        what put every check in contention with ordinary API work.

        Pre-fix this raises ``ValueError: zip() argument 2 is shorter than
        argument 1`` from the very first call, exactly as production did.
        """
        content = _probe(probing_client)()

        assert content, "the probe must still return the pane's live content"
        assert shared_tmux.broad_observations == 0, (
            "a single-pane liveness observation must not issue a server-wide "
            "libtmux listing — that breadth is COND-0242's contention source"
        )
        assert shared_tmux.narrow_observations >= 1


class TestConcurrentApiAvailability:
    """The acceptance boundary from the evidence: an observed full-list parse
    ``ValueError`` and a bounded-slow observation must not stop terminal-status,
    sentinel-loopback, dashboard or ordinary API work from being served.

    The proven production impact was shared tmux/blocking-call contention plus
    unbounded retry amplification — not the event loop itself ceasing to run.
    Both are asserted here, and they are deliberately separate: the contention
    assertion is the reproduction, and the loop-acceptance assertion is a
    control that must hold before AND after the repair.
    """

    SAMPLES = 4
    PER_REQUEST_BUDGET_S = 0.5
    HARD_WAIT_S = 3.0

    @staticmethod
    def _start_storm(manager, probing_client, monkeypatch, tmp_path):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        # Tick continuously. Production ran a 4s interval over four enrolled
        # terminals, which was already enough to keep the observation storm
        # running without a gap once each observation was slow.
        monkeypatch.setattr(fr, "PIPE_LIVENESS_CHECK_INTERVAL_S", 0.0)
        probe = _probe(probing_client)
        for terminal_id in ("7864d54f", "0d618fcf", "c736df05", "b503a93e"):
            manager._pane_probe[terminal_id] = probe
            manager._rearm[terminal_id] = lambda: None
            manager._last_data_at[terminal_id] = time.monotonic()
        manager._ensure_watchdog()

    @pytest.mark.asyncio
    async def test_status_read_survives_a_liveness_probe_storm(
        self, shared_tmux, probing_client, tmp_path, monkeypatch
    ):
        """The sentinel's bounded loopback ``GET /terminals/{id}`` reads one
        pane, exactly like the watchdog does. Pre-fix both go through the same
        server-wide listing, so the request queues behind every in-flight
        liveness observation and then fails on the same malformed row.
        """
        manager = FifoManager()
        self._start_storm(manager, probing_client, monkeypatch, tmp_path)

        def status_read():
            return probing_client.get_history(
                "cao-p1-closure", "p1-closure-supervisor-terra-97d0", tail_lines=80
            )

        try:
            await asyncio.sleep(0.05)  # let the storm reach steady state

            worst = 0.0
            for _ in range(self.SAMPLES):
                started = time.monotonic()
                try:
                    content = await asyncio.wait_for(
                        asyncio.to_thread(status_read), timeout=self.HARD_WAIT_S
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    worst = max(worst, self.HARD_WAIT_S)
                    break
                worst = max(worst, time.monotonic() - started)
                assert content, "a status read must return the pane's live content"
                await asyncio.sleep(0.01)
        finally:
            manager.stop_watchdog()

        assert worst < self.PER_REQUEST_BUDGET_S, (
            f"a concurrent terminal-status read waited {worst:.3f}s behind the "
            f"FIFO liveness watchdog (budget {self.PER_REQUEST_BUDGET_S}s) — "
            "this is COND-0242's control-plane unavailability"
        )

    @pytest.mark.asyncio
    async def test_event_loop_keeps_accepting_work_during_the_storm(
        self, shared_tmux, probing_client, tmp_path, monkeypatch
    ):
        """Control, not reproduction: the watchdog runs on its own thread, so
        ``/health``-class work that touches no tmux resource must keep being
        accepted and served throughout — before and after the repair. Asserting
        it here keeps the repair honest about what actually broke.
        """
        manager = FifoManager()
        self._start_storm(manager, probing_client, monkeypatch, tmp_path)

        try:
            await asyncio.sleep(0.05)
            worst = 0.0
            for _ in range(20):
                started = time.monotonic()
                await asyncio.sleep(0)
                worst = max(worst, time.monotonic() - started)
        finally:
            manager.stop_watchdog()

        assert worst < 0.1, f"event loop scheduling latency reached {worst:.3f}s"


class TestBoundedFailureBehavior:
    """Repeated failure must be bounded backoff plus rate-limited logging, not
    a tight retry with one traceback per tick per terminal.
    """

    def _manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        return FifoManager()

    def test_persistent_probe_failure_backs_off_and_stops_logging_a_storm(
        self, tmp_path, monkeypatch, caplog
    ):
        """Pre-fix: 25 ticks produce 25 probe attempts and 25 full tracebacks.

        The failure is intermittent in production, so the watchdog must keep
        the terminal enrolled and keep retrying — but on a bounded, backing-off
        cadence, and without re-logging the same traceback every time.
        """
        monkeypatch.setattr(fr, "PIPE_LIVENESS_CHECK_INTERVAL_S", 0.0)
        manager = self._manager(tmp_path, monkeypatch)

        attempts = {"n": 0}

        def always_failing_probe():
            attempts["n"] += 1
            raise ValueError(LIBTMUX_PARSE_ERROR)

        manager._pane_probe["term"] = always_failing_probe
        manager._rearm["term"] = lambda: None
        manager._last_data_at["term"] = time.monotonic()

        ticks = 25
        with caplog.at_level(logging.WARNING, logger=fr.logger.name):
            for _ in range(ticks):
                manager._run_liveness_sweep()

        assert attempts["n"] < ticks, (
            f"a persistently failing probe was retried on every one of {ticks} "
            f"ticks ({attempts['n']} attempts) — no backoff bounds the storm"
        )
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) <= 2, (
            f"{len(errors)} error records for one persistently failing probe — "
            "repeated identical failures must be rate-limited, not re-logged "
            "with a full traceback every tick"
        )
        assert "term" in manager._pane_probe, (
            "an intermittent observation failure must not silently un-enroll a "
            "live terminal from the liveness watchdog"
        )

    def test_probe_failure_never_advances_liveness_truth(self, tmp_path, monkeypatch):
        """Conservative under malformed/timeout output: an observation that
        could not be made is not evidence of a stall, must not re-arm, and must
        not overwrite the last known-healthy baseline.
        """
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 1)
        manager = self._manager(tmp_path, monkeypatch)

        state = {"fail": False}
        rearms: list = []

        def flaky_probe():
            if state["fail"]:
                raise ValueError(LIBTMUX_PARSE_ERROR)
            return "healthy baseline"

        manager._pane_probe["term"] = flaky_probe
        manager._rearm["term"] = lambda: rearms.append(True)
        manager._last_data_at["term"] = time.monotonic()

        manager._check_pipe_liveness("term")  # establishes the baseline
        baseline = manager._liveness["term"]

        state["fail"] = True
        for _ in range(3):
            with pytest.raises(ValueError):
                manager._check_pipe_liveness("term")

        assert rearms == [], "an unobservable pane must never be judged stalled"
        assert manager._liveness["term"] == baseline, (
            "a failed observation must leave the last known-healthy baseline "
            "intact — overwriting it would let a parse failure fabricate a "
            "divergence on the next successful check"
        )

    def test_backoff_clears_and_is_reported_once_the_probe_recovers(
        self, tmp_path, monkeypatch, caplog
    ):
        """The production failure was intermittent, so recovery must restore the
        normal cadence rather than leave the terminal parked on a long backoff.
        """
        monkeypatch.setattr(fr, "PIPE_LIVENESS_CHECK_INTERVAL_S", 0.0)
        monkeypatch.setattr(fr, "PIPE_LIVENESS_PROBE_BACKOFF_BASE_S", 0.01)
        manager = self._manager(tmp_path, monkeypatch)

        state = {"fail": True}
        manager._pane_probe["term"] = lambda: (
            (_ for _ in ()).throw(ValueError(LIBTMUX_PARSE_ERROR)) if state["fail"] else "pane tail"
        )
        manager._rearm["term"] = lambda: None
        manager._last_data_at["term"] = time.monotonic()

        manager._run_liveness_sweep()
        assert manager._probe_failures["term"] == 1
        assert manager._probe_retry_at["term"] > time.monotonic()

        state["fail"] = False
        time.sleep(0.02)  # let the (deliberately tiny) backoff expire
        with caplog.at_level(logging.INFO, logger=fr.logger.name):
            manager._run_liveness_sweep()

        assert "term" not in manager._probe_failures, "backoff must clear on recovery"
        assert "term" not in manager._probe_retry_at
        assert any(
            "recovered after" in r.getMessage() for r in caplog.records
        ), "recovery from a bounded observation failure must be reported"

    def test_stop_reader_drops_probe_backoff_state(self, tmp_path, monkeypatch):
        """Backoff bookkeeping is per-terminal state like every other watchdog
        dict: a torn-down terminal must not leave an entry behind, and a
        re-created one must not inherit a stale backoff deadline.
        """
        manager = self._manager(tmp_path, monkeypatch)
        manager._probe_failures["term"] = 3
        manager._probe_retry_at["term"] = time.monotonic() + 999
        manager._probe_logged_at["term"] = time.monotonic()

        manager.stop_reader("term")

        assert "term" not in manager._probe_failures
        assert "term" not in manager._probe_retry_at
        assert "term" not in manager._probe_logged_at


class TestObservationIsBoundedAndConservative:
    """``get_history`` is the observation primitive both the liveness watchdog
    and terminal-status reads go through, so its failure modes are liveness
    truth. None of them may be reported as "the pane was empty"."""

    def test_capture_timeout_raises_rather_than_reporting_an_empty_pane(
        self, probing_client, monkeypatch
    ):
        from cli_agent_orchestrator.clients import tmux as tmux_module

        def timing_out_run(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 10.0))

        monkeypatch.setattr(tmux_module.subprocess, "run", timing_out_run)

        with pytest.raises(TimeoutError):
            probing_client.get_history("ses", "win", tail_lines=80)

    def test_malformed_pane_listing_raises_rather_than_guessing_a_target(
        self, probing_client, monkeypatch
    ):
        from cli_agent_orchestrator.clients import tmux as tmux_module

        def garbled_run(cmd, *args, **kwargs):
            argv = list(cmd)
            if "list-panes" in argv:
                # The shape COND-0242 was made of: a row that does not carry
                # the field it was asked for.
                return subprocess.CompletedProcess(argv, 0, stdout="\t\tnot-a-pane-id\n", stderr="")
            raise AssertionError("capture-pane must not run against an unresolved target")

        monkeypatch.setattr(tmux_module.subprocess, "run", garbled_run)

        with pytest.raises(ValueError, match="no usable pane"):
            probing_client.get_history("ses", "win", tail_lines=80)

    def test_observation_runs_under_a_hard_per_call_bound(self, probing_client, monkeypatch):
        from cli_agent_orchestrator.clients import tmux as tmux_module

        timeouts: list = []

        def recording_run(cmd, *args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            argv = list(cmd)
            out = "%0\n" if "list-panes" in argv else "tail\n"
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        monkeypatch.setattr(tmux_module.subprocess, "run", recording_run)
        probing_client.get_history("ses", "win", tail_lines=80)

        assert timeouts and all(
            t == tmux_module.TMUX_CALL_TIMEOUT_SECONDS for t in timeouts
        ), "every liveness/history tmux call must carry the hard per-call bound"


class TestMissingTerminalWarningStorm:
    """The second-occurrence log burst in the evidence was hundreds of
    ``Terminal metadata not found`` warnings for live terminals, several per
    second, on top of the liveness failure itself. A repeated observation of
    the same absent row carries no new information after the first.
    """

    def test_repeated_missing_terminal_lookups_log_once_per_interval(self, monkeypatch):
        from cli_agent_orchestrator.clients import database as db

        monkeypatch.setattr(db, "_missing_terminal_logged_at", {})
        reported = [db._should_report_missing_terminal("7864d54f") for _ in range(50)]

        assert reported[0] is True, "the first observation must stay loud"
        assert not any(reported[1:]), "repeats within the interval must be suppressed"

    def test_each_terminal_is_reported_on_its_own_first_observation(self, monkeypatch):
        from cli_agent_orchestrator.clients import database as db

        monkeypatch.setattr(db, "_missing_terminal_logged_at", {})

        assert db._should_report_missing_terminal("c736df05") is True
        assert db._should_report_missing_terminal("b503a93e") is True, (
            "rate limiting is per terminal — one noisy id must not silence the "
            "first report of a different one"
        )

    def test_a_terminal_that_comes_back_is_reported_again_if_it_disappears(self, monkeypatch):
        from cli_agent_orchestrator.clients import database as db

        logged: dict = {}
        monkeypatch.setattr(db, "_missing_terminal_logged_at", logged)

        assert db._should_report_missing_terminal("87abc952") is True
        assert db._should_report_missing_terminal("87abc952") is False
        logged.pop("87abc952")  # what a successful lookup does
        assert db._should_report_missing_terminal("87abc952") is True
