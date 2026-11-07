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


class TestWaitingForAReader:
    """The writer waits for a reader without blocking in ``open()``, and its pane ends the wait.

    A blocking ``open()`` of a FIFO with no reader never returns and never reads
    stdin, so a writer started while its reader was not attached outlived its
    pane and its tmux server as an orphan.
    """

    def test_stdin_closing_before_any_reader_ends_the_writer(self, tmp_path):
        import time

        fifo = tmp_path / "t.fifo"
        os.mkfifo(fifo, 0o600)
        src_r, src_w = os.pipe()
        os.write(src_w, b"output nobody will read")
        os.close(src_w)  # the pane is gone
        try:
            start = time.monotonic()
            assert fifo_writer.run(str(fifo), src_fd=src_r) == 0
            assert time.monotonic() - start < 2.0
        finally:
            os.close(src_r)
        assert stat.S_ISFIFO(os.stat(fifo).st_mode)

    def test_a_late_reader_still_gets_output_written_before_it_attached(self, tmp_path):
        fifo = tmp_path / "t.fifo"
        os.mkfifo(fifo, 0o600)
        src_r, src_w = os.pipe()
        os.write(src_w, b"early ")
        got = []
        writer = threading.Thread(
            target=lambda: got.append(fifo_writer.run(str(fifo), src_fd=src_r)), daemon=True
        )
        writer.start()
        try:
            writer.join(timeout=0.3)
            assert writer.is_alive(), "the writer gave up before a reader attached"
            read_fd = _reader(fifo)
            try:
                os.write(src_w, b"late")
                os.close(src_w)
                out = _drain(read_fd, len(b"early late"))
                writer.join(timeout=5.0)
            finally:
                os.close(read_fd)
        finally:
            os.close(src_r)
        assert got == [0]
        assert out == b"early late"

    def test_the_opened_fd_is_blocking(self, tmp_path):
        fifo = tmp_path / "t.fifo"
        os.mkfifo(fifo, 0o600)
        read_fd = _reader(fifo)
        try:
            dst_fd = fifo_writer.open_fifo_for_writing(str(fifo))
            try:
                assert os.get_blocking(dst_fd)
            finally:
                os.close(dst_fd)
        finally:
            os.close(read_fd)

    def test_without_src_fd_the_open_waits_for_a_late_reader(self, tmp_path):
        fifo = tmp_path / "t.fifo"
        os.mkfifo(fifo, 0o600)
        read_fds = []
        late = threading.Timer(0.2, lambda: read_fds.append(_reader(fifo)))
        late.start()
        try:
            dst_fd = fifo_writer.open_fifo_for_writing(str(fifo))
            os.close(dst_fd)
        finally:
            late.join()
            for fd in read_fds:
                os.close(fd)


class TestWaitBranchesInProcess:
    """The same branches the subprocess tests reach, driven in this process.

    Coverage does not follow the ``-I -S`` subprocess, so these keep the wait loop's
    refusal and hang-up checks measured.
    """

    def test_enxio_on_a_socket_path_is_refused_not_waited_on(self, monkeypatch):
        """Linux answers a non-blocking open of a socket with ENXIO, like a FIFO with no reader."""
        import errno as errno_mod
        import shutil
        import socket
        import tempfile

        # Short directory: tmp_path can exceed the 104-byte AF_UNIX path limit on macOS.
        short_dir = tempfile.mkdtemp(prefix="cao-fw-", dir="/tmp")
        path = os.path.join(short_dir, "t.fifo")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(path)

            calls = []

            def enxio(*_args, **_kwargs):
                calls.append(1)
                if len(calls) > 1:
                    # A regressed refusal would retry for ever; fail instead of hanging.
                    raise AssertionError("retried after a non-FIFO")
                raise OSError(errno_mod.ENXIO, "No such device or address")

            # Scoped, so the cleanup below gets the real os.open back.
            with monkeypatch.context() as patch:
                patch.setattr(fifo_writer.os, "open", enxio)
                with pytest.raises(OSError) as info:
                    fifo_writer.open_fifo_for_writing(path, src_fd=None)
        finally:
            sock.close()
            shutil.rmtree(short_dir, ignore_errors=True)
        assert info.value.errno == errno_mod.EEXIST
        assert len(calls) == 1

    def test_stdin_closed_is_false_for_events_other_than_a_hang_up(self, monkeypatch):
        class _Poller:
            def register(self, fd, mask):
                pass

            def poll(self, timeout):
                assert timeout == 0
                return [(0, fifo_writer.select.POLLIN)]

        monkeypatch.setattr(fifo_writer.select, "poll", _Poller)
        assert fifo_writer._stdin_closed(0) is False

    def test_stdin_closed_is_true_on_a_hang_up_and_false_while_open(self):
        r, w = os.pipe()
        try:
            assert fifo_writer._stdin_closed(r) is False
            os.close(w)
            w = None
            assert fifo_writer._stdin_closed(r) is True
        finally:
            os.close(r)
            if w is not None:
                os.close(w)


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

    def test_subprocess_with_no_reader_exits_when_its_pane_closes_stdin(self, tmp_path):
        """The orphan this guards against: a writer stuck in open() after its pane is gone."""
        fifo = tmp_path / "t.fifo"
        os.mkfifo(fifo, 0o600)
        proc = self._spawn(fifo)
        try:
            _, err = proc.communicate(b"pane output with no reader\n", timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        assert proc.returncode == 0, err

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFOs")
    def test_subprocess_waiting_with_unread_stdin_does_not_spin(self, tmp_path):
        """Pane output waiting in stdin, stdin still open, no reader: the wait must sleep.

        Readable data could make a hang-up poll return at once on a platform that
        reports readable data to it, so a wait built on that poll alone would retry
        the open in a busy loop for as long as the pane lives.
        """
        import resource
        import time

        fifo = tmp_path / "t.fifo"
        os.mkfifo(fifo, 0o600)
        proc = self._spawn(fifo)
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            proc.stdin.write(b"pane output nobody is reading yet\n")
            proc.stdin.flush()
            time.sleep(2.0)
            assert proc.poll() is None, "the writer must keep waiting while stdin is open"
            proc.stdin.close()
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        print(f"writer CPU over a 2 s wait with unread stdin: {cpu:.3f} s")
        assert proc.returncode == 0
        assert cpu < 0.2, f"the writer used {cpu:.3f} s of CPU in a 2 s wait"

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

    @pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX sockets")
    def test_subprocess_refuses_a_socket_at_the_path(self):
        """A socket is refused, never waited on as if it were a FIFO with no reader."""
        import shutil
        import socket
        import tempfile

        # Short directory: tmp_path can exceed the 104-byte AF_UNIX path limit on macOS.
        short_dir = tempfile.mkdtemp(prefix="cao-fw-", dir="/tmp")
        path = os.path.join(short_dir, "t.fifo")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(path)
            proc = self._spawn(path)
            _, err = proc.communicate(b"output for a socket\n", timeout=10)
        finally:
            sock.close()
            shutil.rmtree(short_dir, ignore_errors=True)
        assert proc.returncode == 1, err
        assert b"fifo_writer" in err

    def test_module_has_no_project_imports(self):
        """Started by file path on every pipe re-arm: it must not pull the package in."""
        source = open(fifo_writer.__file__, encoding="utf-8").read()
        assert "cli_agent_orchestrator" not in source.replace(
            "``cli_agent_orchestrator`` package", ""
        )
        mode = os.stat(fifo_writer.__file__).st_mode
        assert stat.S_ISREG(mode)
