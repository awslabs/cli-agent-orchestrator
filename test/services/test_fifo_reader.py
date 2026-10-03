"""Tests for the FIFO reader manager."""

import os
import stat
import threading
import time
from unittest.mock import patch

import pyte
import pytest

from cli_agent_orchestrator.services import fifo_reader as fr
from cli_agent_orchestrator.services.fifo_reader import FifoManager, _open_fifo

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
        assert manager.stop_reader("term-stale") is True

        assert not fifo_path.exists()

    def test_is_noop_when_nothing_to_clean(self, tmp_path, monkeypatch):
        """stop_reader is safe when there is neither a tracked reader nor a
        FIFO file on disk."""
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()

        # Must not raise even though there is nothing to stop or unlink.
        assert manager.stop_reader("term-missing") is True

    def test_unlink_failure_remains_retryable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        manager = FifoManager()
        fifo_path = tmp_path / "term-unlink-retry.fifo"
        os.mkfifo(fifo_path)

        with patch.object(type(fifo_path), "unlink", side_effect=PermissionError("busy")):
            assert manager.stop_reader("term-unlink-retry") is False

        assert fifo_path.exists()
        assert manager.stop_reader("term-unlink-retry") is True
        assert not fifo_path.exists()

    @pytest.mark.parametrize("blocked_at", ["read", "publish"])
    def test_retries_track_reader_until_thread_exits(self, tmp_path, monkeypatch, blocked_at):
        """A timed-out stop must stay incomplete until the real FIFO reader exits."""
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        manager = FifoManager()
        monkeypatch.setattr(manager, "_ensure_watchdog", lambda: None)
        blocked = threading.Event()
        release = threading.Event()
        real_read = os.read

        def wait_for_release():
            blocked.set()
            assert release.wait(timeout=5.0)

        def delayed_read(fd, size):
            data = real_read(fd, size)
            if blocked_at == "read":
                wait_for_release()
            return data

        def delayed_publish(topic, payload):
            if blocked_at == "publish":
                wait_for_release()

        monkeypatch.setattr(fr.os, "read", delayed_read)
        monkeypatch.setattr(fr.bus, "publish", delayed_publish)
        terminal_id = "term-stop-retry"
        manager.create_reader(terminal_id, pane_probe=lambda: "pane", rearm=lambda: None)
        thread = manager._threads[terminal_id]
        stop_flag = manager._readers[terminal_id]
        real_join = thread.join
        # Keep the regression fast while still joining the actual live thread.
        monkeypatch.setattr(thread, "join", lambda timeout=None: real_join(timeout=0.01))
        fifo_path = tmp_path / f"{terminal_id}.fifo"
        writer = os.open(fifo_path, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(writer, b"last output")
            assert blocked.wait(timeout=2.0)

            assert manager.stop_reader(terminal_id) is False
            assert manager.stop_reader(terminal_id) is False
            assert thread.is_alive()
            assert manager._threads[terminal_id] is thread
            assert manager._readers[terminal_id] is stop_flag
            assert stop_flag.is_set()
            assert not fifo_path.exists()
            for state in (
                manager._pane_probe,
                manager._rearm,
                manager._liveness,
                manager._last_data_at,
                manager._registered_at,
                manager._ever_delivered,
                manager._cold_start_attempts,
                manager._rearm_failures,
                manager._probe_failures,
            ):
                assert terminal_id not in state

            # The pending generation still owns this ID, even after unlink.
            manager.create_reader(terminal_id)
            assert manager._threads[terminal_id] is thread
            assert not fifo_path.exists()

            release.set()
            real_join(timeout=2.0)
            assert not thread.is_alive()
            # A read that completes after stop must not revive watchdog state.
            assert terminal_id not in manager._last_data_at
            assert terminal_id not in manager._ever_delivered
            assert manager.stop_reader(terminal_id) is True
            assert terminal_id not in manager._readers
            assert terminal_id not in manager._threads

            manager.create_reader(terminal_id)
            assert manager._threads[terminal_id] is not thread
            assert fifo_path.exists()
        finally:
            release.set()
            os.close(writer)
            real_join(timeout=2.0)
            manager.stop_reader(terminal_id)

    def test_concurrent_stop_does_not_remove_replacement(self, tmp_path, monkeypatch):
        """A late stop of the previous generation cannot unlink a replacement FIFO."""
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        manager = FifoManager()
        manager.create_reader("term-replaced")
        old_thread = manager._threads["term-replaced"]
        real_join = old_thread.join
        joined = threading.Event()
        release = threading.Event()
        results = []

        def delayed_join(timeout=None):
            real_join(timeout=timeout)
            if threading.current_thread() is stopper:
                joined.set()
                assert release.wait(timeout=5.0)

        monkeypatch.setattr(old_thread, "join", delayed_join)
        stopper = threading.Thread(
            target=lambda: results.append(manager.stop_reader("term-replaced")), daemon=True
        )
        try:
            stopper.start()
            assert joined.wait(timeout=3.0)
            assert not old_thread.is_alive()
            assert manager.stop_reader("term-replaced") is True
            manager.create_reader("term-replaced")
            new_thread = manager._threads["term-replaced"]
            new_stop_flag = manager._readers["term-replaced"]
            fifo_path = tmp_path / "term-replaced.fifo"
            inode = fifo_path.stat().st_ino

            release.set()
            stopper.join(timeout=2.0)

            assert not stopper.is_alive()
            assert results == [False]
            assert manager._threads["term-replaced"] is new_thread
            assert manager._readers["term-replaced"] is new_stop_flag
            assert not new_stop_flag.is_set()
            assert fifo_path.stat().st_ino == inode
            assert "term-replaced" in manager._last_data_at
        finally:
            release.set()
            stopper.join(timeout=3.0)
            manager.stop_reader("term-replaced")


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
        leftover = [t.name for t in threading.enumerate() if t.name.startswith("fifo-term-cycle")]
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
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)

        fifo_path = tmp_path / "term-coalesce.fifo"
        os.mkfifo(fifo_path)

        published: list[dict] = []

        def fake_publish(topic, payload):
            published.append({"topic": topic, "data": payload["data"]})

        # _reader_loop is an instance method (it records per-terminal liveness
        # on self for the #388 watchdog), so it needs a bound manager, not the
        # bare unbound function.
        manager = FifoManager()
        stop_flag = threading.Event()
        with patch(
            "cli_agent_orchestrator.services.fifo_reader.bus.publish",
            side_effect=fake_publish,
        ):
            reader = threading.Thread(
                target=manager._reader_loop,
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
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)

        fifo_path = tmp_path / "term-flush.fifo"
        os.mkfifo(fifo_path)

        published: list[dict] = []

        def fake_publish(topic, payload):
            published.append({"topic": topic, "data": payload["data"]})

        # _reader_loop is an instance method (it records per-terminal liveness
        # on self for the #388 watchdog), so it needs a bound manager, not the
        # bare unbound function.
        manager = FifoManager()
        stop_flag = threading.Event()
        with patch(
            "cli_agent_orchestrator.services.fifo_reader.bus.publish",
            side_effect=fake_publish,
        ):
            reader = threading.Thread(
                target=manager._reader_loop,
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

    @pytest.mark.parametrize("tail", [b"\x97\x8f done\n", b""])
    def test_utf8_split_across_batches_and_stop_flush(self, tmp_path, monkeypatch, tail):
        fifo_path = tmp_path / "term-utf8.fifo"
        os.mkfifo(fifo_path)
        monkeypatch.setattr(fr, "_COALESCE_MAX_BYTES", 1)
        monkeypatch.setattr(fr, "_POLL_INTERVAL", 0.01)
        received = []
        first = threading.Event()
        second = threading.Event()

        def publish(topic, payload):
            received.append(payload["data"])
            first.set()
            if "done" in payload["data"]:
                second.set()

        manager = FifoManager()
        stop_flag = threading.Event()
        reader = threading.Thread(
            target=manager._reader_loop, args=("term-utf8", fifo_path, stop_flag), daemon=True
        )
        with patch.object(fr.bus, "publish", side_effect=publish):
            reader.start()
            writer = os.open(fifo_path, os.O_WRONLY)
            try:
                os.write(writer, b"final \xe2")
                assert first.wait(2)
                assert received == ["final "]
                if tail:
                    os.write(writer, tail)
                    assert second.wait(2)
            finally:
                os.close(writer)
                stop_flag.set()
                reader.join(2)
        assert not reader.is_alive()
        assert "".join(received) == (b"final \xe2" + tail).decode("utf-8", errors="replace")


class TestPipeLivenessWatchdog:
    """Issue #388: tmux's pipe-pane forwarder can silently stop delivering bytes
    to the FIFO after an alternate-screen redraw burst — the pane keeps
    rendering (visible via capture-pane) but the piped copy freezes, so the FIFO
    reader, the StatusMonitor buffer, and GET /terminals/{id}/output stall on
    stale content indefinitely. The watchdog compares tmux's live pane content
    against whether the FIFO delivered any bytes and re-arms a stalled forwarder.

    These drive ``_check_pipe_liveness`` directly (no real tmux, no timing) so
    the detect/idle/healthy branches are deterministic. Enrollment/threading is
    covered separately below.
    """

    def _manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        return FifoManager()

    def _enroll(
        self,
        manager,
        terminal_id,
        pane_holder,
        rearm_calls,
        last_data_at,
        fifo_buffer=None,
    ):
        """Register a terminal with fake probe/rearm WITHOUT starting the reader
        thread or the real watchdog — we call _check_pipe_liveness by hand.

        The FIFO buffer defaults to the raw-stream shape tmux pipe-pane really
        delivers: CRLF line ends, SGR runs and partial-redraw control
        sequences, not the pane's rendered text.
        """
        if fifo_buffer is None:
            fifo_buffer = {
                "content": "\x1b[32m" + pane_holder["content"].replace("\n", "\r\n") + "\x1b[0m"
            }
        manager._pane_probe[terminal_id] = lambda: pane_holder["content"]
        manager._rearm[terminal_id] = lambda: rearm_calls.append(True)
        manager._last_data_at[terminal_id] = last_data_at
        if not hasattr(manager, "_fifo_buffer_probe"):
            manager._fifo_buffer_probe = {}
        manager._fifo_buffer_probe[terminal_id] = lambda: fifo_buffer["content"]

    def test_stall_is_detected_and_pipe_rearmed(self, tmp_path, monkeypatch):
        """Pane advanced but the FIFO delivered nothing since the last check ->
        the forwarder is stalled and gets re-armed once PIPE_LIVENESS_STALL_CHECKS
        consecutive checks confirm it (monkeypatched to 1 here to isolate the
        detect/re-arm decision from the default-threshold behavior, which
        test_stall_requires_configured_consecutive_checks covers separately)."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 1)
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "line0"}
        rearm_calls: list = []
        # FIFO's last delivery is in the past — it has been silent.
        self._enroll(manager, "term", pane, rearm_calls, last_data_at=time.monotonic())

        # First check just establishes a baseline, never acts.
        manager._check_pipe_liveness("term")
        assert rearm_calls == []

        # Pane advances (redraw) while the FIFO stays silent -> stall.
        pane["content"] = "line0\nline1 (rendered but never piped)"
        manager._check_pipe_liveness("term")
        assert rearm_calls == [True], "a stalled pipe-pane forwarder must be re-armed"

    def test_idle_terminal_is_not_rearmed(self, tmp_path, monkeypatch):
        """Pane unchanged + FIFO silent = a genuinely idle terminal, which must
        NOT trigger a needless re-pipe (the false-positive guard)."""
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "idle prompt"}
        rearm_calls: list = []
        self._enroll(manager, "term", pane, rearm_calls, last_data_at=time.monotonic())

        for _ in range(4):
            manager._check_pipe_liveness("term")  # pane never changes
        assert rearm_calls == [], "an idle terminal must never be re-armed"

    def test_healthy_pipe_is_not_rearmed(self, tmp_path, monkeypatch):
        """Raw FIFO delivery must not cause strikes against capture-pane rows,
        either during sustained redraws or after the terminal settles."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 2)
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "prompt\nline0"}
        fifo_buffer = {"content": "prompt\r\n\x1b[32mline0\x1b[0m"}
        rearm_calls: list = []
        calls = {"n": 0}

        def _flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("pyte mid-redraw")
            return None

        self._enroll(
            manager,
            "term",
            pane,
            rearm_calls,
            last_data_at=time.monotonic(),
            fifo_buffer=fifo_buffer,
        )
        manager._fifo_screen_probe["term"] = _flaky

        manager._check_pipe_liveness("term")
        strikes = []
        for i in range(1, 9):
            pane["content"] = f"prompt\nline{i}"
            fifo_buffer["content"] += f"\r\x1b[2K\x1b[32mline{i}\x1b[0m"
            manager._last_data_at["term"] = time.monotonic()
            manager._check_pipe_liveness("term")
            strikes.append(manager._liveness["term"][2])
        for _ in range(4):
            manager._check_pipe_liveness("term")
            strikes.append(manager._liveness["term"][2])

        assert (strikes, rearm_calls) == ([0] * 12, [])
        assert manager._liveness["term"][0] == "prompt\nline8"

        # _flaky returns None after its raise, so every tick above ran on the
        # buffer tier; one tick with a healthy screen probe closes the healthy
        # path on the screen tier too
        manager._fifo_screen_probe["term"] = lambda: pane["content"]
        pane["content"] = "prompt\nline9"
        manager._last_data_at["term"] = time.monotonic()
        manager._check_pipe_liveness("term")
        assert manager._liveness["term"][0] == "prompt\nline9"
        assert manager._liveness["term"][2] == 0
        assert rearm_calls == []

    def test_rearm_replays_live_pane_into_pipeline(self, tmp_path, monkeypatch):
        """After re-arm the lost bytes are gone, but the pane's CURRENT content
        is republished so the StatusMonitor buffer / GET output stop being frozen
        instead of waiting for the agent to emit something new."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 1)
        published: list = []
        monkeypatch.setattr(fr.bus, "publish", lambda topic, data: published.append((topic, data)))

        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "before"}
        rearm_calls: list = []
        self._enroll(manager, "term", pane, rearm_calls, last_data_at=time.monotonic())

        manager._check_pipe_liveness("term")  # baseline
        pane["content"] = "current live screen the pipe never forwarded"
        manager._check_pipe_liveness("term")

        assert rearm_calls == [True]
        # Single-line content has no "\n" to CRLF-convert; the payload is the
        # pane content with only the leading line separator the replay adds so
        # it cannot glue onto a stale mid-line buffer tail. The multi-line
        # CRLF conversion itself is covered by test_rearm_replay_converts_lf_to_crlf.
        assert ("terminal.term.output", {"data": "\r\n" + pane["content"]}) in published

    def test_rearm_replay_converts_lf_to_crlf(self, tmp_path, monkeypatch):
        """Regression for the round-2 review finding: capture-pane joins lines
        with a bare "\\n" (clients/tmux.py's get_history), which pyte treats as
        linefeed-without-carriage-return (LNM off by default) — replaying that
        verbatim staircases the composited screen, so status detection for
        CAO_PYTE_STATUS providers (claude_code, #388's own repro target) can
        stay broken after the very re-arm meant to fix it. The replay must
        convert each bare "\\n" to "\\r\\n" so pyte renders it as a real newline."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 1)
        published: list = []
        monkeypatch.setattr(fr.bus, "publish", lambda topic, data: published.append((topic, data)))

        manager = self._manager(tmp_path, monkeypatch)
        multiline = "line0\nline1\nline2 (rendered but never piped)"
        pane = {"content": "line0"}
        rearm_calls: list = []
        self._enroll(manager, "term", pane, rearm_calls, last_data_at=time.monotonic())

        manager._check_pipe_liveness("term")  # baseline
        pane["content"] = multiline
        manager._check_pipe_liveness("term")

        assert rearm_calls == [True]
        assert (
            "terminal.term.output",
            {"data": "\r\n" + multiline.replace("\n", "\r\n")},
        ) in published
        # And the raw bare-LF form must NOT have been published — that's
        # exactly the payload that staircases pyte's screen.
        assert ("terminal.term.output", {"data": multiline}) not in published

    def test_pyte_screen_staircases_on_bare_lf_and_is_fixed_by_crlf(self):
        """End-to-end confirmation (not just string equality) that bare "\\n"
        actually breaks pyte's composited screen the way the review claimed,
        and that "\\r\\n" fixes it — using the real pyte library, not a mock."""
        multiline = "root@host:~$ line one\nroot@host:~$ line two\nroot@host:~$ line three"

        bare_screen = pyte.Screen(80, 24)
        pyte.Stream(bare_screen).feed(multiline)
        bare_lines = [line for line in bare_screen.display if line.strip()]

        crlf_screen = pyte.Screen(80, 24)
        pyte.Stream(crlf_screen).feed(multiline.replace("\n", "\r\n"))
        crlf_lines = [line for line in crlf_screen.display if line.strip()]

        # With bare "\n" (LNM off), each line's cursor column carries over
        # from the previous line's end instead of returning to 0, so
        # "line two"/"line three" render indented ("staircased") rather than
        # left-aligned like the first line.
        assert not all(line.startswith("root@host") for line in bare_lines), (
            "expected the bare-LF replay to staircase — if this now passes, "
            "pyte's LNM default changed and the CRLF fix may be unnecessary"
        )
        # With "\r\n" every line starts at column 0, exactly like a real
        # terminal rendering tmux's own output.
        assert all(
            line.startswith("root@host") for line in crlf_lines
        ), "CRLF-converted replay must render each line left-aligned"

    def test_stall_requires_configured_consecutive_checks(self, tmp_path, monkeypatch):
        """With PIPE_LIVENESS_STALL_CHECKS > 1, a single diverging check is not
        enough — re-arm waits for the configured number of consecutive stalls."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 2)
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "l0"}
        rearm_calls: list = []
        self._enroll(manager, "term", pane, rearm_calls, last_data_at=time.monotonic())

        manager._check_pipe_liveness("term")  # baseline
        pane["content"] = "l1"
        manager._check_pipe_liveness("term")  # strike 1 — not yet
        assert rearm_calls == []
        pane["content"] = "l2"
        manager._check_pipe_liveness("term")  # strike 2 — re-arm
        assert rearm_calls == [True]

    def test_burst_then_settle_stall_is_still_detected(self, tmp_path, monkeypatch):
        """Regression for harness-control#148's live repro: a stall that occurs
        mid-burst and then settles into a NEW static frame before the next poll
        (e.g. an AskUserQuestion menu finishing its render and sitting idle) must
        still be caught, not just an ongoing/ever-changing divergence.

        Before the fix, ``_check_pipe_liveness`` compared each check's content
        only against the IMMEDIATELY PREVIOUS check's content. That works for a
        stall that's actively churning while polled, but a single clean
        transition into a new static frame produced exactly one diverging check
        (baseline -> new frame) followed by unchanging checks thereafter (new
        frame -> same new frame each time), resetting the strike counter to 0
        forever — even though the FIFO-fed buffer was permanently stuck on stale
        mid-burst content. The fix pins the comparison baseline to the last
        known-healthy content (not the previous check) so a settle-and-freeze
        keeps accumulating strikes exactly like an ongoing divergence would.
        """
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 2)  # production default
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "idle prompt"}
        rearm_calls: list = []
        self._enroll(manager, "term", pane, rearm_calls, last_data_at=time.monotonic())

        manager._check_pipe_liveness("term")  # baseline
        assert rearm_calls == []

        # The pipe silently stalls right now (no further last_data_at bumps in
        # this test — the FIFO never delivers anything again). The pane renders
        # a burst of redraws that settles into ONE new static frame before the
        # next poll observes it — a single clean transition, not an ongoing
        # divergence.
        pane["content"] = "1) yes\n2) no\n3) cancel  (menu fully rendered, now static)"

        manager._check_pipe_liveness("term")  # strike 1: diverged from baseline
        assert rearm_calls == [], "a single diverging check must not re-arm yet"

        # Pane is now static — unchanged from the previous check — but still
        # diverged from the pre-stall baseline. This is the exact shape that
        # evaded detection pre-fix.
        manager._check_pipe_liveness("term")  # strike 2: still diverged -> re-arm
        assert rearm_calls == [True], (
            "a stall that settles into a new static frame after one clean "
            "transition must still be re-armed once the configured number of "
            "checks confirm the divergence persists"
        )

        # And it must not keep re-arming on every subsequent check once healthy
        # again (rearm() bumps _last_data_at in the real code path via the
        # replay publish; simulate that here).
        manager._last_data_at["term"] = time.monotonic()
        manager._check_pipe_liveness("term")
        assert rearm_calls == [True], "must not spuriously re-arm again once healthy"

    def test_stop_during_probe_does_not_resurrect_state(self, tmp_path, monkeypatch):
        """Regression for the round-2 review's stop-during-probe race:
        ``_check_pipe_liveness`` calls the injected ``probe()`` (a slow tmux
        ``capture-pane``) without holding the lock. If ``stop_reader()`` pops a
        terminal's watchdog state while that call is in flight, the check must
        not write ``_liveness``/``_last_data_at`` back afterward — the old
        unconditional write-back resurrected entries for a terminal that
        ``_watchdog_loop`` (which only iterates ``_pane_probe``) would never
        revisit again, leaking them for process lifetime across create/stop
        churn."""
        manager = self._manager(tmp_path, monkeypatch)

        def slow_probe():
            # Simulate stop_reader() completing atomically, under its own
            # lock, while this capture-pane call is still in flight.
            manager._pane_probe.pop("term", None)
            manager._rearm.pop("term", None)
            manager._liveness.pop("term", None)
            manager._last_data_at.pop("term", None)
            return "content"

        manager._pane_probe["term"] = slow_probe
        manager._rearm["term"] = lambda: None
        manager._liveness["term"] = ("previous content", 0.0, 0)
        manager._last_data_at["term"] = 0.0

        manager._check_pipe_liveness("term")

        assert "term" not in manager._liveness, "stopped terminal's state must not be resurrected"
        assert (
            "term" not in manager._last_data_at
        ), "stopped terminal's state must not be resurrected"

    def test_rearm_failures_capped_and_terminal_dropped(self, tmp_path, monkeypatch):
        """A rearm() that keeps raising must not retry forever: after
        PIPE_LIVENESS_MAX_REARM_FAILURES consecutive failures the terminal is
        dropped from the watchdog instead of re-striking and logging a
        WARNING/exception every cycle indefinitely."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 1)
        monkeypatch.setattr(fr, "PIPE_LIVENESS_MAX_REARM_FAILURES", 3)

        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "l0"}
        rearm_calls: list = []

        def failing_rearm():
            rearm_calls.append(True)
            raise RuntimeError("tmux pane gone")

        manager._pane_probe["term"] = lambda: pane["content"]
        manager._rearm["term"] = failing_rearm
        manager._last_data_at["term"] = time.monotonic()

        manager._check_pipe_liveness("term")  # baseline

        for i in range(1, 4):
            pane["content"] = f"l{i}"
            manager._check_pipe_liveness("term")

        assert len(rearm_calls) == 3, "must stop attempting after the failure cap"
        assert "term" not in manager._pane_probe, "terminal must be dropped after repeated failures"
        assert "term" not in manager._rearm
        assert "term" not in manager._rearm_failures

    def test_probe_failure_is_bounded_and_terminal_dropped(self, tmp_path, monkeypatch):
        """harness-control#845: when probe() itself raises (session/window/whole
        tmux server gone, e.g. libtmux ObjectDoesNotExist) the exception must NOT
        propagate to _watchdog_loop (which would log a full traceback per terminal
        per tick forever — the storm). Instead it is caught and bounded: after
        PIPE_LIVENESS_MAX_PROBE_FAILURES consecutive failures the terminal is
        dropped from the watchdog, once, with a single summary log."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_MAX_PROBE_FAILURES", 3)

        manager = self._manager(tmp_path, monkeypatch)
        probe_calls: list = []

        def gone_probe():
            probe_calls.append(True)
            raise RuntimeError(
                "No objects found: session gone"
            )  # mimics libtmux ObjectDoesNotExist

        manager._pane_probe["term"] = gone_probe
        manager._rearm["term"] = lambda: None
        manager._last_data_at["term"] = time.monotonic()

        # Each call must return normally (NOT raise) — this is the storm fix: the
        # exception is swallowed here so _watchdog_loop never logs a per-tick traceback.
        for _ in range(3):
            manager._check_pipe_liveness("term")  # must not raise

        assert len(probe_calls) == 3
        assert (
            "term" not in manager._pane_probe
        ), "gone terminal must be dropped after the probe-failure cap"
        assert "term" not in manager._rearm
        assert "term" not in manager._probe_failures

        # A further watchdog pass no longer probes it at all (storm over): the
        # terminal is unenrolled, so _watchdog_loop wouldn't even iterate it.
        manager._check_pipe_liveness("term")
        assert len(probe_calls) == 3, "a dropped terminal must never be probed again"

    def test_probe_failure_counter_resets_on_success(self, tmp_path, monkeypatch):
        """A brief transient probe failure (session momentarily unavailable but not
        gone) must not accumulate toward the cap across recoveries: a successful
        probe resets the counter, so the terminal is never falsely dropped."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_MAX_PROBE_FAILURES", 3)
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 1)

        manager = self._manager(tmp_path, monkeypatch)
        state = {"fail": True, "content": "l0"}

        def flaky_probe():
            if state["fail"]:
                raise RuntimeError("transient: No objects found")
            return state["content"]

        manager._pane_probe["term"] = flaky_probe
        manager._rearm["term"] = lambda: None
        manager._last_data_at["term"] = time.monotonic()

        # Two failures (below the cap of 3), then a success.
        manager._check_pipe_liveness("term")
        manager._check_pipe_liveness("term")
        assert manager._probe_failures.get("term") == 2
        state["fail"] = False
        manager._check_pipe_liveness("term")  # success -> resets the counter
        assert "term" in manager._pane_probe, "must not be dropped after recovering"
        assert "term" not in manager._probe_failures, "counter must reset on a successful probe"

        # Two more failures must again NOT drop it (proves it didn't secretly carry 2+2).
        state["fail"] = True
        manager._check_pipe_liveness("term")
        manager._check_pipe_liveness("term")
        assert "term" in manager._pane_probe
        assert manager._probe_failures.get("term") == 2

    def test_create_reader_enrolls_and_starts_watchdog(self, tmp_path, monkeypatch):
        """A tmux caller passing probe+rearm enrolls the terminal and starts the
        watchdog; stop_reader unenrolls it and clears its liveness state."""
        manager = self._manager(tmp_path, monkeypatch)
        try:
            manager.create_reader(
                "term-enroll",
                pane_probe=lambda: "content",
                rearm=lambda: None,
            )
            assert "term-enroll" in manager._pane_probe
            assert "term-enroll" in manager._rearm
            assert manager._watchdog_thread is not None
            assert manager._watchdog_thread.is_alive()

            # no FIFO probes supplied: bytes-arrived alone decides health
            manager._liveness["term-enroll"] = ("line0", time.monotonic() - 5, 0)
            manager._last_data_at["term-enroll"] = time.monotonic()
            manager._check_pipe_liveness("term-enroll")
            assert manager._liveness["term-enroll"][2] == 0

            manager.stop_reader("term-enroll")
            assert "term-enroll" not in manager._pane_probe
            assert "term-enroll" not in manager._rearm
            assert "term-enroll" not in manager._liveness
        finally:
            manager.stop_watchdog()

    def test_screen_probe_compares_rendered_to_rendered(self, tmp_path, monkeypatch):
        """A composited pyte screen decides health rendered-to-rendered."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 2)
        manager = self._manager(tmp_path, monkeypatch)
        # the pane wraps at its width; the pyte grid is far wider, so the
        # same frame must match across different line boundaries
        pane = {"content": "a long line the pane wraps\nacross two rows"}
        screen = {"content": "scrollback\na long line the pane wraps across two rows"}
        rearm_calls: list = []
        self._enroll(
            manager,
            "term",
            pane,
            rearm_calls,
            last_data_at=time.monotonic(),
            fifo_buffer={"content": "raw bytes that match nothing"},
        )
        manager._fifo_screen_probe["term"] = lambda: screen["content"]

        manager._check_pipe_liveness("term")  # baseline
        pane["content"] = "a long line the pane wraps\nacross two rows now"
        screen["content"] = "scrollback\na long line the pane wraps across two rows now"
        manager._last_data_at["term"] = time.monotonic()
        manager._check_pipe_liveness("term")

        assert manager._liveness["term"][2] == 0, "screen match must re-baseline"
        assert manager._liveness["term"][0] == pane["content"]
        assert rearm_calls == []

    def test_screen_probe_matches_styled_and_soft_wrapped_pane(self, tmp_path, monkeypatch):
        """A pane tail with escapes and a soft wrap still matches the screen."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 2)
        manager = self._manager(tmp_path, monkeypatch)
        # capture-pane -e keeps escapes and wraps the row at the pane width
        # (80 + 40); the screen side holds the same content unwrapped, so the
        # match must survive both the escapes and the wrap boundary
        pane = {"content": "prompt"}
        screen = {"content": "scrollback\nprompt"}
        rearm_calls: list = []
        self._enroll(manager, "term", pane, rearm_calls, last_data_at=time.monotonic())
        manager._fifo_screen_probe["term"] = lambda: screen["content"]

        manager._check_pipe_liveness("term")  # baseline
        long = "x" * 120
        pane["content"] = "\x1b[32m" + long[:80] + "\x1b[0m\n" + long[80:]
        screen["content"] = "scrollback\n" + long
        manager._last_data_at["term"] = time.monotonic()
        manager._check_pipe_liveness("term")

        assert manager._liveness["term"][2] == 0, "soft wrap must not break the match"
        assert manager._liveness["term"][0] == pane["content"]
        assert rearm_calls == []

    def test_screen_probe_stale_frame_still_accumulates_strikes(self, tmp_path, monkeypatch):
        """#711: a burst arrives, the pane settles on a frame the FIFO never
        showed, and the pipe then goes silent. The delivering interval must
        withhold the re-baseline (re-baselining there would latch the never-
        shown frame and hide the stall, the pre-PR bug); the silent checks
        then strike against the preserved pre-stall baseline and re-arm."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 2)
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "prompt\nline0"}
        screen = {"content": "prompt\nline0"}
        rearm_calls: list = []
        self._enroll(
            manager,
            "term",
            pane,
            rearm_calls,
            last_data_at=time.monotonic(),
        )
        manager._fifo_screen_probe["term"] = lambda: screen["content"]

        manager._check_pipe_liveness("term")  # baseline: prompt/line0
        # Burst interval: bytes arrive but the screen never saw the settled
        # frame -> delivering, so no strike; the baseline must NOT move.
        pane["content"] = "prompt\nline9"
        manager._last_data_at["term"] = time.monotonic()
        manager._check_pipe_liveness("term")
        assert rearm_calls == []
        assert manager._liveness["term"][2] == 0
        assert manager._liveness["term"][0] == "prompt\nline0"
        # The pipe goes silent while the pane sits on the never-shown frame:
        # strikes accumulate against the preserved baseline and re-arm.
        manager._check_pipe_liveness("term")
        manager._check_pipe_liveness("term")

        assert rearm_calls == [True], "stale rendered frame is a stall"

    def test_screen_probe_real_pyte_display_matches_pane(self, tmp_path, monkeypatch):
        """The screen-tier probe must survive a real ``pyte.Screen`` display —
        fixed-width padded rows, trailing blank rows, wide grid — not just
        pre-collapsed one-line strings (round-4 review: the two existing
        'match' tests passed regardless of what _screen_lines produced)."""
        import pyte

        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 2)
        manager = self._manager(tmp_path, monkeypatch)
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("prompt $ \r\nstep one done\r\n")
        display = "\n".join(screen.display)

        pane = {"content": "prompt $ \nstep one done"}
        rearm_calls: list = []
        self._enroll(
            manager,
            "term",
            pane,
            rearm_calls,
            last_data_at=time.monotonic(),
        )
        manager._fifo_screen_probe["term"] = lambda: display

        manager._check_pipe_liveness("term")  # baseline
        stream.feed("step two done\r\n")
        display = "\n".join(screen.display)
        pane["content"] = "prompt $ \nstep one done\nstep two done"
        manager._last_data_at["term"] = time.monotonic()
        manager._check_pipe_liveness("term")

        assert manager._liveness["term"][2] == 0, "real pyte display must match"
        assert manager._liveness["term"][0] == pane["content"]
        assert rearm_calls == []

    def test_healthy_fallback_churn_with_real_capture_shapes_never_strikes(
        self, tmp_path, monkeypatch
    ):
        """Round-4 review repro, fallback tier (kiro_cli's only path): a busy
        pane whose two sides never compare byte-equal — capture-pane trims
        per-row trailing whitespace while the stream keeps it, a soft-wrapped
        row splits across pane rows but stays one line in the stream, a
        trailing blank viewport row — must accumulate ZERO strikes while
        bytes keep arriving. At the reviewed head this shape re-armed a
        delivering pipe on a cadence."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 2)
        manager = self._manager(tmp_path, monkeypatch)
        wrap = "y" * 120
        # capture-pane shape: rows joined by \n, per-row trailing spaces
        # trimmed, the 120-char row soft-wrapped 80+40, trailing blank row
        pane = {"content": f"sh-4.2$\n{wrap[:80]}\n{wrap[80:]}\nsh-4.2$\n"}
        rearm_calls: list = []
        # raw stream shape: CRLF line ends, trailing spaces kept, the long
        # row NOT wrapped in the stream, SGR noise around it
        fifo_buffer = {"content": f"sh-4.2$ \r\n\x1b[32m{wrap}\x1b[0m\r\nsh-4.2$ \r\n"}
        self._enroll(
            manager,
            "term",
            pane,
            rearm_calls,
            last_data_at=time.monotonic(),
            fifo_buffer=fifo_buffer,
        )

        manager._check_pipe_liveness("term")  # baseline
        strikes = []
        for i in range(1, 7):
            # churn: another command lands on the pane and in the stream,
            # same trimmed-vs-kept and soft-wrap shapes every tick
            pane["content"] = f"sh-4.2$\n{wrap[:80]}\n{wrap[80:]}\ncmd{i}\nsh-4.2$\n"
            fifo_buffer["content"] += f"\r\n\x1b[32mcmd{i}\x1b[0m\r\nsh-4.2$ \r\n"
            manager._last_data_at["term"] = time.monotonic()
            manager._check_pipe_liveness("term")
            strikes.append(manager._liveness["term"][2])

        assert strikes == [0] * 6, "a delivering pipe must never strike"
        assert rearm_calls == []

    def test_idle_after_persistent_mismatch_rearms_once_then_reconciles(
        self, tmp_path, monkeypatch
    ):
        """Round-5 review: a pane whose frame never reconciles while bytes
        flow goes silent, strikes into exactly ONE re-arm, and the replay's
        leading-CRLF publish must make the next check re-baseline with no
        further re-arm. A delivering interval also resets a silent strike."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 2)
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "sh-4.2$ make"}
        fifo_buffer = {"content": "\x1b[32mmake: running\x1b[0m\r\n"}
        rearm_calls: list = []

        # the replay lands in the rolling buffer the way StatusMonitor gets it
        def _publish(topic, data):
            fifo_buffer["content"] += data["data"]

        monkeypatch.setattr(fr.bus, "publish", _publish)
        self._enroll(
            manager,
            "term",
            pane,
            rearm_calls,
            last_data_at=time.monotonic(),
            fifo_buffer=fifo_buffer,
        )

        manager._check_pipe_liveness("term")  # baseline: sh-4.2$ make
        assert rearm_calls == []

        # delivering with a persistent mismatch: the pane cleared to a bare
        # prompt the stream never shows as its last row
        pane["content"] = "$"
        for i in range(2):
            fifo_buffer["content"] += f"more output {i}\r\n"
            manager._last_data_at["term"] = time.monotonic()
            manager._check_pipe_liveness("term")
        assert rearm_calls == []
        assert manager._liveness["term"][2] == 0, "delivering resets strikes"
        assert manager._liveness["term"][0] == "sh-4.2$ make"

        # idle: strikes against the preserved baseline, one re-arm
        manager._check_pipe_liveness("term")
        assert manager._liveness["term"][2] == 1
        manager._check_pipe_liveness("term")
        assert rearm_calls == [True], "the mismatch must cost exactly one re-arm"

        # the replay published the live pane behind a leading CRLF: the next
        # check reaches the frame by row anchor instead of striking again
        assert fifo_buffer["content"].endswith("\r\n$")
        manager._check_pipe_liveness("term")
        assert manager._liveness["term"][0] == "$", "post-replay check re-baselines"
        assert manager._liveness["term"][2] == 0
        for _ in range(3):
            manager._check_pipe_liveness("term")
        assert rearm_calls == [True], "a reconciled idle pane must not re-arm again"

        # a delivering interval after a silent strike resets the counter
        pane["content"] = "$ done"
        manager._check_pipe_liveness("term")  # silent: strike 1
        assert manager._liveness["term"][2] == 1
        fifo_buffer["content"] += "tail bytes\r\n"
        manager._last_data_at["term"] = time.monotonic()
        manager._check_pipe_liveness("term")  # delivering: evidence of life
        assert manager._liveness["term"][2] == 0, "byte arrival clears strikes"
        manager._check_pipe_liveness("term")  # silent: strike 1 again
        manager._check_pipe_liveness("term")  # silent: strike 2 -> re-arm
        assert rearm_calls == [True, True]

    def test_raising_buffer_probe_degrades_instead_of_aborting(self, tmp_path, monkeypatch):
        """A raising fifo_buffer_probe must not abort the check with a
        per-tick traceback — it degrades to the #388 bytes-arrived tier, the
        same way a raising screen probe falls back to the buffer."""
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "prompt"}
        rearm_calls: list = []
        self._enroll(
            manager,
            "term",
            pane,
            rearm_calls,
            last_data_at=time.monotonic(),
            fifo_buffer={"content": "anything"},
        )

        def _boom():
            raise RuntimeError("buffer probe exploded")

        manager._fifo_buffer_probe["term"] = _boom
        pane["content"] = "prompt\nnew line"
        manager._last_data_at["term"] = time.monotonic()
        manager._check_pipe_liveness("term")
        assert manager._liveness["term"][0] == "prompt\nnew line"
        assert rearm_calls == []

    def test_soft_wrapped_tail_advances_baseline_and_idle_does_not_strike(
        self, tmp_path, monkeypatch
    ):
        """Round-5 review, the kiro_cli dominant case: the pane wraps a wide
        row at its width while the stream holds the same bytes unwrapped, so
        rows can never align. The folded comparison must reach the frame, the
        baseline must advance during delivery, and the activity->idle
        transition that re-armed healthy terminals at the fifth head must
        produce zero strikes and zero re-arms."""
        manager = self._manager(tmp_path, monkeypatch)
        wrap = "y" * 120
        pane = {"content": "sh-4.2$"}
        fifo_buffer = {"content": "sh-4.2$ \r\n"}
        rearm_calls: list = []
        self._enroll(
            manager,
            "term",
            pane,
            rearm_calls,
            last_data_at=time.monotonic(),
            fifo_buffer=fifo_buffer,
        )

        manager._check_pipe_liveness("term")  # baseline: bare prompt
        # one wide output line lands: wrapped 80+40 on the pane, one
        # unwrapped SGR-wrapped line in the stream, then the prompt again
        pane["content"] = f"sh-4.2$\n{wrap[:80]}\n{wrap[80:]}\nsh-4.2$"
        fifo_buffer["content"] += f"\x1b[32m{wrap}\x1b[0m\r\nsh-4.2$ \r\n"
        manager._last_data_at["term"] = time.monotonic()
        manager._check_pipe_liveness("term")

        assert (
            manager._liveness["term"][0] == pane["content"]
        ), "the folded comparison must let the baseline advance through the wrap"
        for _ in range(3):
            manager._check_pipe_liveness("term")  # idle on the delivered frame
        assert manager._liveness["term"][2] == 0
        assert rearm_calls == []

    def test_screen_probe_short_pane_tail_cannot_match_stale_screen(self, tmp_path, monkeypatch):
        """Round-5 review: a bare prompt tail whose folded form is a suffix
        of stale screen content must NOT count as frame reached — the #711
        miss in the opposite direction. The stall that follows is still
        caught."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 2)
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "prompt $ build running"}
        screen = {"content": "step one done\nstep two$"}
        rearm_calls: list = []
        self._enroll(
            manager,
            "term",
            pane,
            rearm_calls,
            last_data_at=time.monotonic(),
            fifo_buffer={"content": "raw bytes that match nothing"},
        )
        manager._fifo_screen_probe["term"] = lambda: screen["content"]

        manager._check_pipe_liveness("term")  # baseline
        # delivering tick: the pane clears to a bare "$"; the stale screen's
        # folded text ends with "$" but its last row does not, and the folded
        # suffix is below the trust floor
        pane["content"] = "$"
        manager._last_data_at["term"] = time.monotonic()
        manager._check_pipe_liveness("term")
        assert (
            manager._liveness["term"][0] == "prompt $ build running"
        ), "a short tail must not latch a frame the FIFO never showed"

        # the pipe goes silent on the never-shown frame: still a caught stall
        manager._check_pipe_liveness("term")
        manager._check_pipe_liveness("term")
        assert rearm_calls == [True], "the coincidence must not hide the stall"

    def test_create_reader_without_callbacks_is_not_watched(self, tmp_path, monkeypatch):
        """Backward compat: callers that omit probe/rearm (or backends without
        pipe-pane) get the old behavior — no enrollment, no watchdog thread."""
        manager = self._manager(tmp_path, monkeypatch)
        manager.create_reader("term-plain")
        try:
            assert "term-plain" not in manager._pane_probe
            assert manager._watchdog_thread is None
        finally:
            manager.stop_reader("term-plain")


