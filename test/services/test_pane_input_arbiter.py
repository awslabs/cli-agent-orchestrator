"""Tests for the shared per-pane input arbiter.

The property under test is not "the lock works" but "two writers can
never be inside the pane at once, in this process or any other".  A pane
has no framing, so a test that only proved in-process mutual exclusion
would pass while the deployment that actually runs — server, CLI, and
companion as separate processes against one tmux server — interleaved
freely.
"""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys
import threading
import time

import pytest

from cli_agent_orchestrator.services.pane_input_arbiter import (
    PaneBusyError,
    PaneInputArbiterError,
    PaneLeaseReentryError,
    is_pane_leased,
    pane_input_lease,
    pane_lock_dir,
    reset_pane_input_arbiter,
)

PANE = "%42"
OTHER_PANE = "%43"

# Probes the cross-process lock from a genuinely separate interpreter.
# Prints 'busy' when another process holds it, 'free' otherwise.
_CHILD_PROBE = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.stdout.write("busy")
else:
    sys.stdout.write("free")
"""


@pytest.fixture(autouse=True)
def _clean_arbiter():
    reset_pane_input_arbiter()
    yield
    reset_pane_input_arbiter()


@pytest.fixture
def lock_dir(tmp_path):
    return tmp_path / "pane-input-locks"


def _probe_from_another_process(path):
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_PROBE, str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class TestSerialisation:
    def test_concurrent_writers_never_overlap(self, lock_dir):
        """The whole point: N threads, never two inside the pane at once."""
        threads = 12
        start = threading.Barrier(threads)
        inside = 0
        peak = 0
        bookkeeping = threading.Lock()
        overlaps = []
        entries = []

        def writer(index):
            nonlocal inside, peak
            start.wait(timeout=30)
            with pane_input_lease(PANE, holder=f"w{index}", timeout=30, lock_dir=lock_dir):
                with bookkeeping:
                    inside += 1
                    peak = max(peak, inside)
                    if inside > 1:
                        overlaps.append(index)
                    entries.append(index)
                # Widen the window so a real overlap would be observed
                # rather than missed by luck.
                time.sleep(0.005)
                with bookkeeping:
                    inside -= 1

        workers = [threading.Thread(target=writer, args=(i,)) for i in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
            assert not worker.is_alive()

        assert overlaps == []
        assert peak == 1
        # Every writer got its turn: serialised, not starved.
        assert sorted(entries) == list(range(threads))

    def test_busy_pane_refuses_immediately_by_default(self, lock_dir):
        """timeout=0 refuses rather than queueing; nothing was written."""
        holding = threading.Event()
        release = threading.Event()

        def holder():
            with pane_input_lease(PANE, holder="first", lock_dir=lock_dir):
                holding.set()
                release.wait(timeout=30)

        first = threading.Thread(target=holder)
        first.start()
        try:
            assert holding.wait(timeout=30)
            started = time.monotonic()
            with pytest.raises(PaneBusyError):
                with pane_input_lease(PANE, holder="second", lock_dir=lock_dir):
                    pytest.fail("a second writer must never enter the pane")
            # Refused, not queued: the caller gets an answer it can act on.
            assert time.monotonic() - started < 1.0
        finally:
            release.set()
            first.join(timeout=30)

    def test_bounded_wait_acquires_after_release(self, lock_dir):
        """Waiting is available, but must be asked for explicitly."""
        holding = threading.Event()
        released = threading.Event()
        acquired = threading.Event()

        def holder():
            with pane_input_lease(PANE, holder="holder", lock_dir=lock_dir):
                holding.set()
                released.wait(timeout=30)

        def waiter():
            with pane_input_lease(PANE, holder="waiter", timeout=30, lock_dir=lock_dir):
                acquired.set()

        first = threading.Thread(target=holder)
        first.start()
        assert holding.wait(timeout=30)
        second = threading.Thread(target=waiter)
        second.start()
        # The waiter must not get in while the holder is inside.
        assert not acquired.wait(timeout=0.2)
        released.set()
        assert acquired.wait(timeout=30)
        first.join(timeout=30)
        second.join(timeout=30)

    def test_different_panes_do_not_block_each_other(self, lock_dir):
        """Exclusion is per pane; a global lock would serialise the fleet."""
        with pane_input_lease(PANE, holder="a", lock_dir=lock_dir):
            with pane_input_lease(OTHER_PANE, holder="b", lock_dir=lock_dir) as lease:
                assert lease.pane_id == OTHER_PANE


class TestCrossProcess:
    def test_lease_excludes_a_separate_process(self, lock_dir):
        """The deployment runs server, CLI, and companion as separate processes."""
        with pane_input_lease(PANE, holder="server", lock_dir=lock_dir):
            path = lock_dir / "pane-42.lock"
            assert _probe_from_another_process(path) == "busy"

    def test_lock_is_free_to_other_processes_after_release(self, lock_dir):
        with pane_input_lease(PANE, holder="server", lock_dir=lock_dir):
            pass
        assert _probe_from_another_process(lock_dir / "pane-42.lock") == "free"

    def test_another_process_holding_the_lock_refuses_this_one(self, lock_dir):
        """A foreign holder must produce PaneBusyError, not a silent pass."""
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = lock_dir / "pane-42.lock"
        # A second file descriptor is an independent open file
        # description, which is exactly what flock arbitrates between —
        # the same relationship a separate process has.
        foreign = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(foreign, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(PaneBusyError):
                with pane_input_lease(PANE, holder="server", lock_dir=lock_dir):
                    pytest.fail("the in-process lock must not mask a foreign holder")
        finally:
            fcntl.flock(foreign, fcntl.LOCK_UN)
            os.close(foreign)

    def test_in_process_lock_is_released_when_the_flock_refuses(self, lock_dir):
        """A failed acquire must not leave the pane permanently unusable."""
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = lock_dir / "pane-42.lock"
        foreign = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(foreign, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(PaneBusyError):
                with pane_input_lease(PANE, holder="server", lock_dir=lock_dir):
                    pass
        finally:
            fcntl.flock(foreign, fcntl.LOCK_UN)
            os.close(foreign)
        assert not is_pane_leased(PANE)
        with pane_input_lease(PANE, holder="server", lock_dir=lock_dir) as lease:
            assert lease.pane_id == PANE


class TestLeaseLifetime:
    def test_lease_is_released_when_the_body_raises(self, lock_dir):
        """A failed write must not strand the pane."""

        class WriteFailed(RuntimeError):
            pass

        with pytest.raises(WriteFailed):
            with pane_input_lease(PANE, holder="a", lock_dir=lock_dir):
                raise WriteFailed()
        assert not is_pane_leased(PANE)
        with pane_input_lease(PANE, holder="b", lock_dir=lock_dir) as lease:
            assert lease.holder == "b"

    def test_reentry_is_refused_rather_than_deadlocking(self, lock_dir):
        """A nested write is a second writer as far as the pane is concerned."""
        with pane_input_lease(PANE, holder="outer", lock_dir=lock_dir):
            with pytest.raises(PaneLeaseReentryError):
                with pane_input_lease(PANE, holder="inner", timeout=30, lock_dir=lock_dir):
                    pytest.fail("reentry must never be granted")

    def test_is_pane_leased_tracks_the_block(self, lock_dir):
        assert not is_pane_leased(PANE)
        with pane_input_lease(PANE, holder="a", lock_dir=lock_dir):
            assert is_pane_leased(PANE)
        assert not is_pane_leased(PANE)

    def test_lease_reports_pane_holder_and_time(self, lock_dir):
        with pane_input_lease(PANE, holder="control-input", lock_dir=lock_dir) as lease:
            assert lease.pane_id == PANE
            assert lease.holder == "control-input"
            assert lease.acquired_at.endswith("Z")


class TestRejections:
    @pytest.mark.parametrize(
        "pane_id",
        ["", "%", "0", "42", "%4a", "%-1", "@1", "%1:2", "%1.0", "-t", None, 42],
    )
    def test_invalid_pane_ids_are_refused(self, pane_id, lock_dir):
        """A pane id the writer would reject must not be lockable here."""
        with pytest.raises(ValueError):
            with pane_input_lease(pane_id, holder="a", lock_dir=lock_dir):
                pytest.fail("an invalid pane id must never yield a lease")

    def test_holder_label_is_required(self, lock_dir):
        with pytest.raises(ValueError):
            with pane_input_lease(PANE, holder="", lock_dir=lock_dir):
                pytest.fail("an anonymous holder is undiagnosable")

    def test_negative_timeout_is_refused(self, lock_dir):
        with pytest.raises(ValueError):
            with pane_input_lease(PANE, holder="a", timeout=-1, lock_dir=lock_dir):
                pytest.fail("a negative timeout has no meaning")

    def test_unusable_lock_directory_is_not_reported_as_busy(self, tmp_path):
        """A broken lock path must not invite an endless 'busy' retry."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("occupied")
        with pytest.raises((PaneInputArbiterError, OSError)) as excinfo:
            with pane_input_lease(PANE, holder="a", lock_dir=blocker):
                pytest.fail("no lease is possible without a lock file")
        assert not isinstance(excinfo.value, PaneBusyError)
        assert not is_pane_leased(PANE)


class TestLockFiles:
    def test_lock_file_and_directory_are_owner_only(self, lock_dir):
        with pane_input_lease(PANE, holder="a", lock_dir=lock_dir):
            pass
        assert stat.S_IMODE(os.stat(lock_dir).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(lock_dir / "pane-42.lock").st_mode) == 0o600

    def test_lock_file_name_is_derived_from_the_pane_id(self, lock_dir):
        with pane_input_lease(OTHER_PANE, holder="a", lock_dir=lock_dir):
            pass
        assert (lock_dir / "pane-43.lock").exists()
        assert not (lock_dir / "pane-42.lock").exists()

    def test_lock_dir_follows_the_state_root(self, monkeypatch, tmp_path):
        """Resolved at call time so an isolated state root takes effect."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.CAO_HOME_DIR", str(tmp_path / "state")
        )
        assert pane_lock_dir() == tmp_path / "state" / "pane-input-locks"
