"""Tests for the FIFO reader manager."""

import os

import pytest

from cli_agent_orchestrator.services.fifo_reader import FifoManager

pytestmark = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="FIFOs require a POSIX platform (os.mkfifo)"
)


class TestStopReader:
    """Tests for FifoManager.stop_reader() cleanup."""

    def test_unlinks_stale_fifo_without_in_memory_reader(self, tmp_path, monkeypatch):
        """stop_reader removes a stale FIFO file even when no reader thread is
        tracked for the terminal.

        Regression for the PR #273 review: retention cleanup iterates DB
        terminals after a server restart, when ``_readers`` is empty. The old
        early-return skipped the unlink, leaking ``*.fifo`` files unbounded.
        """
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()

        fifo_path = tmp_path / "term-stale.fifo"
        os.mkfifo(fifo_path)
        assert fifo_path.exists()

        # No create_reader() was called, so _readers/_threads are empty.
        manager.stop_reader("term-stale")

        assert not fifo_path.exists()

    def test_is_noop_when_nothing_to_clean(self, tmp_path, monkeypatch):
        """stop_reader is safe when there is neither a tracked reader nor a
        FIFO file on disk."""
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()

        # Must not raise even though there is nothing to stop or unlink.
        manager.stop_reader("term-missing")


class TestReaderThreadLifecycle:
    """Issue #382 regressions: reader threads must never leak, no matter when
    stop_reader is called relative to writer activity. The old blocking-open
    loop stranded threads in the kernel's ``wait_for_partner`` whenever the
    stop-time wakeup missed the reader's reopen window; leaked threads
    accumulated across create/delete cycles until the server wedged."""

    def _manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        return FifoManager()

    def _thread(self, manager, terminal_id):
        with manager._lock:
            return manager._threads.get(terminal_id)

    def test_stop_with_no_writer_ever_attached_does_not_leak(self, tmp_path, monkeypatch):
        """The #382 leak case: reader parked with no writer, then stopped.

        The old loop blocked inside ``open(O_RDONLY)`` here; if the wakeup
        raced, join timed out and the unlink stranded the thread forever."""
        manager = self._manager(tmp_path, monkeypatch)
        manager.create_reader("term-nolock")
        thread = self._thread(manager, "term-nolock")
        assert thread is not None and thread.is_alive()

        manager.stop_reader("term-nolock")

        thread.join(timeout=3.0)
        assert not thread.is_alive()
        assert not (tmp_path / "term-nolock.fifo").exists()

    def test_stop_right_after_writer_eof_does_not_leak(self, tmp_path, monkeypatch):
        """The race window of the old design: a writer connects and disconnects
        (EOF pulse — what stop_pipe_pane produces) immediately before
        stop_reader. The old loop was mid-reopen at that point and the wakeup
        open failed with ENXIO, leaking the thread."""
        manager = self._manager(tmp_path, monkeypatch)
        manager.create_reader("term-race")
        fifo_path = tmp_path / "term-race.fifo"

        # Writer attaches and detaches, like tmux tearing down pipe-pane.
        wfd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(wfd)

        thread = self._thread(manager, "term-race")
        manager.stop_reader("term-race")

        thread.join(timeout=3.0)
        assert not thread.is_alive()
        assert not fifo_path.exists()

    def test_data_received_across_writer_reconnects(self, tmp_path, monkeypatch):
        """Chunks written by successive writers (tmux re-attaching pipe-pane)
        are all published; writer disconnects must not kill the reader."""
        import time

        from cli_agent_orchestrator.services import fifo_reader as fr

        received = []
        monkeypatch.setattr(
            fr.bus, "publish", lambda topic, data: received.append((topic, data["data"]))
        )
        manager = self._manager(tmp_path, monkeypatch)
        manager.create_reader("term-data")
        fifo_path = tmp_path / "term-data.fifo"

        for payload in (b"first", b"second"):
            wfd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            os.write(wfd, payload)
            os.close(wfd)
            # Wait for the reader's select loop to pick the chunk up.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not any(
                payload.decode() in d for _, d in received
            ):
                time.sleep(0.02)

        manager.stop_reader("term-data")

        data = "".join(d for _, d in received)
        assert "first" in data
        assert "second" in data
        assert all(t == "terminal.term-data.output" for t, _ in received)

    def test_repeated_create_stop_cycles_leave_no_threads(self, tmp_path, monkeypatch):
        """Accumulation guard: the #382 report showed 26+ leaked reader threads
        after repeated session create/delete cycles."""
        import threading as _threading

        manager = self._manager(tmp_path, monkeypatch)
        threads = []
        for i in range(5):
            tid = f"term-cycle{i}"
            manager.create_reader(tid)
            threads.append(self._thread(manager, tid))
            manager.stop_reader(tid)

        for t in threads:
            t.join(timeout=3.0)
        assert all(not t.is_alive() for t in threads)
        leftover = [t.name for t in _threading.enumerate() if t.name.startswith("fifo-term-cycle")]
        assert leftover == []
