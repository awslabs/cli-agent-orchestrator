"""On-disk layout for installed Agent Plugins and their install records.

Layout::

    ~/.aws/cli-agent-orchestrator/
    ├── agent-plugins/
    │   ├── <plugin-name>/              # PLUGIN_ROOT — package bytes, never mutated
    │   └── .state/<plugin-name>.json   # install record (CAO-owned)
    ├── agent-plugin-data/<plugin-name>/ # PLUGIN_DATA (§9.1), survives updates
    └── skills/                          # existing SKILLS_DIR — projection target

``.state/`` is a **dot-prefixed sibling** so it is never mistaken for a plugin:
:meth:`InstalledPluginStore.list_installed` skips every name beginning with
``.``. That also makes crash-time staging directories (which are dot-prefixed
for the same reason) invisible to listing rather than half-installed plugins.

``PLUGIN_DATA`` lives **outside** ``AGENT_PLUGINS_DIR`` on purpose: §9.1 requires
its contents survive a plugin update, and an update replaces the plugin root
wholesale.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import shutil
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable, List, Optional

from cli_agent_orchestrator.agent_plugins.models import PluginRecord
from cli_agent_orchestrator.constants import AGENT_PLUGIN_DATA_DIR, AGENT_PLUGINS_DIR

logger = logging.getLogger(__name__)

# Owner-only, matching CAO_HOME_DIR's posture. When CAO_HOME_DIR is relocated
# outside ~/.aws there is no parent permission umbrella, and an installed plugin
# root is executable content in every sense that matters.
_DIR_MODE = 0o700

# Subdirectory of AGENT_PLUGINS_DIR holding install records.
_STATE_DIRNAME = ".state"
_LOCK_FILENAME = ".lock"


class PluginStoreError(RuntimeError):
    """Raised when the store cannot complete a publish or unpublish."""


def _ensure_dir(path: Path) -> Path:
    """Create ``path`` (and parents) with owner-only permissions, idempotently."""
    path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    try:
        os.chmod(path, _DIR_MODE)
    except OSError:  # pragma: no cover - read-only mounts / foreign ownership
        pass
    return path


class InstalledPluginStore:
    """Owns the installed set and its install records.

    The store knows nothing about validation, resolution, or skill projection.
    It publishes bytes atomically and hands back records.
    """

    def __init__(
        self,
        plugins_dir: Optional[Path] = None,
        data_dir: Optional[Path] = None,
    ) -> None:
        """Bind the store to its directories.

        Arguments default to the module-level constants and exist so tests (and
        any future multi-root use) can point a store at a scratch tree without
        re-importing ``constants``.
        """
        self._plugins_dir = Path(plugins_dir) if plugins_dir else AGENT_PLUGINS_DIR
        self._data_dir = Path(data_dir) if data_dir else AGENT_PLUGIN_DATA_DIR

    # ── locations ────────────────────────────────────────────────────────

    @property
    def plugins_dir(self) -> Path:
        """Root holding one directory per installed plugin."""
        return self._plugins_dir

    @property
    def data_dir(self) -> Path:
        """Root holding one ``PLUGIN_DATA`` directory per plugin."""
        return self._data_dir

    @property
    def state_dir(self) -> Path:
        """Directory holding ``<plugin-name>.json`` install records."""
        return self._plugins_dir / _STATE_DIRNAME

    def plugin_root(self, name: str) -> Path:
        """Return the ``PLUGIN_ROOT`` path for ``name`` (need not exist)."""
        return self._plugins_dir / _validate_plugin_dirname(name)

    def plugin_data_dir(self, name: str, *, create: bool = False) -> Path:
        """Return the ``PLUGIN_DATA`` path for ``name``, optionally creating it."""
        path = self._data_dir / _validate_plugin_dirname(name)
        if create:
            _ensure_dir(self._data_dir)
            _ensure_dir(path)
        return path

    def _record_path(self, name: str) -> Path:
        return self.state_dir / f"{_validate_plugin_dirname(name)}.json"

    # ── reads ────────────────────────────────────────────────────────────

    def list_installed(self) -> List[PluginRecord]:
        """Return every install record, sorted by plugin name.

        Sorted by ``name`` rather than returned in directory order because the
        projection engine's deterministic collision rule is a total order over
        plugin names; handing callers an already-ordered list means no caller
        can accidentally depend on ``os.scandir`` ordering.

        A record whose JSON is unreadable or malformed is logged and skipped
        rather than raising: listing is called from ``cao plugin list``, the web
        panel, and the projection rebuild, none of which may fail because one
        record got corrupted.
        """
        state_dir = self.state_dir
        if not state_dir.is_dir():
            return []

        records: List[PluginRecord] = []
        for entry in sorted(state_dir.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".") or entry.suffix != ".json" or not entry.is_file():
                continue
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
                records.append(PluginRecord.from_dict(data))
            except Exception as exc:
                logger.warning("Skipping unreadable agent-plugin record '%s': %s", entry, exc)
        return sorted(records, key=lambda record: record.name)

    def get(self, name: str) -> Optional[PluginRecord]:
        """Return one install record, or ``None`` when the plugin is not installed."""
        try:
            path = self._record_path(name)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            return PluginRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.warning("Unreadable agent-plugin record '%s': %s", path, exc)
            return None

    def is_installed(self, name: str) -> bool:
        """Whether a plugin root exists for ``name``."""
        try:
            return self.plugin_root(name).exists()
        except ValueError:
            return False

    # ── writes ───────────────────────────────────────────────────────────

    def publish(self, staged: Path, record: PluginRecord, *, force: bool = False) -> PluginRecord:
        """Move a validated staging tree into the store atomically.

        Uses the stage-then-rename pattern already proven in
        ``cli/commands/init.py::seed_default_skills()``: copy into a
        dot-prefixed temporary directory *inside* the destination's parent (so
        the rename is same-filesystem and therefore atomic), then rename into
        place. A process interruption at any point before the rename leaves the
        store byte-identical to its pre-publish state — which is the mechanism
        behind correctness property P4.

        ``force`` replaces an existing plugin of the same name. The replacement
        is *not* atomic end-to-end (POSIX cannot rename a directory over a
        non-empty directory), so the old root is moved aside first and only
        deleted once the new root is in place; a crash between the two leaves
        the old bytes recoverable in a dot-prefixed sibling rather than losing
        both.
        """
        name = _validate_plugin_dirname(record.name)
        _ensure_dir(self._plugins_dir)
        destination = self.plugin_root(name)

        with _store_lock(self.state_dir):
            return self._publish_locked(staged, record, name, destination, force=force)

    def _publish_locked(
        self,
        staged: Path,
        record: PluginRecord,
        name: str,
        destination: Path,
        *,
        force: bool,
    ) -> PluginRecord:
        """The body of :meth:`publish`, run while holding the store lock."""
        if destination.exists() and not force:
            raise PluginStoreError(
                f"Agent plugin '{name}' is already installed. Use --force to replace it."
            )

        # Captured before anything moves, so a failed record write on a force
        # update can restore the *previous* record rather than leaving the old
        # one to describe the new bytes.
        record_path = self._record_path(name)
        previous_record_bytes: Optional[bytes] = (
            record_path.read_bytes() if record_path.is_file() else None
        )

        with TemporaryDirectory(prefix=f".{name}.", dir=self._plugins_dir) as staging_root:
            staged_copy = Path(staging_root) / name
            shutil.copytree(staged, staged_copy, symlinks=True)

            backup: Optional[Path] = None
            if destination.exists():
                # Re-checked against `force`, not just for existence. Without the
                # lock above this is the load-bearing guard: an install that
                # found the name free at entry and occupied at swap time has lost
                # a race, and must fail loudly instead of silently replacing the
                # winner's bytes.
                if not force:
                    raise PluginStoreError(
                        f"Agent plugin '{name}' was published concurrently; re-run the install."
                    )
                backup = self._plugins_dir / f".{name}.replaced.{os.getpid()}"
                _rmtree_quiet(backup)
                destination.rename(backup)

            try:
                staged_copy.rename(destination)
            except OSError as exc:
                if backup is not None and not destination.exists():
                    # Put the previous bytes back. `not destination.exists()` is
                    # the concurrency guard: if something else published while we
                    # were mid-swap, restoring would clobber *its* tree, so the
                    # newer content is left alone and the old backup is dropped
                    # below instead.
                    try:
                        backup.rename(destination)
                    except OSError as restore_exc:
                        # The one case where a failure can destroy a working
                        # plugin: the aside-move succeeded, the swap failed, and
                        # now the restore has failed too. `destination` does not
                        # exist and this backup holds the only copy of the
                        # operator's bytes.
                        #
                        # `backup` is cleared *before* raising, which is the whole
                        # point: `finally` runs on the way out of an exception too,
                        # so leaving the variable set would delete the very
                        # directory this branch exists to preserve. The path is
                        # kept in a local for the message, because manual recovery
                        # is a single `mv` and an operator who is not told where
                        # to look cannot perform it.
                        preserved, backup = backup, None
                        raise PluginStoreError(
                            f"Agent plugin '{name}' could not be replaced, and the previous "
                            f"version could not be restored automatically ({restore_exc}). "
                            f"Its files are intact at {preserved} — move that directory back "
                            f"to {destination} to recover. Nothing was deleted."
                        ) from restore_exc
                    backup = None
                if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                    raise PluginStoreError(
                        f"Agent plugin '{name}' was published concurrently; " f"re-run the install."
                    ) from exc
                raise
            else:
                # The install record is committed *here*, inside the block that
                # still holds `backup`, and not after it. Writing it later meant
                # the `finally` below had already deleted the previous bytes, so
                # a failing record write (full or read-only state volume) left
                # the new bytes in place under the *old* record — a v1 record
                # describing v2 files, which is exactly the failed-install
                # isolation guarantee this store claims to provide.
                try:
                    self.write_record(record)
                except Exception:
                    if backup is not None:
                        # Force update: undo the swap and put the previous record
                        # back, so the net effect of a failed force install is
                        # nothing at all.
                        _rmtree_quiet(destination)
                        try:
                            backup.rename(destination)
                        except OSError as restore_exc:
                            preserved, backup = backup, None
                            raise PluginStoreError(
                                f"Agent plugin '{name}' record could not be written, and the "
                                f"previous version could not be restored automatically "
                                f"({restore_exc}). Its files are intact at {preserved} — move "
                                f"that directory back to {destination} to recover. Nothing "
                                f"was deleted."
                            ) from restore_exc
                        backup = None
                        if previous_record_bytes is not None:
                            self._record_path(name).write_bytes(previous_record_bytes)
                    else:
                        # First install: there is no previous state to return to,
                        # so the new root is removed rather than left orphaned —
                        # untracked bytes would block a later non-force add.
                        _rmtree_quiet(destination)
                    raise
            finally:
                # Reached on success (the old tree is now redundant) and on a
                # failure that restored successfully (`backup` set to None above,
                # so nothing is deleted). The unrecoverable path raises before
                # here with `backup` deliberately left on disk.
                if backup is not None:
                    _rmtree_quiet(backup)

        return record

    def write_record(self, record: PluginRecord) -> Path:
        """Persist (or overwrite) one install record, atomically.

        Whole-record write, and therefore **last-write-wins**: a caller that
        reconstructs a record from a stale snapshot will revert whatever another
        process committed in the meantime. Callers that only need to patch
        ``projected_skill_names`` must use :meth:`update_projected_names` or
        :meth:`release_projected_name`, which re-read under the store lock.
        """
        _ensure_dir(self._plugins_dir)
        _ensure_dir(self.state_dir)
        path = self._record_path(record.name)
        temp_path = path.with_name(f".{path.name}.tmp")
        temp_path.write_text(record.to_json(), encoding="utf-8")
        temp_path.replace(path)
        return path

    def update_projected_names(self, name: str, owned: Iterable[str]) -> bool:
        """Set one record's ``projected_skill_names``, honestly, under the lock.

        Reported in review of revision 2: ``rebuild_projection`` snapshots the
        installed set, does slow filesystem work, then wrote back a **complete**
        record rebuilt from that stale snapshot with no lock and no freshness
        check. Two interleavings followed. A concurrent ``add --force`` publishing
        v2 under the lock had its record reverted to the snapshot's v1 metadata —
        the store then describing v1 while v2 bytes were installed, exactly the
        corruption ``_publish_locked``'s rollback exists to prevent. And a
        concurrent removal had its unpublished record *resurrected*, so ``get()``
        reported a plugin whose bytes were gone and a later non-force ``add`` was
        refused. Both are live in-process: ``POST /plugins`` and
        ``DELETE /plugins/{name}`` each offload to a thread, so two in-flight
        requests are two concurrent rebuilds.

        This is a compare-and-set instead: take the lock, re-read the record, and
        patch **only** that one field, so a concurrent version/source/findings
        change survives. Returns ``False`` without writing when the record no
        longer exists — tolerating unpublish-during-rebuild rather than
        resurrecting it.

        Deliberately narrow rather than holding the lock across the whole
        rebuild: materialization is slow filesystem work, and a rebuild that held
        the lock would serialize every install behind it.

        **Invariant:** no caller may invoke this (or ``rebuild_projection``) while
        already holding ``_store_lock`` — ``flock`` on a second file descriptor
        would deadlock rather than re-enter. ``publish``/``unpublish`` release the
        lock before the installer rebuilds, which is what keeps this true.
        """
        validated = _validate_plugin_dirname(name)
        desired = tuple(owned)
        with _store_lock(self.state_dir):
            fresh = self.get(validated)
            if fresh is None:
                logger.info(
                    "Agent plugin '%s' is no longer installed; not writing back its "
                    "projected skill names",
                    validated,
                )
                return False
            if tuple(fresh.projected_skill_names) == desired:
                return True
            self.write_record(replace(fresh, projected_skill_names=desired))
            return True

    def release_projected_name(self, skill_name: str) -> Optional[str]:
        """Drop ``skill_name`` from whichever record claims it, under the lock.

        The sibling of :meth:`update_projected_names`, for the ownership transfer
        performed by ``cao skills add --force``. Same discipline: the find-owner →
        write-remaining sequence happens inside the lock and against freshly read
        records, so it cannot revert a concurrent publish.

        Returns the plugin that gave up the claim, or ``None`` when no installed
        plugin claimed the name. Raises :class:`PluginStoreError` when a plugin
        *did* claim it and the record could not be updated — the caller must be
        able to tell "nothing to release" from "the release failed", because a
        failure that reads as success is what let a later rebuild delete the
        user's own skill directory.
        """
        with _store_lock(self.state_dir):
            try:
                records = self.list_installed()
            except OSError as exc:
                raise PluginStoreError(
                    f"Could not read agent plugin records to release '{skill_name}': {exc}"
                ) from exc

            for record in records:
                if skill_name not in record.projected_skill_names:
                    continue
                remaining = tuple(n for n in record.projected_skill_names if n != skill_name)
                try:
                    self.write_record(replace(record, projected_skill_names=remaining))
                except OSError as exc:
                    raise PluginStoreError(
                        f"Could not update agent plugin '{record.name}' to release its "
                        f"claim on skill '{skill_name}': {exc}"
                    ) from exc
                return record.name
        return None

    def unpublish(self, name: str, *, purge_data: bool = False) -> bool:
        """Remove a plugin's root and install record.

        ``purge_data`` defaults to ``False``: §9.1 says a client *MAY* delete
        ``PLUGIN_DATA`` on uninstall, and retaining it makes ``remove``
        non-destructive by default. ``cao plugin remove --purge-data`` opts in.
        Making the choice explicit rather than incidental is what makes the
        idempotence property (P5) decidable.

        Returns ``True`` when something was actually removed.

        Raises :class:`PluginStoreError` when the root (or, with
        ``purge_data``, the data directory) could not be deleted. Reporting the
        failure is what keeps the return value honest: the record is unlinked
        only *after* every tree this call promises to remove is confirmed gone,
        so ``get()`` can never report a plugin as absent while its root — or the
        data directory a purge undertook to delete — is still on disk.
        """
        validated = _validate_plugin_dirname(name)
        with _store_lock(self.state_dir):
            return self._unpublish_locked(validated, purge_data=purge_data)

    def _unpublish_locked(self, validated: str, *, purge_data: bool) -> bool:
        """The body of :meth:`unpublish`, run while holding the store lock.

        Serialized with ``publish`` for the same reason: a removal interleaved
        with an install of the same name could otherwise delete bytes the install
        had just placed, or unlink a record the install had just written.
        """
        removed = False

        root = self.plugin_root(validated)
        if root.exists() or root.is_symlink():
            # Ordered deliberately: the bytes go first and the deletion reports
            # failure, so an undeletable root aborts here with the record still
            # in place and the installation still tracked.
            _rmtree_reporting(root, what=f"agent plugin '{validated}'")
            removed = True

        # Before the record unlink, not after. The record is the retry handle: a
        # data-purge failure used to raise *after* the record was gone, so the
        # error's promise that "the installation remains tracked and can be
        # retried" was false and the stranded data directory had no retry path at
        # all — the next remove reports "not installed". With the purge first, the
        # docstring's ordering invariant (the record is unlinked only after the
        # bytes are confirmed gone) covers both trees, and a retry re-runs safely
        # because root deletion treats absence as success (property P5).
        if purge_data:
            data_path = self.plugin_data_dir(validated)
            if data_path.exists():
                _rmtree_reporting(data_path, what=f"agent plugin data for '{validated}'")

        record_path = self._record_path(validated)
        if record_path.exists():
            record_path.unlink()
            removed = True

        return removed


def _validate_plugin_dirname(name: str) -> str:
    """Reject any plugin name that could escape the store's directories.

    §5.5 already constrains manifest names to ``[a-z0-9.-]`` with no ``..``, and
    the validator enforces that. This is the second, independent guard applied
    at the point a name becomes a path — a store must not rely on its callers
    having validated, because ``get()``/``plugin_root()`` are also reachable
    from the CLI, the API, and install records written by an older version.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("Agent plugin name must be a non-empty string")
    if name.startswith("."):
        raise ValueError(f"Agent plugin name must not start with '.': {name!r}")
    if "\x00" in name:
        raise ValueError(f"Agent plugin name must not contain a NUL byte: {name!r}")
    # Built as a comprehension rather than a conditional add: `os.altsep` is
    # None on POSIX, so an `if os.altsep:` branch is dead code on this platform
    # and unreachable by any test running here.
    separators = {sep for sep in ("/", "\\", os.sep, os.altsep) if sep}
    if any(sep in name for sep in separators):
        raise ValueError(f"Agent plugin name must not contain a path separator: {name!r}")
    if ".." in name:
        raise ValueError(f"Agent plugin name must not contain '..': {name!r}")
    return name


