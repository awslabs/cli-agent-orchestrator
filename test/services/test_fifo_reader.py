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