class TestColdStartStallDetection:
    """harness-control#93: the divergence check above can ONLY ever catch a
    pipe that WAS delivering and then stalled — it needs an established
    "healthy" baseline to diverge from. A pipe that has been dead since the
    terminal was created never gets one: an already-idle shell prompt renders
    once and never changes again, so every check sees identical content and
    "diverged_from_baseline" is permanently False — the watchdog could wait
    forever without ever re-arming, while wait_for_shell() times out (60s)
    waiting on a FIFO buffer that was never going to fill. Live-reproduced:
    a real tmux pane sitting on a stable, genuinely-ready shell prompt whose
    FIFO never delivered a single byte from the moment pipe-pane attached.

    This is a positive, independent check — not a variant of the divergence
    logic — checked BEFORE it: has the FIFO delivered anything since
    registration, within a short grace period? If not, and the pane already
    shows real content (ruling out "still genuinely booting"), re-arm
    immediately.
    """

    def _manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        return FifoManager()

    def _enroll_cold(self, manager, terminal_id, pane_holder, rearm_calls, registered_at):
        """Register a terminal exactly as create_reader() would for a pipe
        that has never delivered anything since ``registered_at``."""
        manager._pane_probe[terminal_id] = lambda: pane_holder["content"]
        manager._rearm[terminal_id] = lambda: rearm_calls.append(True)
        manager._last_data_at[terminal_id] = registered_at
        manager._registered_at[terminal_id] = registered_at
        manager._ever_delivered[terminal_id] = False

    def test_cold_start_stall_is_detected_and_rearmed_on_first_check(self, tmp_path, monkeypatch):
        """The defining case: a single, static, already-rendered pane (no
        divergence ever occurs) whose FIFO never delivered anything must
        still be re-armed — on the VERY FIRST check, unlike the steady-state
        path which always lets the first observation pass to establish a
        baseline. Requiring a second check here would just re-introduce the
        same "nothing ever changes" blind spot for a terminal whose pane
        genuinely never changes again after this."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_COLD_START_GRACE_S", 0.0)
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "user@host:~$ "}
        rearm_calls: list = []
        # Registered "in the past" relative to now — grace period already elapsed.
        self._enroll_cold(manager, "term", pane, rearm_calls, registered_at=time.monotonic() - 10)

        manager._check_pipe_liveness("term")

        assert rearm_calls == [
            True
        ], "a born-stale pipe must be re-armed without waiting for divergence"

    def test_cold_start_not_triggered_before_grace_period_elapses(self, tmp_path, monkeypatch):
        """A terminal registered moments ago must get its grace period, not
        an instant re-arm — the pipe may simply not have had a chance to
        deliver its first byte yet."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_COLD_START_GRACE_S", 3.0)
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "user@host:~$ "}
        rearm_calls: list = []
        self._enroll_cold(manager, "term", pane, rearm_calls, registered_at=time.monotonic())

        manager._check_pipe_liveness("term")

        assert rearm_calls == [], "must not re-arm before the cold-start grace period elapses"

    def test_cold_start_not_triggered_while_pane_still_empty(self, tmp_path, monkeypatch):
        """A pane with no content yet (genuinely still booting — nothing has
        rendered at all) must not be re-armed just because time passed: there
        is nothing for a re-arm+replay to recover, and re-arming an already-
        correct "still starting" pipe is pure churn."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_COLD_START_GRACE_S", 0.0)
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "   \n  "}  # whitespace only
        rearm_calls: list = []
        self._enroll_cold(manager, "term", pane, rearm_calls, registered_at=time.monotonic() - 10)

        manager._check_pipe_liveness("term")

        assert rearm_calls == [], "an empty pane must not be treated as a cold-start stall"

    def test_cold_start_never_fires_once_fifo_has_delivered(self, tmp_path, monkeypatch):
        """Backward compat with the steady-state path: once the FIFO has ever
        delivered a byte, `_ever_delivered` is True and every future check —
        including one where the pane happens to be unchanged — must go
        through the ordinary divergence logic (idle, no re-arm), never the
        cold-start branch, even though content never diverges here either."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_COLD_START_GRACE_S", 0.0)
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "user@host:~$ "}
        rearm_calls: list = []
        self._enroll_cold(manager, "term", pane, rearm_calls, registered_at=time.monotonic() - 10)
        manager._ever_delivered["term"] = True  # the FIFO has delivered at least once

        for _ in range(4):
            manager._check_pipe_liveness("term")  # pane never changes, FIFO silent since

        assert (
            rearm_calls == []
        ), "a pipe that has ever delivered must use the idle/divergence path, not cold-start"

    def test_cold_start_state_absent_falls_back_to_existing_behavior(self, tmp_path, monkeypatch):
        """Terminals enrolled directly (bypassing create_reader — exactly how
        TestPipeLivenessWatchdog's own `_enroll` helper works) never populate
        `_ever_delivered`/`_registered_at`. The cold-start check must default
        to "not a cold start" rather than crashing or misfiring, so every
        pre-existing test in TestPipeLivenessWatchdog keeps exercising only
        the divergence path unchanged."""
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "line0"}
        rearm_calls: list = []
        manager._pane_probe["term"] = lambda: pane["content"]
        manager._rearm["term"] = lambda: rearm_calls.append(True)
        manager._last_data_at["term"] = time.monotonic()
        # Deliberately NOT setting _ever_delivered / _registered_at.

        manager._check_pipe_liveness("term")  # must not raise, must not rearm (baseline only)

        assert rearm_calls == []
        assert "term" in manager._liveness

    def test_cold_start_rearm_replays_live_pane_into_pipeline(self, tmp_path, monkeypatch):
        """Same recovery mechanism as the steady-state stall: the pane's
        current snapshot is republished directly to the event bus, bypassing
        the FIFO/reader thread entirely — this is what actually unblocks
        wait_for_shell() even though the FIFO itself never delivered a byte."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_COLD_START_GRACE_S", 0.0)
        published: list = []
        monkeypatch.setattr(fr.bus, "publish", lambda topic, data: published.append((topic, data)))

        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "user@host:~$ "}
        rearm_calls: list = []
        self._enroll_cold(manager, "term", pane, rearm_calls, registered_at=time.monotonic() - 10)

        manager._check_pipe_liveness("term")

        assert rearm_calls == [True]
        assert ("terminal.term.output", {"data": "\r\n" + pane["content"]}) in published

    def test_cold_start_end_to_end_via_real_reader_thread(self, tmp_path, monkeypatch):
        """Integration-level: create_reader() seeds the cold-start state
        correctly, and a real reader thread flips `_ever_delivered` to True
        the moment actual bytes flow through the FIFO — after which the
        cold-start check must never fire again for this terminal, even if
        content stops changing."""
        manager = self._manager(tmp_path, monkeypatch)
        try:
            manager.create_reader("term-e2e", pane_probe=lambda: "content", rearm=lambda: None)
            with manager._lock:
                assert manager._registered_at.get("term-e2e") is not None
                assert manager._ever_delivered.get("term-e2e") is False

            fifo_path = tmp_path / "term-e2e.fifo"
            wfd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            os.write(wfd, b"real bytes")
            os.close(wfd)

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                with manager._lock:
                    if manager._ever_delivered.get("term-e2e") is True:
                        break
                time.sleep(0.02)

            with manager._lock:
                assert (
                    manager._ever_delivered.get("term-e2e") is True
                ), "the reader thread must flip _ever_delivered once real bytes are read"
        finally:
            manager.stop_reader("term-e2e")
            manager.stop_watchdog()

    def test_cold_start_gives_up_after_max_attempts_instead_of_retrying_forever(
        self, tmp_path, monkeypatch
    ):
        """Self-ROAST regression: a rearm() call SUCCEEDING does not mean the
        pipe actually started delivering — only the reader thread pulling a
        real byte off it flips `_ever_delivered` (the cold-start replay
        publishes straight to the event bus, bypassing the FIFO entirely, so
        it never touches that flag). Before this test existed, a genuinely,
        permanently dead pipe (rearm() never raises, but nothing ever
        actually flows) re-triggered the cold-start branch on literally every
        watchdog check with no bound at all — live-reproduced directly: 5
        consecutive checks against a fake FIFO that never delivers produced 5
        re-arms, `_ever_delivered` still False, and `_rearm_failures` never
        touched (the exception-based give-up mechanism has no visibility into
        this failure class). This must now bound to
        PIPE_LIVENESS_MAX_COLD_START_ATTEMPTS and then stop — dropping the
        terminal from the watchdog entirely, exactly like the rearm()-
        exception give-up path already does."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_COLD_START_GRACE_S", 0.0)
        monkeypatch.setattr(fr, "PIPE_LIVENESS_MAX_COLD_START_ATTEMPTS", 3)
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "user@host:~$ "}  # never changes, FIFO never delivers
        rearm_calls: list = []
        self._enroll_cold(manager, "term", pane, rearm_calls, registered_at=time.monotonic() - 10)

        # Attempts 1-3 (<= max) must still re-arm.
        for _ in range(3):
            manager._check_pipe_liveness("term")
        assert rearm_calls == [True, True, True]
        assert "term" in manager._pane_probe, "must still be enrolled within the attempt budget"

        # The 4th check crosses the budget: give up, do NOT re-arm again, and
        # fully unenroll the terminal (matching the exception-path give-up).
        manager._check_pipe_liveness("term")
        assert rearm_calls == [True, True, True], "must not re-arm past the attempt cap"
        assert "term" not in manager._pane_probe
        assert "term" not in manager._rearm
        assert "term" not in manager._liveness

        # Further checks on an unenrolled terminal must be silent no-ops, not
        # errors and not further re-arms (probe/rearm are gone).
        manager._check_pipe_liveness("term")
        assert rearm_calls == [True, True, True]

    def test_cold_start_attempt_counter_resets_the_grace_clock_between_tries(
        self, tmp_path, monkeypatch
    ):
        """Each cold-start attempt gets its own fresh grace period rather than
        re-firing on literally the next watchdog tick — `_registered_at` is
        bumped forward on every attempt, so a second check immediately after
        the first (before a new grace period has elapsed) must not re-arm
        again yet."""
        monkeypatch.setattr(fr, "PIPE_LIVENESS_COLD_START_GRACE_S", 100.0)
        manager = self._manager(tmp_path, monkeypatch)
        pane = {"content": "user@host:~$ "}
        rearm_calls: list = []
        # Grace period already elapsed for the FIRST attempt only.
        self._enroll_cold(manager, "term", pane, rearm_calls, registered_at=time.monotonic() - 200)

        manager._check_pipe_liveness("term")
        assert rearm_calls == [True]

        # Immediately after: _registered_at was just reset to "now", so the
        # 100s grace period has NOT elapsed again yet.
        manager._check_pipe_liveness("term")
        assert rearm_calls == [True], "must wait out a fresh grace period before re-arming again"


