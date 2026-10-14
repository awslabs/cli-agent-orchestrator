"""Filesystem invariants at CAO's predictable paths.

Derived from the ``fs_invariants`` security harness written by Augusto Santos
(@gutosantos82) while reviewing CAO's CI security gates. The probes it carried
that are already pinned next to their fixes are not repeated here
(``test/services/test_fifo_reader.py::TestFifoPathHardening`` for the FIFO path,
``test/utils/test_atomic_file.py`` for the umask rule). These are the two angles
that were not:

* a symlink planted at the per-terminal FIFO path that points at a *genuine* FIFO
  outside CAO's directory is still refused by ``FifoManager.create_reader``: the
  check must be on the link itself (``lstat``, not ``stat``), and the outside FIFO
  is left untouched (#823);
* with two threads creating files through the atomic-file primitives under
  ``umask 022``, no file is ever born group- or world-writable. The helper that
  used to read the umask via ``os.umask(0)`` + restore opened exactly that window
  for every other thread in the process (#821).
"""

import os
import stat
import threading

import pytest

from cli_agent_orchestrator.services.fifo_reader import FifoManager
from cli_agent_orchestrator.utils.atomic_file import locked_atomic_write

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX FIFOs and file modes")


class TestFifoPathIsNeverFollowed:
    def test_symlink_to_a_real_fifo_outside_cao_home_is_refused(self, tmp_path, monkeypatch):
        fifo_dir = tmp_path / "fifos"
        fifo_dir.mkdir(mode=0o700)
        outside = tmp_path / "outside"
        outside.mkdir()
        planted = outside / "planted.fifo"
        os.mkfifo(planted)
        os.chmod(planted, 0o600)
        planted_before = os.lstat(planted)
        link = fifo_dir / "cafebabe.fifo"
        link.symlink_to(planted)
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", fifo_dir)
        manager = FifoManager()

        with pytest.raises(OSError, match="not a FIFO"):
            manager.create_reader("cafebabe")

        with manager._lock:
            assert "cafebabe" not in manager._readers
            assert "cafebabe" not in manager._threads
        assert not any(t.name == "fifo-cafebabe" for t in threading.enumerate())
        # Refused, not replaced: the link is still a link, the outside FIFO untouched.
        assert link.is_symlink()
        planted_after = os.lstat(planted)
        assert stat.S_ISFIFO(planted_after.st_mode)
        assert planted_after.st_ino == planted_before.st_ino
        assert stat.S_IMODE(planted_after.st_mode) == 0o600


class TestNoFileIsBornGroupOrWorldWritable:
    WRITES_PER_THREAD = 40

    def test_concurrent_atomic_writers_under_umask_022(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.utils.atomic_file.LOCK_DIR", tmp_path / "locks")
        born: list = []
        failures: list = []

        def writer(prefix: str) -> None:
            try:
                for i in range(self.WRITES_PER_THREAD):
                    target = tmp_path / f"{prefix}-{i}.md"
                    locked_atomic_write(target, "content")
                    born.append((target.name, stat.S_IMODE(os.stat(target).st_mode)))
            except Exception as exc:  # pragma: no cover - surfaced by the assertion below
                failures.append(repr(exc))

        previous_umask = os.umask(0o022)
        try:
            threads = [threading.Thread(target=writer, args=(p,)) for p in ("left", "right")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=120)
        finally:
            os.umask(previous_umask)

        assert not failures, failures
        assert len(born) == 2 * self.WRITES_PER_THREAD
        loose = [(name, oct(mode)) for name, mode in born if mode & 0o022]
        assert not loose, f"files born group/world-writable under umask 022: {loose}"
        assert {mode for _, mode in born} == {0o644}
