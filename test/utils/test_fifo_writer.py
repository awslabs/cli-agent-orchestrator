"""The pipe-pane writer refuses anything that is not a FIFO at its path.

``TmuxClient.pipe_pane`` used to run ``cat >> <fifo>``; a shell ``>>`` follows
symlinks and appends to a regular file, so a path swapped after the reader
attached received the pane's output. ``utils/fifo_writer.py`` is what tmux runs
now. These tests drive it two ways: in-process through ``run()`` for the
refusals and the copy loop, and as the real subprocess ``pipe-pane`` starts, so
the file-path invocation and the interpreter choice are exercised too.
"""

import os
import stat
import subprocess
import sys
import threading

import pytest

from cli_agent_orchestrator.utils import fifo_writer

pytestmark = pytest.mark.skipif(os.name != "posix", reason="FIFOs and O_NOFOLLOW are POSIX")


def _reader(fifo_path):
    """Open the read end the way ``fifo_reader`` does, so a writer's open returns."""
    return os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)


def _drain(read_fd, expected_len, timeout=5.0):
    """Read until ``expected_len`` bytes arrived; polling, the reader is non-blocking."""
    import time

    out = b""
    deadline = time.monotonic() + timeout
    while len(out) < expected_len and time.monotonic() < deadline:
        try:
            chunk = os.read(read_fd, 65536)
        except BlockingIOError:
            time.sleep(0.005)
            continue
        if not chunk:
            time.sleep(0.005)
            continue
        out += chunk
    return out


class TestRefusals:
    def test_symlink_at_the_path_is_refused_and_target_untouched(self, tmp_path):
        target = tmp_path / "victim.log"
        target.write_bytes(b"")
        link = tmp_path / "t.fifo"
        link.symlink_to(target)
        src_r, src_w = os.pipe()
        os.write(src_w, b"pane output")
        os.close(src_w)
        try:
            assert fifo_writer.run(str(link), src_fd=src_r) == 1
        finally:
            os.close(src_r)
        assert target.read_bytes() == b""

    def test_regular_file_at_the_path_is_refused_and_unchanged(self, tmp_path):
        regular = tmp_path / "t.fifo"
        regular.write_bytes(b"before")
        src_r, src_w = os.pipe()
        os.write(src_w, b"pane output")
        os.close(src_w)
        try:
            assert fifo_writer.run(str(regular), src_fd=src_r) == 1
        finally:
            os.close(src_r)
        assert regular.read_bytes() == b"before"

    def test_missing_path_is_refused_not_created(self, tmp_path):
        """``cat >>`` would have created the file; the writer must not."""
        missing = tmp_path / "never.fifo"
        src_r, src_w = os.pipe()
        os.close(src_w)
        try:
            assert fifo_writer.run(str(missing), src_fd=src_r) == 1
        finally:
            os.close(src_r)
        assert not missing.exists()

    def test_open_requires_a_fifo(self, tmp_path):
        regular = tmp_path / "f"
        regular.write_bytes(b"")
        with pytest.raises(OSError):
            fifo_writer.open_fifo_for_writing(str(regular))


class TestCopy:
    def test_copies_stdin_into_a_real_fifo(self, tmp_path):
        fifo = tmp_path / "t.fifo"
        os.mkfifo(fifo, 0o600)
        read_fd = _reader(fifo)
        src_r, src_w = os.pipe()
        payload = b"x" * (200 * 1024) + b"\nend\n"  # several 64 KiB chunks
        try:

            def feed():
                os.write(src_w, payload)
                os.close(src_w)

            feeder = threading.Thread(target=feed, daemon=True)
            feeder.start()
            got = []

            def write():
                got.append(fifo_writer.run(str(fifo), src_fd=src_r))

            writer = threading.Thread(target=write, daemon=True)
            writer.start()
            out = _drain(read_fd, len(payload))
            writer.join(timeout=5.0)
            feeder.join(timeout=5.0)
        finally:
            os.close(read_fd)
            os.close(src_r)
        assert got == [0]
        assert out == payload

    def test_reader_going_away_ends_the_copy_quietly(self, tmp_path):
        fifo = tmp_path / "t.fifo"
        os.mkfifo(fifo, 0o600)
        read_fd = _reader(fifo)
        dst_fd = fifo_writer.open_fifo_for_writing(str(fifo))
        os.close(read_fd)  # terminal torn down: no reader left
        src_r, src_w = os.pipe()
        os.write(src_w, b"late output")
        os.close(src_w)
        try:
            fifo_writer.copy(src_r, dst_fd)  # BrokenPipeError handled, no raise
        finally:
            os.close(src_r)
            os.close(dst_fd)


class TestAsPipePaneRunsIt:
    """The exact invocation ``TmuxClient._pipe_pane_command`` builds."""

    def _spawn(self, fifo_path):
        return subprocess.Popen(
            [sys.executable, "-I", "-S", fifo_writer.__file__, str(fifo_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def test_subprocess_delivers_output_to_the_fifo(self, tmp_path):
        fifo = tmp_path / "t.fifo"
        os.mkfifo(fifo, 0o600)
        read_fd = _reader(fifo)
        try:
            proc = self._spawn(fifo)
            # communicate() writes stdin and closes it itself; closing it by
            # hand first makes communicate() raise on Python 3.10-3.12
            # ("flush of closed file"), which only 3.13+ tolerates.
            _, err = proc.communicate(b"hello from the pane\n", timeout=10)
            out = _drain(read_fd, len(b"hello from the pane\n"))
        finally:
            os.close(read_fd)
        assert proc.returncode == 0, err
        assert out == b"hello from the pane\n"

    def test_subprocess_refuses_a_swapped_symlink(self, tmp_path):
        victim = tmp_path / "victim.log"
        victim.write_bytes(b"")
        link = tmp_path / "t.fifo"
        link.symlink_to(victim)
        proc = self._spawn(link)
        _, err = proc.communicate(b"stolen output\n", timeout=10)
        assert proc.returncode == 1
        assert b"fifo_writer" in err
        assert victim.read_bytes() == b""

    def test_module_has_no_project_imports(self):
        """Started by file path on every pipe re-arm: it must not pull the package in."""
        source = open(fifo_writer.__file__, encoding="utf-8").read()
        assert "cli_agent_orchestrator" not in source.replace(
            "``cli_agent_orchestrator`` package", ""
        )
        mode = os.stat(fifo_writer.__file__).st_mode
        assert stat.S_ISREG(mode)