class TestConcurrencyRaces:
    """Round-3 Copilot review on #397: two lock-scope gaps in code the round-2
    review had already touched.

    True interpreter-level race conditions (a context switch landing in the
    exact unlucky spot) can't be forced deterministically in a unit test the
    way the round-2 ``test_stop_during_probe_does_not_resurrect_state`` could
    (that one worked because the injected ``probe()`` callback gave us a hook
    to run the "concurrent" mutation synchronously, in-line, at the precise
    moment). Neither gap here has an equivalent injection point, so these
    tests use real threads and a synchronization barrier (for the first) and
    a real-thread churn stress test (for the second) to raise confidence
    rather than guarantee a repro on every run.
    """

    def test_reader_loop_last_data_at_write_is_atomic_with_stop(self, tmp_path, monkeypatch):
        """The reader loop's ``if terminal_id in self._readers:
        self._last_data_at[terminal_id] = time.monotonic()`` must check and
        write under the same ``_lock`` acquisition. If the check and the write
        are not atomic, a ``stop_reader()`` that pops both dicts between them
        resurrects ``_last_data_at[terminal_id]`` after teardown — a slow leak
        across create/stop churn that ``_watchdog_loop`` (which only iterates
        ``_pane_probe``) will never revisit or clean up.

        This forces the exact interleaving via a synchronization barrier
        (blocking ``time.monotonic()`` while the reader thread holds the lock
        for the write) instead of relying on timing luck, and additionally
        asserts that ``stop_reader()`` is provably blocked on the held lock
        during that window — a mechanical proof the two sections share a
        critical section, not just a probabilistic absence of the bug.
        """
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        fifo_path = tmp_path / "term-race.fifo"
        os.mkfifo(fifo_path)

        manager = FifoManager()
        terminal_id = "term-race"
        # Enroll by hand (no create_reader/watchdog needed for this race).
        stop_flag = threading.Event()
        manager._readers[terminal_id] = stop_flag
        manager._last_data_at[terminal_id] = 0.0

        entered_write_section = threading.Event()
        release_write_section = threading.Event()
        real_monotonic = time.monotonic

        def blocking_monotonic():
            entered_write_section.set()
            release_write_section.wait(timeout=2.0)
            return real_monotonic()

        with (
            patch("cli_agent_orchestrator.services.fifo_reader.bus.publish"),
            patch(
                "cli_agent_orchestrator.services.fifo_reader.time.monotonic",
                side_effect=blocking_monotonic,
            ),
        ):
            reader = threading.Thread(
                target=manager._reader_loop,
                args=(terminal_id, fifo_path, stop_flag),
                daemon=True,
            )
            reader.start()
            time.sleep(0.1)  # let the reader open its fds

            wfd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(wfd, b"x")
                assert entered_write_section.wait(timeout=2.0), (
                    "reader thread never reached the _last_data_at write — "
                    "test setup is broken, not exercising the race"
                )

                stopper = threading.Thread(target=manager.stop_reader, args=(terminal_id,))
                stopper.start()
                # If the check-then-write is atomic under _lock, stop_reader
                # must block trying to acquire it (held by the reader thread,
                # parked in blocking_monotonic) rather than racing ahead.
                time.sleep(0.1)
                assert stopper.is_alive(), (
                    "stop_reader must be blocked on the lock held by the reader "
                    "thread's check-then-write, not proceeding concurrently"
                )

                release_write_section.set()
                stopper.join(timeout=2.0)
            finally:
                os.close(wfd)
                stop_flag.set()
                reader.join(timeout=2.0)

        assert terminal_id not in manager._last_data_at, (
            "stop_reader's pop must win the race — an atomic check-then-write "
            "must not resurrect the entry after teardown"
        )
        assert terminal_id not in manager._readers

    def test_watchdog_loop_survives_concurrent_enroll_unenroll_churn(self, tmp_path, monkeypatch):
        """``_watchdog_loop`` must snapshot ``_pane_probe.keys()`` under
        ``_lock``. Taken unlocked, concurrent create_reader()/stop_reader()
        calls resizing the dict mid-snapshot can raise ``RuntimeError:
        dictionary changed size during iteration`` inside the watchdog
        thread's target — an unhandled exception there kills the thread
        outright (it's not inside the loop's own try/except, which only
        wraps ``_check_pipe_liveness``), permanently disabling self-healing
        for the rest of the process's life.

        Stress test: hammer real enroll/unenroll churn from several threads
        while the real ``_watchdog_loop`` runs with a tiny check interval,
        with the OS thread-switch interval lowered to maximize the chance of
        an unlucky interleaving. Confirms the watchdog thread is still alive
        immediately before being told to stop — if it had already died from
        an unhandled exception, it would be observably not-alive at that
        point instead of only after ``stop_watchdog()``.
        """
        import sys

        monkeypatch.setattr(fr, "PIPE_LIVENESS_CHECK_INTERVAL_S", 0.005)
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()
        # Keep _check_pipe_liveness cheap and side-effect-free: this test is
        # about the snapshot line racing churn, not probe/rearm behavior
        # (covered by TestPipeLivenessWatchdog).
        monkeypatch.setattr(manager, "_check_pipe_liveness", lambda terminal_id: None)

        manager._watchdog_stop.clear()
        watchdog = threading.Thread(target=manager._watchdog_loop, daemon=True)

        stop_churn = threading.Event()

        def churn():
            i = 0
            while not stop_churn.is_set():
                tid = f"term-{i % 8}"
                manager._pane_probe[tid] = lambda: "content"
                manager._rearm[tid] = lambda: None
                manager._pane_probe.pop(tid, None)
                manager._rearm.pop(tid, None)
                i += 1

        churners = [threading.Thread(target=churn) for _ in range(6)]

        original_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-5)
        try:
            watchdog.start()
            for t in churners:
                t.start()

            time.sleep(0.5)

            still_alive_before_stop = watchdog.is_alive()

            stop_churn.set()
            for t in churners:
                t.join(timeout=2.0)
        finally:
            sys.setswitchinterval(original_interval)
            manager._watchdog_stop.set()
            watchdog.join(timeout=2.0)

        assert still_alive_before_stop, (
            "watchdog thread died before being told to stop — an unlocked "
            "dict-keys snapshot racing concurrent enroll/unenroll churn is "
            "exactly the kind of unhandled RuntimeError that would kill it"
        )
        assert not watchdog.is_alive(), "watchdog thread must exit cleanly once stopped"