def _store_lock(state_dir: Path):
    """Serialize whole-store mutations across processes.

    Reported in review of revision 1: ``publish()`` checked
    ``destination.exists() and not force`` once, before staging, while the swap
    re-checked existence **without** re-consulting ``force``. Two concurrent
    installs of the same not-yet-installed name therefore both passed the first
    guard, and the loser silently took the *replace* path — backing up the
    winner's freshly published tree, renaming over it and deleting the backup —
    while the two unordered ``write_record`` calls could leave a record
    describing one plugin's metadata beside the other's bytes. The
    ``EEXIST``/``ENOTEMPTY`` guard never fires on that interleaving, because no
    ``rename`` actually fails.

    An advisory ``flock`` is used rather than a lock *directory*: the kernel
    releases it when the holder dies, so a crashed install cannot leave a stale
    lock that wedges every later one. Where ``fcntl`` is unavailable this
    degrades to a no-op and the swap-time ``force`` re-check inside ``publish``
    remains the correctness guarantee — that check turns the race into a clean
    error rather than silent data loss even with no lock at all.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        return contextlib.nullcontext()

    @contextlib.contextmanager
    def _locked():
        _ensure_dir(state_dir)
        lock_path = state_dir / _LOCK_FILENAME
        handle = open(lock_path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    return _locked()


def _rmtree_quiet(path: Path) -> None:
    """Delete a path (file, symlink, or tree) without raising on absence."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - permissions / busy handles
        logger.warning("Could not remove '%s': %s", path, exc)


def _rmtree_reporting(path: Path, *, what: str) -> None:
    """Delete a path and **raise** when it could not be removed.

    The counterpart to :func:`_rmtree_quiet`, for the callers where swallowing
    the error produces a *false success*. ``unpublish()`` used the quiet helper
    and then unlinked the install record regardless, so a busy handle or a
    permission failure (very plausible on Windows while an MCP executable from
    the plugin is still open) left the full plugin root on disk with no record
    pointing at it — an untracked installation that ``get()`` reports as absent
    and that blocks a later non-force ``add``.

    Absence is still success: removal is idempotent, which is what keeps the
    ``remove`` idempotence property (P5) decidable. The post-condition is
    checked rather than inferred from the absence of an exception, because a
    partial ``shutil.rmtree`` can leave the tree behind after deleting some of
    it.
    """
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PluginStoreError(
            f"Could not remove {what} at {path}: {exc}. Nothing was recorded as "
            f"removed, so the installation remains tracked and can be retried "
            f"once whatever holds the path releases it."
        ) from exc

    if path.exists() or path.is_symlink():
        raise PluginStoreError(
            f"Could not remove {what}: {path} still exists after deletion. "
            f"Nothing was recorded as removed, so the installation remains "
            f"tracked and can be retried."
        )
