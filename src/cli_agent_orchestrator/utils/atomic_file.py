"""Shared helpers for inter-process-safe atomic writes to a file.

Two entry points share one set of guarantees:

* :func:`locked_atomic_rewrite` — read the existing content, compute the new
  content from it, publish atomically. For genuine read-modify-write callers.
* :func:`locked_atomic_write` — publish new content without reading the old.
  For full replaces, where the read is pointless and is the only step that can
  fail on a target holding undecodable bytes.

Several call sites (the Codex/Claude Code memory-injection plugins,
``utils/skill_injection.py``) implement the same "read existing content,
compute new content, replace atomically" idiom against a shared target file
that can be written by more than one **process** — a CAO CLI command and
``cao-server`` can both touch the same ``AGENTS.md`` / ``CLAUDE.md`` /
``.agent.md``, and multiple ``cao-server`` worker terminals can trigger the
hook concurrently for the same working directory.

Two independent bugs show up when the read-modify-write + "atomic" replace
is done without any lock:

* **Shared temp filename** — ``target.with_suffix(target.suffix + ".tmp")``
  is a fixed path. Two concurrent writers interleave on the *same* temp
  file: one writer's ``os.replace`` can publish the other's half-written
  content, and the ``finally`` unlink can delete the other writer's live
  temp file out from under it (``FileNotFoundError`` on its own replace).
* **Lost update** — even with unique temp files, two writers can both read
  the same base content, compute their own new content, and replace in
  turn. The second replace wins; the first writer's change is silently
  lost even though both report success.

``locked_atomic_rewrite`` fixes both: it holds an inter-process
``fcntl.flock`` (advisory, ``LOCK_EX``) on a lockfile from before the read
until after ``os.replace``, and writes through a temp file that is unique
per-call (so concurrent *unlocked* readers/writers elsewhere never collide)
and lives in the same directory as the target (so the final ``os.replace``
stays on one filesystem and atomic).

Unlike the ``memory_service.py`` / ``wiki_healer.py`` idiom this was modelled
on — whose targets live under ``CAO_HOME_DIR`` (CAO's own space), so a sidecar
``target + ".lock"`` never pollutes anything — this helper's targets are
USER-AUTHORED files in the user's working tree (``AGENTS.md``,
``.claude/CLAUDE.md``, ``dev.agent.md``). A lock file placed beside each target
would leave permanent untracked ``*.lock`` files in the user's repo. So the
lock file is instead kept in a CAO-owned scratch directory (``LOCK_DIR``,
under ``CAO_HOME_DIR``), keyed by a hash of the target's resolved absolute
path. This keeps the cross-process serialization guarantee intact — every
process that locks the same target computes the same lock path — while adding
ZERO new files to the user's tree.

A threading/asyncio lock alone would not be sufficient here: the CAO CLI
and ``cao-server`` are separate OS processes, and ``fcntl.flock`` is the
only primitive in this codebase's Unix-only lock idiom (see
``services/memory_service.py``, ``services/wiki_healer.py``,
``services/memory_reconciliation.py``) that is honoured across processes.

Unix-only. ``fcntl`` is imported at module load; on a platform without it
(Windows) the lock degrades to a no-op best-effort context manager rather
than raising ImportError at import time — the write itself still happens,
just without the inter-process guarantee. CAO is developed for macOS/Linux
(README lists tmux as a hard requirement, which is itself Unix-oriented),
so this is a pragmatic fallback, not the primary supported path.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterator

from cli_agent_orchestrator.constants import LOCK_DIR

try:
    import fcntl

    _FCNTL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on non-Unix platforms
    fcntl = None  # type: ignore[assignment]
    _FCNTL_AVAILABLE = False

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_INTERVAL_SECONDS = 0.05


def _lock_path_for(target: Path) -> Path:
    """Return the CAO-owned lock file path for ``target``.

    The lock lives under ``LOCK_DIR`` (CAO's scratch space), NOT beside the
    target, so locking a user-tree file adds no files to the user's repo. The
    lock filename is a hash of the target's *resolved absolute* path, which is
    what makes the scheme correct across processes: any process (CLI or
    cao-server) that locks the same target computes the same key, and two
    distinct targets cannot collide.

    ``Path.resolve(strict=False)`` normalizes the path (absolute, symlinks and
    ``..`` resolved) even when the target does not exist yet — the common case
    here, since these files are often created on first write — so the key is
    stable regardless of whether the target exists at lock time.
    """
    resolved = str(target.resolve(strict=False))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return LOCK_DIR / f"{digest}.lock"


class LockTimeoutError(TimeoutError):
    """Raised when the inter-process lock cannot be acquired in time."""

    def __init__(self, lock_path: Path, timeout: float) -> None:
        self.lock_path = lock_path
        self.timeout = timeout
        super().__init__(
            f"timed out after {timeout}s waiting for lock {lock_path} "
            "(another process is holding it)"
        )


@contextlib.contextmanager
def _file_lock(lock_path: Path, timeout: float) -> Iterator[None]:
    """Hold an exclusive, inter-process ``fcntl.flock`` on ``lock_path``.

    Blocks (polling, so a timeout can be enforced) until the lock is
    acquired or ``timeout`` elapses, then yields with the lock held and
    releases it on exit. Degrades to a no-op on platforms without
    ``fcntl`` (best-effort, single-process safety only).
    """
    if not _FCNTL_AVAILABLE:  # pragma: no cover - exercised only on non-Unix platforms
        logger.debug(
            "atomic_file: fcntl unavailable on this platform; proceeding without "
            "an inter-process lock for %s",
            lock_path,
        )
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(lock_path, timeout)
                time.sleep(_LOCK_POLL_INTERVAL_SECONDS)
        try:
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(lock_fd)


def _umask_default_mode() -> int:
    """Return the umask-respecting default file mode (``0o666 & ~umask``).

    ``os.umask`` can only be *read* by temporarily setting it, so restore it
    immediately to avoid a permanent process-wide side effect.
    """
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _target_mode(target: Path) -> int:
    """Return the permission bits to give ``target`` after the atomic replace.

    tempfile.mkstemp creates its temp file at a hard-coded 0600, so without
    fixing this up ``os.replace`` would silently downgrade a user-authored
    0644 file (AGENTS.md, CLAUDE.md, .agent.md) to 0600 on every rewrite,
    breaking group-shared checkouts and other-uid tooling. Preserve the
    existing target's mode when it exists; otherwise fall back to the
    umask-respecting default the old ``write_text`` idiom would have produced.
    """
    try:
        return stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        return _umask_default_mode()


def locked_atomic_rewrite(
    target: Path,
    compute_new_content: Callable[[str], str],
    *,
    encoding: str = "utf-8",
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> None:
    """Read-modify-write ``target`` atomically and safely across processes.

    Holds an exclusive inter-process lock — on a CAO-owned lock file under
    ``LOCK_DIR``, keyed by a hash of ``target``'s resolved absolute path (see
    :func:`_lock_path_for`) — for the whole read-modify-write-replace cycle, so
    two concurrent writers (same or different process) never interleave: the
    second writer's ``compute_new_content`` always sees the first writer's
    published result.

    Args:
        target: The file to rewrite. Parent directory is created if
            missing. Read as ``""`` if the file does not yet exist.
        compute_new_content: Called with the current text content of
            ``target`` (``""`` if it does not exist) while the lock is
            held; must return the full new content to write.
        encoding: Text encoding for both the read and the write.
        lock_timeout: Seconds to wait for the lock before raising
            ``LockTimeoutError``. Use a bounded timeout (rather than an
            indefinite block) so a crashed holder or a genuine deadlock
            surfaces as a clear error instead of hanging the caller.

    Raises:
        LockTimeoutError: If the lock is not acquired within
            ``lock_timeout`` seconds.
        OSError: Propagated from filesystem operations (read, write,
            fsync, replace).

    Note:
        The lock files are **permanent by design** (never unlinked), but they
        live in CAO's scratch dir (``LOCK_DIR``), NOT beside the target — so
        the user's working tree gains no files even though the targets
        themselves are user-authored. Keeping them permanent matters because
        ``fcntl.flock`` operates on the inode: an unlink+recreate between two
        calls would break the inter-process serialization guarantee — callers
        would see different inodes and their locks would not conflict.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path_for(target)

    with _file_lock(lock_path, lock_timeout):
        existing = target.read_text(encoding=encoding) if target.exists() else ""
        _atomic_publish(target, compute_new_content(existing), encoding)


