"""One active cao-server owner, enforced rather than assumed (#745).

`replicas: 1` does not prevent two servers: a rolling update overlaps pods by
design, and a manual replacement can race the outgoing process's shutdown.
These tests pin the guard that makes the second server refuse instead of
quietly becoming a second writer to the same SQLite state.

Every test that needs a competing owner uses a real second OS process. flock is
a property of the open file description, so an in-process second open() would
also be refused - and would prove nothing about the case that actually happens
in a cluster.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from cli_agent_orchestrator.services import server_owner
from cli_agent_orchestrator.services.server_owner import (
    ServerOwnershipError,
    acquire_server_ownership,
    ownership_enforced,
    release_server_ownership,
)

# Acquire in a child process, announce it on stdout, then block until killed or
# until stdin closes. Announcing AFTER the lock is held is what makes the
# parent's assertions free of a sleep-and-hope race.
_HOLDER = textwrap.dedent("""
    import sys
    from pathlib import Path
    from cli_agent_orchestrator.services import server_owner
    server_owner.OWNER_LOCK_PATH = Path(sys.argv[1])
    server_owner.acquire_server_ownership()
    print("HELD", flush=True)
    sys.stdin.read()
    """)


@pytest.fixture()
def lock_path(tmp_path, monkeypatch):
    """A private lock file, with this process's lock state reset around it."""
    path = tmp_path / "server-owner.lock"
    monkeypatch.setattr(server_owner, "OWNER_LOCK_PATH", path)
    monkeypatch.setattr(server_owner, "_locks", {})
    yield path
    for held in list(server_owner._locks):
        while server_owner.is_held(held):
            release_server_ownership(held)


@pytest.fixture()
def holder_at():
    """Start a live second server process owning an arbitrary lock path."""
    procs = []

    def start(path):
        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        procs.append(proc)
        assert proc.stdout.readline().strip() == "HELD", "child never took the lock"
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


@pytest.fixture()
def holder(lock_path, holder_at):
    """A live second server process owning ``lock_path``."""
    return lambda: holder_at(lock_path)


class TestSecondServerRefused:
    def test_another_process_holding_the_lock_is_refused(self, lock_path, holder):
        holder()
        with pytest.raises(ServerOwnershipError) as excinfo:
            acquire_server_ownership()
        # Refusal has to be actionable: an operator mid-rollout needs to know
        # which process to stop, not just that "something" holds a file.
        assert str(lock_path.parent) in str(excinfo.value)
        assert "pid" in excinfo.value.holder

    def test_the_holder_records_a_readable_identity(self, lock_path, holder):
        proc = holder()
        recorded = json.loads(lock_path.read_text())
        assert recorded["pid"] == proc.pid
        assert recorded["host"] and recorded["started_at"]

    def test_the_server_refuses_to_start_before_touching_its_state(
        self, lock_path, holder, monkeypatch
    ):
        """The guard runs ahead of init_db, not alongside it.

        Ordering is the whole point: a server that opened the database and then
        discovered it was not the owner has already written to state it does
        not own. ``init_db`` is replaced with a tripwire, so if ownership were
        checked later (or not at all) this fails loudly instead of passing.
        """
        monkeypatch.setenv("CAO_RUNTIME_TOKEN", "test-runtime-token")
        holder()

        from cli_agent_orchestrator.api import main as api_main

        def _must_not_run():
            raise AssertionError("init_db ran despite another server owning the state")

        monkeypatch.setattr(api_main, "init_db", _must_not_run)

        from fastapi.testclient import TestClient

        with pytest.raises(ServerOwnershipError):
            with TestClient(api_main.app):
                pass


class TestOwnershipHandover:
    def test_a_killed_owner_leaves_no_stale_lock(self, lock_path, holder):
        """SIGKILL, the case a pidfile or a lease row gets wrong.

        The kernel drops flock when the holding process dies, so the
        replacement server starts immediately with nothing to expire or clear
        by hand.
        """
        proc = holder()
        with pytest.raises(ServerOwnershipError):
            acquire_server_ownership()

        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)

        deadline = time.monotonic() + 10
        while True:
            try:
                assert acquire_server_ownership() is True
                break
            except ServerOwnershipError:
                # The lock is freed by process teardown, which is not
                # synchronous with wait() returning on every platform.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def test_release_frees_it_for_the_next_owner(self, lock_path, holder):
        assert acquire_server_ownership() is True
        release_server_ownership()
        # A second process can now take it: an orderly shutdown does not make
        # the incoming server wait for the outgoing one to be reaped.
        holder()

    def test_reentry_in_one_process_is_reference_counted(self, lock_path):
        """Two lifespans in one process must not deadlock against each other.

        The test suite and any embedded server do exactly this. flock is keyed
        on the open file description, so a naive second open() in the same
        process would block on itself.
        """
        assert acquire_server_ownership() is True
        assert acquire_server_ownership() is True
        assert server_owner.holders_for(lock_path) == 2

        release_server_ownership()
        assert server_owner.is_held(lock_path), "still held while one holder remains"
        release_server_ownership()
        assert not server_owner.is_held(lock_path)

    def test_release_without_a_lock_is_a_noop(self, lock_path):
        release_server_ownership()  # must not raise


