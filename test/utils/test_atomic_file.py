"""Tests for the shared locked-atomic-rewrite helper (caom-47e).

Covers the two defects reported against the memory plugins'
read-modify-write + "atomic" replace idiom:

* Defect A (shared temp filename): two concurrent unlocked writers using a
  FIXED temp path can interleave and one can unlink the other's live temp
  file, surfacing as FileNotFoundError on os.replace.
* Defect B (lost update): two concurrent writers doing read -> modify ->
  replace without a lock can both read the same base content; the second
  replace silently discards the first writer's change.

``locked_atomic_rewrite`` is exercised directly here (not through a
specific plugin) so the concurrency guarantees are pinned independently of
any one call site.
"""

from __future__ import annotations

import multiprocessing
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from cli_agent_orchestrator.constants import LOCK_DIR
from cli_agent_orchestrator.utils.atomic_file import (
    LockTimeoutError,
    _file_lock,
    _lock_path_for,
    locked_atomic_rewrite,
)


def test_creates_file_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"

    locked_atomic_rewrite(target, lambda existing: existing + "hello")

    assert target.read_text(encoding="utf-8") == "hello"


def test_rewrite_sees_existing_content(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("base\n", encoding="utf-8")

    locked_atomic_rewrite(target, lambda existing: existing + "more\n")

    assert target.read_text(encoding="utf-8") == "base\nmore\n"


def test_no_tmp_or_partial_files_left_behind(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"

    locked_atomic_rewrite(target, lambda existing: "content")

    # The lock file now lives under LOCK_DIR, not beside the target, so the
    # target's directory must contain ONLY the target afterward — no .tmp and
    # no .lock.
    leftovers = [p for p in tmp_path.iterdir() if p.name != target.name]
    assert leftovers == []


def test_no_lock_file_pollutes_target_directory(tmp_path: Path) -> None:
    """Regression test for the PR #492 review (Stan Fan): the lock file must
    NOT land beside the (user-authored) target. After a write, the target's
    parent directory must contain only the target itself — zero new files, in
    particular no ``AGENTS.md.lock`` or any other ``*.lock``.
    """
    target = tmp_path / "AGENTS.md"

    locked_atomic_rewrite(target, lambda existing: "content")

    entries = sorted(p.name for p in tmp_path.iterdir())
    assert entries == [target.name], f"unexpected files beside target: {entries}"
    assert not (target.with_name(target.name + ".lock")).exists()
    assert list(tmp_path.glob("*.lock")) == []

    # And the lock file that WAS used lives under the CAO-owned LOCK_DIR, keyed
    # by the target's resolved absolute path.
    lock_path = _lock_path_for(target)
    assert LOCK_DIR in lock_path.parents
    assert lock_path.exists(), "lock file should be created (and kept) under LOCK_DIR"


def test_lock_path_stable_and_distinct(tmp_path: Path) -> None:
    """The lock key must be stable for a given target (even before it exists)
    and distinct for different targets — the cross-process agreement contract.
    """
    target = tmp_path / "sub" / "AGENTS.md"

    # Non-existent target still yields a stable key (resolve(strict=False)).
    assert not target.exists()
    first = _lock_path_for(target)
    second = _lock_path_for(target)
    assert first == second

    # A logically identical but un-normalized path maps to the SAME lock.
    equivalent = tmp_path / "sub" / "." / "AGENTS.md"
    assert _lock_path_for(equivalent) == first

    # A different target must not collide.
    other = tmp_path / "sub" / "CLAUDE.md"
    assert _lock_path_for(other) != first


def test_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "CLAUDE.md"

    locked_atomic_rewrite(target, lambda existing: "content")

    assert target.read_text(encoding="utf-8") == "content"


# ---------------------------------------------------------------------------
# File-mode preservation (regression for PR #492 finding 1: mkstemp creates the
# temp at 0600, so os.replace was silently downgrading user-authored files).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_preserves_existing_target_mode(tmp_path: Path) -> None:
    """A rewrite of an existing 0644 file must keep it 0644, not downgrade it
    to mkstemp's 0600 (the regression a maintainer verified on PR #492)."""
    target = tmp_path / "AGENTS.md"
    target.write_text("base\n", encoding="utf-8")
    os.chmod(target, 0o644)

    locked_atomic_rewrite(target, lambda existing: existing + "more\n")

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644
    assert target.read_text(encoding="utf-8") == "base\nmore\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_preserves_non_default_existing_target_mode(tmp_path: Path) -> None:
    """Whatever mode the existing target has is preserved verbatim (not just
    0644): prove the helper reads the real mode rather than hard-coding one."""
    target = tmp_path / "AGENTS.md"
    target.write_text("base\n", encoding="utf-8")
    os.chmod(target, 0o640)

    locked_atomic_rewrite(target, lambda existing: "replaced\n")

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_new_file_uses_umask_default_not_hardcoded_0600(tmp_path: Path) -> None:
    """On first write (no existing target) the published file must get a
    sane umask-respecting mode (0o666 & ~umask), NOT mkstemp's hard 0600."""
    target = tmp_path / "AGENTS.md"
    assert not target.exists()

    old_umask = os.umask(0o022)
    try:
        locked_atomic_rewrite(target, lambda existing: "content")
    finally:
        os.umask(old_umask)

    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == (0o666 & ~0o022)  # 0o644 under the standard umask
    assert mode != 0o600, "new file must not inherit mkstemp's 0600"


def test_own_temp_file_cleaned_up_when_compute_raises(tmp_path: Path) -> None:
    """If compute_new_content raises, no temp file should survive and the
    target must be untouched (mirrors the finally-unlink contract)."""
    target = tmp_path / "AGENTS.md"
    target.write_text("original", encoding="utf-8")

    def boom(existing: str) -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        locked_atomic_rewrite(target, boom)

    assert target.read_text(encoding="utf-8") == "original"
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Defect B — lost update. Two-writer barrier test.
# ---------------------------------------------------------------------------


def test_two_writer_barrier_both_updates_survive(tmp_path: Path) -> None:
    """Two threads race to append their own marker block to the same file.

    Both threads are released simultaneously via a threading.Barrier right
    before calling ``locked_atomic_rewrite``, so they arrive at the lock at
    (as close to) the same instant as the GIL allows — the same "both ready
    to read/write together" scenario that produced the lost-update bug when
    the read-modify-write cycle ran unlocked. Because the lock serializes
    the whole cycle, whichever thread goes second is forced to read the
    FIRST thread's already-published content rather than the shared
    pre-race base, so both blocks must survive in the final file and
    neither call may raise.
    """
    target = tmp_path / "AGENTS.md"
    target.write_text("# Notes\n", encoding="utf-8")

    start_barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def append_block(marker: str) -> None:
        start_barrier.wait(timeout=5)
        try:
            locked_atomic_rewrite(
                target,
                lambda existing, m=marker: existing.rstrip("\n") + f"\n<{m}>block</{m}>\n",
            )
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion
            errors.append(exc)

    t1 = threading.Thread(target=append_block, args=("agent-a",))
    t2 = threading.Thread(target=append_block, args=("agent-b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert errors == [], f"writers must not raise: {errors}"
    final = target.read_text(encoding="utf-8")
    assert "<agent-a>block</agent-a>" in final, "first writer's block was lost"
    assert "<agent-b>block</agent-b>" in final, "second writer's block was lost"
    # Base content must survive too.
    assert "# Notes" in final


def test_two_writer_barrier_forces_same_base_read_before_racing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the lock genuinely serializes rather than the threads simply
    never overlapping by chance. A barrier release-gate on ``read_text``
    forces both threads' first lock-holder to block waiting for the SECOND
    thread to reach the gate before either is allowed to proceed past its
    own read — guaranteeing true overlap in time between the two attempts —
    and asserts the second one still lands correctly (serialized, not
    dropped) once released.
    """
    target = tmp_path / "AGENTS.md"
    target.write_text("base\n", encoding="utf-8")

    arrived = threading.Barrier(2, timeout=5)
    real_read_text = Path.read_text
    call_count = {"n": 0}
    call_lock = threading.Lock()

    def gated_read_text(self: Path, *args, **kwargs):  # noqa: ANN001
        if self == target:
            with call_lock:
                call_count["n"] += 1
                is_first_reader = call_count["n"] == 1
            if is_first_reader:
                # The first thread to win the lock and reach the read still
                # waits here until the second thread has started trying to
                # acquire the (currently held) lock, proving real contention
                # existed rather than accidental sequential execution.
                time.sleep(0.2)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", gated_read_text)

    errors: list[BaseException] = []

    def append_block(marker: str) -> None:
        arrived.wait(timeout=5)
        try:
            locked_atomic_rewrite(
                target, lambda existing, m=marker: existing.rstrip("\n") + f"\n{m}\n"
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=append_block, args=("MARK-A",))
    t2 = threading.Thread(target=append_block, args=("MARK-B",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert errors == [], f"writers must not raise: {errors}"
    assert call_count["n"] == 2
    final = target.read_text(encoding="utf-8")
    assert "MARK-A" in final
    assert "MARK-B" in final


# ---------------------------------------------------------------------------
# Defect A — shared/fixed temp filename collision shape.
# ---------------------------------------------------------------------------


def test_temp_files_are_unique_per_call_not_fixed_name(tmp_path: Path) -> None:
    """Regression guard for defect A: the old idiom used a single fixed
    ``target + ".tmp"`` path. Assert the helper's temp files are unique by
    capturing the names actually created via tempfile.mkstemp for two
    sequential calls and confirming they differ (never previously the same
    path 'AGENTS.md.tmp' the way the old buggy code hard-coded it).
    """
    target = tmp_path / "AGENTS.md"
    seen_names: list[str] = []

    import cli_agent_orchestrator.utils.atomic_file as atomic_file_module

    real_mkstemp = atomic_file_module.tempfile.mkstemp

    def spying_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen_names.append(name)
        return fd, name

    atomic_file_module.tempfile.mkstemp = spying_mkstemp
    try:
        locked_atomic_rewrite(target, lambda existing: "one")
        locked_atomic_rewrite(target, lambda existing: "two")
    finally:
        atomic_file_module.tempfile.mkstemp = real_mkstemp

    assert len(seen_names) == 2
    assert seen_names[0] != seen_names[1], "temp file names must not collide across calls"
    fixed_legacy_path = str(target.with_suffix(target.suffix + ".tmp"))
    assert fixed_legacy_path not in seen_names, "must not reuse the old fixed .tmp name"


def test_concurrent_writers_never_produce_filenotfound_on_replace(tmp_path: Path) -> None:
    """End-to-end shape of defect A: run many concurrent writers against the
    same target and assert none of them ever raise FileNotFoundError (the
    signature of one writer's finally-unlink deleting another's live temp
    file under the old fixed-name idiom).
    """
    target = tmp_path / "AGENTS.md"
    target.write_text("base\n", encoding="utf-8")

    errors: list[BaseException] = []
    lock = threading.Lock()

    def writer(i: int) -> None:
        try:
            locked_atomic_rewrite(target, lambda existing, i=i: existing + f"\nwriter-{i}")
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    file_not_found = [e for e in errors if isinstance(e, FileNotFoundError)]
    assert file_not_found == [], f"defect A shape reproduced: {file_not_found}"
    assert errors == [], f"no writer should raise at all: {errors}"


# ---------------------------------------------------------------------------
# Inter-process lock semantics (single-process coverage of flock behavior).
# ---------------------------------------------------------------------------


def _hold_lock_in_subprocess(
    target_str: str, hold_seconds: float, ready_event
) -> None:  # noqa: ANN001
    """Helper run in a child process: acquire the flock and hold it.

    Crucially, the child computes the lock path ITSELF from the target via
    ``_lock_path_for`` rather than receiving a precomputed path — so a green
    test proves two independent processes derive the SAME lock file for the
    same target (the cross-process correctness invariant).
    """
    import fcntl

    from cli_agent_orchestrator.utils.atomic_file import _lock_path_for

    lock_path = _lock_path_for(Path(target_str))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    ready_event.set()
    time.sleep(hold_seconds)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


@pytest.mark.skipif(os.name != "posix", reason="fcntl.flock is POSIX-only")
def test_second_writer_blocked_by_subprocess_holding_lock(tmp_path: Path) -> None:
    """Multi-process coverage of the lock: a real OS subprocess acquires and
    holds the CAO-owned lockfile via flock (the same mechanism
    locked_atomic_rewrite uses), and a second, in-process attempt to acquire
    the SAME lock must either block until released or time out clearly —
    never silently proceed. This is the process-level guarantee threading
    locks alone cannot provide (CLI process vs cao-server process).

    Both the child (via ``_hold_lock_in_subprocess``) and this process derive
    the lock path from the target with ``_lock_path_for``, so the test also
    proves two independent processes agree on the same relocated lock file.
    """
    target = tmp_path / "AGENTS.md"
    lock_path = _lock_path_for(target)

    ctx = multiprocessing.get_context("spawn")
    ready_event = ctx.Event()
    hold_seconds = 1.5
    proc = ctx.Process(
        target=_hold_lock_in_subprocess,
        args=(str(target), hold_seconds, ready_event),
    )
    proc.start()
    try:
        assert ready_event.wait(timeout=5), "subprocess never acquired the lock"

        # 1) A short timeout must fail clearly while the subprocess holds it.
        start = time.monotonic()
        with pytest.raises(LockTimeoutError):
            with _file_lock(lock_path, timeout=0.3):
                pass
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, "timeout path must not block far past the requested timeout"

        # 2) A longer timeout must succeed once the subprocess releases it.
        with _file_lock(lock_path, timeout=hold_seconds + 5):
            pass
    finally:
        proc.join(timeout=10)
        assert proc.exitcode == 0


def test_lock_timeout_raises_clear_error_not_indefinite_block(tmp_path: Path) -> None:
    """A same-process contention check: holding the lock in one thread and
    attempting a short-timeout acquire from another must raise
    LockTimeoutError rather than hang forever.
    """
    target = tmp_path / "AGENTS.md"
    lock_path = _lock_path_for(target)

    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold() -> None:
        with _file_lock(lock_path, timeout=5):
            holder_ready.set()
            release_holder.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holder_ready.wait(timeout=5)
        with pytest.raises(LockTimeoutError):
            with _file_lock(lock_path, timeout=0.3):
                pass
    finally:
        release_holder.set()
        t.join(timeout=5)


def test_locked_atomic_rewrite_propagates_lock_timeout(tmp_path: Path) -> None:
    """locked_atomic_rewrite itself must surface LockTimeoutError (not hang)
    when it cannot acquire the lock within the given budget.
    """
    target = tmp_path / "AGENTS.md"
    lock_path = _lock_path_for(target)

    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold() -> None:
        with _file_lock(lock_path, timeout=5):
            holder_ready.set()
            release_holder.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holder_ready.wait(timeout=5)
        with pytest.raises(LockTimeoutError):
            locked_atomic_rewrite(target, lambda existing: "x", lock_timeout=0.3)
    finally:
        release_holder.set()
        t.join(timeout=5)


def test_reentrant_same_target_raises_timeout_not_deadlock(tmp_path: Path) -> None:
    """Regression test for self-deadlock: a compute_fn that calls
    locked_atomic_rewrite on the SAME target file must raise
    LockTimeoutError rather than hang forever, and the outer target file must
    remain uncorrupted and the lock must be cleanly released afterward.

    This pins the exact failure mode that caused a rival implementation (lock
    held via a try/finally around the whole cycle with no timeout on
    re-entry) to be rejected: if compute_fn tries to re-enter the lock while
    it's held by the same thread, the inner call must fail fast rather than
    deadlock.
    """
    target = tmp_path / "AGENTS.md"
    target.write_text("base\n", encoding="utf-8")

    def reentrant_compute(existing: str) -> str:
        # Attempt to acquire the same lock that is currently held by the
        # outer locked_atomic_rewrite call. This must timeout, not hang.
        locked_atomic_rewrite(target, lambda _: "inner", lock_timeout=0.5)
        return existing + "should-not-reach"

    # The outer call must raise LockTimeoutError propagated from the inner call.
    with pytest.raises(LockTimeoutError):
        locked_atomic_rewrite(target, reentrant_compute, lock_timeout=0.5)

    # The target file must be untouched by the failed attempt.
    assert target.read_text(encoding="utf-8") == "base\n"

    # No leftover .tmp files from the failed inner write.
    leftovers = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == []

    # The lock must be cleanly released: a normal write to the same target
    # immediately afterward must succeed (proves no leaked lock).
    locked_atomic_rewrite(target, lambda existing: existing + "next\n")
    assert target.read_text(encoding="utf-8") == "base\nnext\n"
