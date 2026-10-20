"""Tests for LogWriter batching behavior."""

import asyncio
from unittest.mock import patch

import pytest


@pytest.fixture
def isolated_log_dir(monkeypatch, tmp_path):
    """Point TERMINAL_LOG_DIR at a temp dir so tests don't touch real logs."""
    log_dir = tmp_path / "terminal"
    log_dir.mkdir()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.log_writer.TERMINAL_LOG_DIR",
        log_dir,
    )
    return log_dir


@pytest.fixture
def mock_settings():
    with patch("cli_agent_orchestrator.services.event_bus.get_server_settings") as m:
        m.return_value = {
            "mcp_request_timeout": 30,
            "event_bus_max_queue_size": 4096,
            "provider_init_timeout": 60,
            "startup_prompt_handler_timeout": 20,
        }
        yield m


class TestLogWriterBatching:
    @pytest.mark.asyncio
    async def test_multiple_events_same_terminal_open_file_once(
        self, isolated_log_dir, mock_settings
    ):
        """Regression: single-write-per-event pattern couldn't keep up with two
        concurrent workers streaming output. Batching drains up to _MAX_BATCH
        events before flushing and groups by file so each unique log is
        opened once per batch, not once per event.
        """
        from cli_agent_orchestrator.services.event_bus import EventBus
        from cli_agent_orchestrator.services.log_writer import LogWriter

        # Fresh bus so consumers only see events published in this test
        bus = EventBus()
        bus.set_loop(asyncio.get_running_loop())

        # Point log_writer at our fresh bus
        with patch("cli_agent_orchestrator.services.log_writer.bus", bus):
            writer = LogWriter()
            open_calls = {"n": 0}

            real_open = open

            def counting_open(path, *args, **kwargs):
                if str(path).endswith(".log"):
                    open_calls["n"] += 1
                return real_open(path, *args, **kwargs)

            with patch("builtins.open", side_effect=counting_open):
                task = asyncio.create_task(writer.run())
                # Publish 100 chunks for terminal aaa
                for i in range(100):
                    bus.publish("terminal.aaaaaaaa.output", {"data": f"line-{i}\n"})

                # Give the writer time to drain
                await asyncio.sleep(0.5)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # All 100 chunks are in the file
        log_path = isolated_log_dir / "aaaaaaaa.log"
        content = log_path.read_text()
        for i in range(100):
            assert f"line-{i}\n" in content

        # But we opened the file WAY less than 100 times.
        # In pure single-write mode this was 100 opens. Batching should
        # collapse it to something like 1-2.
        assert (
            open_calls["n"] < 20
        ), f"expected <20 file opens for 100 batched events, got {open_calls['n']}"

    @pytest.mark.asyncio
    async def test_ordering_preserved_within_terminal(self, isolated_log_dir, mock_settings):
        """Batched writes must preserve chunk order or interleaved output would
        become unreadable."""
        from cli_agent_orchestrator.services.event_bus import EventBus
        from cli_agent_orchestrator.services.log_writer import LogWriter

        bus = EventBus()
        bus.set_loop(asyncio.get_running_loop())

        with patch("cli_agent_orchestrator.services.log_writer.bus", bus):
            writer = LogWriter()
            task = asyncio.create_task(writer.run())

            for i in range(500):
                bus.publish("terminal.bbbbbbbb.output", {"data": f"{i}\n"})

            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        content = (isolated_log_dir / "bbbbbbbb.log").read_text()
        lines = content.strip().splitlines()
        assert lines == [
            str(i) for i in range(500)
        ], f"ordering broken: first 5 = {lines[:5]}, last 5 = {lines[-5:]}"

    @pytest.mark.asyncio
    async def test_multiple_terminals_are_written_to_separate_files(
        self, isolated_log_dir, mock_settings
    ):
        """A batch may contain events for multiple terminals; each must land
        in its own log file."""
        from cli_agent_orchestrator.services.event_bus import EventBus
        from cli_agent_orchestrator.services.log_writer import LogWriter

        bus = EventBus()
        bus.set_loop(asyncio.get_running_loop())

        with patch("cli_agent_orchestrator.services.log_writer.bus", bus):
            writer = LogWriter()
            task = asyncio.create_task(writer.run())

            for i in range(50):
                bus.publish("terminal.cccccccc.output", {"data": f"C{i}\n"})
                bus.publish("terminal.dddddddd.output", {"data": f"D{i}\n"})

            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        c_content = (isolated_log_dir / "cccccccc.log").read_text()
        d_content = (isolated_log_dir / "dddddddd.log").read_text()
        assert c_content.count("C") == 50
        assert d_content.count("D") == 50
        # No cross-contamination
        assert "D" not in c_content
        assert "C" not in d_content


class TestLogWriterPrivateFiles:
    """Terminal logs hold raw PTY bytes: created 0600, never appended through a symlink."""

    def test_write_creates_log_private(self, tmp_path):
        from cli_agent_orchestrator.services.log_writer import LogWriter

        path = tmp_path / "abcd1234.log"
        LogWriter._write(path, "hello\n")
        LogWriter._write(path, "again\n")
        assert path.read_text() == "hello\nagain\n"
        assert oct(path.stat().st_mode & 0o777) == oct(0o600)

    def test_write_tightens_an_existing_world_readable_log(self, tmp_path):
        """O_CREAT's mode only applies on create: a 0644 log left by a terminal
        that was running across the upgrade must be tightened by the next write,
        and the write must still append rather than truncate."""
        from cli_agent_orchestrator.services.log_writer import LogWriter

        path = tmp_path / "abcd1234.log"
        path.write_text("old\n")
        path.chmod(0o644)
        assert oct(path.stat().st_mode & 0o777) == oct(0o644)
        LogWriter._write(path, "new\n")
        assert oct(path.stat().st_mode & 0o777) == oct(0o600)
        assert path.read_text() == "old\nnew\n"

    def test_write_refuses_a_symlinked_log_path(self, tmp_path):
        import pytest

        from cli_agent_orchestrator.services.log_writer import LogWriter

        victim = tmp_path / "victim.txt"
        victim.write_text("do not touch")
        link = tmp_path / "abcd1234.log"
        link.symlink_to(victim)
        with pytest.raises(OSError):
            LogWriter._write(link, "leak\n")
        assert victim.read_text() == "do not touch"