class TestReentryIsPerStateDirectory:
    """Re-entry is only re-entry for the directory already held (#802 review).

    A single refcounted slot answered True to *any* second ``lock_path`` while it
    held one, so an embedded or test server started for a different
    ``CAO_HOME_DIR`` believed it owned that directory without ever locking it -
    leaving the second state free for another process to own simultaneously.
    That is the failure this module exists to prevent, arriving through the
    mechanism meant to make the safe case work.
    """

    @pytest.fixture()
    def second_lock_path(self, tmp_path):
        path = tmp_path / "other-home" / "server-owner.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_a_second_directory_is_actually_locked_not_counted(self, lock_path, second_lock_path):
        acquire_server_ownership(lock_path)

        assert acquire_server_ownership(second_lock_path) is True

        # The proof is on disk, not in the refcount: the old code returned True
        # here having opened nothing.
        assert second_lock_path.exists()
        assert server_owner.holders_for(lock_path) == 1
        assert server_owner.holders_for(second_lock_path) == 1

    def test_another_process_cannot_also_own_the_second_directory(
        self, lock_path, second_lock_path, holder_at
    ):
        acquire_server_ownership(lock_path)
        acquire_server_ownership(second_lock_path)

        # The whole point: a real flock is held, so a genuinely separate process
        # is refused. Under the shared-slot refcount this child started happily.
        with pytest.raises(AssertionError, match="never took the lock"):
            holder_at(second_lock_path)

    def test_releasing_one_directory_leaves_the_other_held(self, lock_path, second_lock_path):
        acquire_server_ownership(lock_path)
        acquire_server_ownership(second_lock_path)

        release_server_ownership(second_lock_path)

        assert not server_owner.is_held(second_lock_path)
        assert server_owner.is_held(lock_path), "releasing one state must not free another"

    def test_the_same_directory_by_another_name_is_still_re_entry(self, lock_path):
        """Paths are compared resolved, so ``./x`` and ``x`` are one claim.

        Treating them as two would make this process flock a file it already
        holds and deadlock on itself - the original reason re-entry exists.
        """
        acquire_server_ownership(lock_path)

        aliased = lock_path.parent / "." / lock_path.name
        assert acquire_server_ownership(aliased) is True

        assert server_owner.holders_for(lock_path) == 2


class TestOptOut:
    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
    def test_opt_out_values_disable_enforcement(self, monkeypatch, value):
        monkeypatch.setenv("CAO_SERVER_OWNER_LOCK", value)
        assert ownership_enforced() is False

    @pytest.mark.parametrize("value", ["1", "true", "", "anything"])
    def test_anything_else_keeps_enforcement_on(self, monkeypatch, value):
        monkeypatch.setenv("CAO_SERVER_OWNER_LOCK", value)
        assert ownership_enforced() is True

    def test_default_is_enforced(self, monkeypatch):
        monkeypatch.delenv("CAO_SERVER_OWNER_LOCK", raising=False)
        assert ownership_enforced() is True

    def test_disabled_acquire_does_not_take_or_refuse_the_lock(
        self, lock_path, holder, monkeypatch
    ):
        holder()
        monkeypatch.setenv("CAO_SERVER_OWNER_LOCK", "0")
        # Explicitly opted out, so no refusal - and no lock either, which is
        # the footgun the warning in the source describes.
        assert acquire_server_ownership() is False
        assert not server_owner.is_held(lock_path)


class TestLockLocation:
    def test_the_lock_lives_with_the_state_it_guards(self, shipped_owner_lock_path):
        from cli_agent_orchestrator.constants import DB_DIR

        # Next to the database, not in /tmp: the guard must travel with the
        # PVC, or two pods mounting the same volume would each get their own.
        # Asserted against the shipped default rather than the live attribute,
        # which the suite redirects so it never contends with a real server.
        assert shipped_owner_lock_path.parent == DB_DIR

    def test_the_lock_file_is_not_world_readable(self, lock_path):
        acquire_server_ownership()
        assert lock_path.exists()
        assert os.stat(lock_path).st_mode & 0o077 == 0