def locked_atomic_write(
    target: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    overwrite: bool = True,
    must_exist: bool = False,
) -> None:
    """Replace ``target``'s entire contents atomically and safely across processes.

    The blind-write sibling of :func:`locked_atomic_rewrite`. Identical locking,
    temp-file, fsync, mode-preservation and ``os.replace`` semantics, but it
    never reads the existing file.

    Use this when the new content does not depend on the old. Reaching for
    ``locked_atomic_rewrite`` with a callback that ignores its argument looks
    equivalent but is not: that path decodes the existing bytes first, so a
    target holding invalid ``encoding`` bytes raises ``UnicodeDecodeError``
    instead of being replaced. For a full replace that read is both pointless
    and the only thing that can fail, which makes a corrupt file unrepairable
    by the very call that would have fixed it.

    Args:
        target: The file to replace. Parent directory is created if missing.
        content: The full new content to write.
        encoding: Text encoding for the write.
        lock_timeout: Seconds to wait for the lock before raising
            ``LockTimeoutError``.
        overwrite: When False, refuse to replace an existing target. The check
            runs *inside* the lock, so two concurrent creators cannot both
            observe an absent file. Callers must not pre-check with
            ``target.exists()`` themselves: that test would sit outside this
            critical section and reintroduce the race.
        must_exist: When True, refuse to create a missing target, so the write
            is an update and never an insert. Checked inside the same lock and
            for the same reason: a caller testing existence beforehand would let
            a concurrent delete slip between the check and the write, turning an
            intended update back into a create. ``overwrite=False`` with
            ``must_exist=True`` is contradictory and raises ``ValueError``.

    Raises:
        FileExistsError: If ``target`` exists and ``overwrite`` is False.
        FileNotFoundError: If ``target`` is absent and ``must_exist`` is True.
        ValueError: If ``overwrite`` is False and ``must_exist`` is True.
        LockTimeoutError: If the lock is not acquired within ``lock_timeout``
            seconds.
        OSError: Propagated from filesystem operations (write, fsync, replace).
    """
    if not overwrite and must_exist:
        raise ValueError(
            "overwrite=False with must_exist=True can never succeed: it demands a "
            "target that exists and refuses to replace it."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path_for(target)

    with _file_lock(lock_path, lock_timeout):
        # Checked HERE, not by the caller: an exists() test outside this
        # critical section lets two concurrent creators both see "absent" and
        # both write, so the second silently clobbers the first. The same
        # applies in reverse to must_exist, where a concurrent delete between
        # an external check and the write would turn an update into a create.
        if not overwrite and target.exists():
            raise FileExistsError(f"{target} already exists")
        if must_exist and not target.exists():
            raise FileNotFoundError(f"{target} does not exist")
        _atomic_publish(target, content, encoding)


def locked_atomic_delete(
    target: Path,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    must_exist: bool = True,
) -> None:
    """Remove ``target`` while holding the same lock the writers use.

    The deleting sibling of :func:`locked_atomic_write`. It exists because
    ``locked_atomic_write``'s ``must_exist`` guarantee is only real if deletion
    participates in the same critical section. An unlocked
    ``exists()``-then-``unlink()`` can land *between* an update's existence check
    and its ``os.replace``: the delete succeeds, the update then republishes the
    file, and both callers are told they succeeded while a supposedly-deleted
    file is back on disk holding the new content. Serialising here makes one of
    the two lose, which is the whole point of ``must_exist``.

    Deletion needs no temp file or ``os.replace`` because ``unlink`` is already
    atomic. The lock is what this adds, not atomicity.

    Note:
        Safe against the ``fcntl.flock``-is-per-inode hazard because the lock
        file is not the target. ``_lock_path_for`` keys a file under
        ``LOCK_DIR`` by a hash of the target's *resolved* path, and those lock
        files are never unlinked, so removing the target leaves the lock inode
        untouched and a concurrent writer keeps contending on the same one.

    Args:
        target: The file to remove.
        lock_timeout: Seconds to wait for the lock before raising
            ``LockTimeoutError``.
        must_exist: When True (default), a missing target raises
            ``FileNotFoundError``. The check runs inside the lock, so two
            concurrent deleters cannot both observe the file as present.
            Pass False to make removal idempotent.

    Raises:
        FileNotFoundError: If ``target`` is absent and ``must_exist`` is True.
        LockTimeoutError: If the lock is not acquired within ``lock_timeout``
            seconds.
        OSError: Propagated from the unlink.
    """
    lock_path = _lock_path_for(target)

    with _file_lock(lock_path, lock_timeout):
        # Inside the lock for the same reason as locked_atomic_write's checks:
        # a caller testing existence beforehand would race both a concurrent
        # deleter (double unlink) and a concurrent update (resurrected file).
        if not target.exists():
            if must_exist:
                raise FileNotFoundError(f"{target} does not exist")
            return
        target.unlink()


def _atomic_publish(target: Path, content: str, encoding: str) -> None:
    """Write ``content`` to ``target`` via a unique temp file + ``os.replace``.

    Shared by :func:`locked_atomic_rewrite` and :func:`locked_atomic_write`.
    The caller MUST already hold the target's lock; this helper does no locking
    of its own.
    """
    # Capture the mode to apply to the published file BEFORE we write —
    # tempfile.mkstemp creates the temp at 0600, so without this fixup the
    # os.replace below would downgrade a user-authored 0644 file to 0600.
    mode = _target_mode(target)

    # Unique temp file in the SAME directory as target (same filesystem,
    # so the final os.replace stays atomic) rather than a fixed
    # ``target + ".tmp"`` name, so an unrelated unlocked writer (or a
    # retry of this same call) can never collide with this call's temp
    # file or unlink it out from under another in-flight write.
    fd, temp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            # Restore the target's (or umask-default) mode on the temp file
            # before the replace, so the published file keeps its intended
            # permissions rather than inheriting mkstemp's 0600.
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        # Only ever removes OUR OWN uniquely-named temp file.
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