class TestFifoPathHardening:
    """The per-terminal FIFO path is predictable; only a real FIFO may live there."""

    def _manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        return FifoManager()

    def test_symlink_at_fifo_path_is_refused_and_target_untouched(self, tmp_path, monkeypatch):
        target = tmp_path / "victim.txt"
        target.write_text("precious")
        (tmp_path / "term-link.fifo").symlink_to(target)
        manager = self._manager(tmp_path, monkeypatch)

        with pytest.raises(OSError, match="not a FIFO"):
            manager.create_reader("term-link")

        with manager._lock:
            assert "term-link" not in manager._threads
        assert target.read_text() == "precious"
        assert (tmp_path / "term-link.fifo").is_symlink()  # refused, not replaced

    def test_regular_file_at_fifo_path_is_refused(self, tmp_path, monkeypatch):
        (tmp_path / "term-file.fifo").write_text("not a pipe")
        manager = self._manager(tmp_path, monkeypatch)
        with pytest.raises(OSError, match="not a FIFO"):
            manager.create_reader("term-file")
        with manager._lock:
            assert "term-file" not in manager._threads

    def test_existing_fifo_is_reused(self, tmp_path, monkeypatch):
        fifo_path = tmp_path / "term-keep.fifo"
        os.mkfifo(fifo_path)
        before = os.lstat(fifo_path).st_ino
        manager = self._manager(tmp_path, monkeypatch)
        manager.create_reader("term-keep")
        try:
            assert os.lstat(fifo_path).st_ino == before
        finally:
            manager.stop_reader("term-keep")

    def test_created_fifo_is_owner_only(self, tmp_path, monkeypatch):
        manager = self._manager(tmp_path, monkeypatch)
        manager.create_reader("term-mode")
        try:
            mode = os.lstat(tmp_path / "term-mode.fifo").st_mode
            assert stat.S_ISFIFO(mode)
            assert stat.S_IMODE(mode) & 0o077 == 0
        finally:
            manager.stop_reader("term-mode")

    def test_open_refuses_symlink_even_to_a_real_fifo(self, tmp_path):
        real = tmp_path / "real.fifo"
        os.mkfifo(real)
        link = tmp_path / "link.fifo"
        link.symlink_to(real)
        with pytest.raises(OSError):
            _open_fifo(link, os.O_RDONLY | os.O_NONBLOCK)

    def test_open_refuses_regular_file(self, tmp_path):
        plain = tmp_path / "plain.fifo"
        plain.write_text("x")
        with pytest.raises(OSError, match="not a FIFO"):
            _open_fifo(plain, os.O_RDONLY | os.O_NONBLOCK)

    def test_open_accepts_a_fifo(self, tmp_path):
        real = tmp_path / "ok.fifo"
        os.mkfifo(real)
        fd = _open_fifo(real, os.O_RDONLY | os.O_NONBLOCK)
        try:
            assert stat.S_ISFIFO(os.fstat(fd).st_mode)
        finally:
            os.close(fd)
