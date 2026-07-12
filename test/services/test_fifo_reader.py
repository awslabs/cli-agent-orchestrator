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


class TestReaderLoopCoalescing:
    """Tests for chunk coalescing in _reader_loop.

    kiro-cli's TUI animates a spinner at ~10 fps and each frame is a separate
    FIFO write. Publishing one event per raw read floods the shared async
    queue (1024 slots, drop-on-full) and wipes out worker state transitions
    that assign/handoff rely on. The reader batches chunks arriving within
    _COALESCE_WINDOW into one publish.
    """

    def test_rapid_writes_produce_fewer_publishes_than_writes(self, tmp_path, monkeypatch):
        """10 back-to-back small writes must not produce 10 publishes.

        Regression guard for the queue-overflow bug that broke assign/handoff:
        each spinner frame was a separate publish, dropping worker completion
        events on the floor.
        """
        import threading
        import time
        from unittest.mock import patch

        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)

        fifo_path = tmp_path / "term-coalesce.fifo"
        os.mkfifo(fifo_path)

        published: list[dict] = []

        def fake_publish(topic, payload):
            published.append({"topic": topic, "data": payload["data"]})

        stop_flag = threading.Event()
        with patch(
            "cli_agent_orchestrator.services.fifo_reader.bus.publish",
            side_effect=fake_publish,
        ):
            reader = threading.Thread(
                target=FifoManager._reader_loop,
                args=("term-coalesce", fifo_path, stop_flag),
                daemon=True,
            )
            reader.start()

            # Give reader time to open its fds.
            time.sleep(0.1)

            # Simulate spinner-frame bursts: 10 tiny writes within one window,
            # separated by short pauses that mimic ~100Hz TUI redraws.
            wfd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            try:
                for i in range(10):
                    os.write(wfd, f"frame-{i}".encode())
                    time.sleep(0.002)  # 2ms — well below the coalesce window
                # Let the reader flush.
                time.sleep(0.2)
            finally:
                os.close(wfd)
                stop_flag.set()
                reader.join(timeout=2.0)

        # 10 writes must NOT produce 10 publishes.
        assert len(published) < 10, (
            f"Coalescing failed: got {len(published)} publishes for 10 writes. "
            f"Expected fewer than 10 (ideally 1-3)."
        )
        # All the bytes must still get through, in order.
        combined = "".join(p["data"] for p in published)
        assert combined == "".join(f"frame-{i}" for i in range(10))
        # All publishes are on the right topic.
        assert all(p["topic"] == "terminal.term-coalesce.output" for p in published)

    def test_pending_flushes_on_writer_idle(self, tmp_path, monkeypatch):
        """When the writer pauses, pending data flushes within one window.

        Otherwise kiro's TUI pausing between spinner frames or waiting for an
        LLM response would strand bytes in the pending buffer, leaving the
        status monitor with a stale view.
        """
        import threading
        import time
        from unittest.mock import patch

        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)

        fifo_path = tmp_path / "term-flush.fifo"
        os.mkfifo(fifo_path)

        published: list[dict] = []

        def fake_publish(topic, payload):
            published.append({"topic": topic, "data": payload["data"]})

        stop_flag = threading.Event()
        with patch(
            "cli_agent_orchestrator.services.fifo_reader.bus.publish",
            side_effect=fake_publish,
        ):
            reader = threading.Thread(
                target=FifoManager._reader_loop,
                args=("term-flush", fifo_path, stop_flag),
                daemon=True,
            )
            reader.start()

            time.sleep(0.1)

            # One write, then long silence — the coalesce timer must trigger
            # a flush without needing a follow-up write.
            wfd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(wfd, b"lonely-chunk")
                # Wait well beyond the coalesce window.
                time.sleep(0.3)
            finally:
                os.close(wfd)
                stop_flag.set()
                reader.join(timeout=2.0)

        assert len(published) >= 1, "Pending data was never flushed"
        combined = "".join(p["data"] for p in published)
        assert "lonely-chunk" in combined
