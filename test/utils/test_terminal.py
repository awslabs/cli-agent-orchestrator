"""Tests for terminal utilities."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.utils.terminal import (
    generate_session_name,
    generate_terminal_id,
    generate_window_name,
    sync_backend_from_server,
    validate_tmux_name,
    wait_for_shell,
    wait_until_status,
    wait_until_terminal_status,
)


class TestGenerateFunctions:
    """Tests for ID generation functions."""

    def test_generate_session_name(self):
        """Test session name generation."""
        name = generate_session_name()

        assert name.startswith("cao-")
        assert len(name) == 12  # cao- (4) + uuid (8)

    def test_generate_session_name_unique(self):
        """Test session names are unique."""
        names = [generate_session_name() for _ in range(100)]

        assert len(set(names)) == 100

    def test_generate_terminal_id(self):
        """Test terminal ID generation."""
        terminal_id = generate_terminal_id()

        assert len(terminal_id) == 8

    def test_generate_terminal_id_unique(self):
        """Test terminal IDs are unique."""
        ids = [generate_terminal_id() for _ in range(100)]

        assert len(set(ids)) == 100

    def test_generate_window_name(self):
        """Test window name generation."""
        name = generate_window_name("developer")

        assert name.startswith("developer-")
        assert len(name) == 14  # developer- (10) + uuid (4)

    def test_generate_window_name_unique(self):
        """Distinct uuid suffixes yield distinct window names.

        The real suffix is only 4 hex chars (65536 values), so asserting that N
        live random draws never collide is a birthday-paradox flake. Pin the
        randomness instead: distinct uuids must map to distinct names, which is
        what the suffix is actually there to guarantee.
        """
        # generate_window_name slices .hex[:4], so vary the FIRST 4 hex chars.
        suffixes = [f"{i:04x}cafe" for i in range(10)]
        with patch("cli_agent_orchestrator.utils.terminal.uuid.uuid4") as mock_uuid4:
            mock_uuid4.side_effect = [MagicMock(hex=s) for s in suffixes]
            names = [generate_window_name("test") for _ in range(10)]

        assert len(set(names)) == 10

    def test_generate_window_name_rejects_unsafe_profile(self):
        """A profile name with tmux delimiters must not produce a window name."""
        with pytest.raises(ValueError):
            generate_window_name("evil:profile")
        with pytest.raises(ValueError):
            generate_window_name("evil profile")
        with pytest.raises(ValueError):
            generate_window_name("../escape")


class TestValidateTmuxName:
    """Tests for the tmux name allowlist validator."""

    @pytest.mark.parametrize(
        "name",
        [
            "cao-abcd1234",
            "developer-1a2b",
            "session_1",
            "A",
            "abc123",
            "_underscore_start",
            "a" * 64,
        ],
    )
    def test_accepts_safe_names(self, name):
        assert validate_tmux_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "-leading-dash",
            "with:colon",
            "with.dot",
            "with space",
            "with/slash",
            "with;semi",
            "with$dollar",
            "with`backtick",
            "with$(cmd)",
            "with\nnewline",
            "trailing\n",
            "trailing\r",
            "..",
            "../escape",
            "a" * 65,
        ],
    )
    def test_rejects_unsafe_names(self, name):
        with pytest.raises(ValueError):
            validate_tmux_name(name)

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            validate_tmux_name(None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            validate_tmux_name(123)  # type: ignore[arg-type]

    def test_error_message_includes_kind(self):
        try:
            validate_tmux_name("bad:name", kind="session_name")
        except ValueError as e:
            assert "session_name" in str(e)
        else:
            pytest.fail("expected ValueError")


class TestWaitForShell:
    """Tests for wait_for_shell function."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_for_shell_success(self, mock_monitor):
        """Test successful shell wait - buffer is non-empty and stable."""
        mock_monitor.get_buffer.return_value = "prompt $"

        result = await wait_for_shell(
            "test-terminal", timeout=2.0, stable_duration=0.3, polling_interval=0.1
        )

        assert result is True

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_for_shell_timeout(self, mock_monitor):
        """Test shell wait timeout - buffer keeps changing."""
        call_count = [0]

        def get_buffer_side_effect(terminal_id):
            call_count[0] += 1
            return f"output {call_count[0]}"

        mock_monitor.get_buffer.side_effect = get_buffer_side_effect

        result = await wait_for_shell(
            "test-terminal", timeout=0.5, stable_duration=0.3, polling_interval=0.1
        )

        assert result is False

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_for_shell_empty_output(self, mock_monitor):
        """Test shell wait with empty output."""
        mock_monitor.get_buffer.return_value = ""

        result = await wait_for_shell(
            "test-terminal", timeout=0.5, stable_duration=0.3, polling_interval=0.1
        )

        assert result is False


class TestWaitForShellEventInbox:
    """wait_for_shell on event-inbox backends (herdr) must read backend history,
    not the (always-empty) StatusMonitor buffer."""

    def _backend(self, *, history, event_inbox=True):
        backend = MagicMock()
        backend.supports_event_inbox.return_value = event_inbox
        backend.get_history.return_value = history
        return backend

    def _provider(self):
        provider = MagicMock()
        provider.session_name = "sess"
        provider.window_name = "win"
        return provider

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.manager.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_reads_backend_history_when_event_inbox(
        self, mock_monitor, mock_get_backend, mock_pm
    ):
        # StatusMonitor buffer is empty (herdr never feeds it); readiness must
        # still be detected from the backend's pane history.
        mock_monitor.get_buffer.return_value = ""
        backend = self._backend(history="user@host:~$ ")
        mock_get_backend.return_value = backend
        mock_pm.get_provider.return_value = self._provider()

        result = await wait_for_shell("t1", timeout=2.0, stable_duration=0.3, polling_interval=0.1)

        assert result is True
        backend.get_history.assert_called_with("sess", "win", strip_escapes=True)
        mock_monitor.get_buffer.assert_not_called()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.manager.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_times_out_when_backend_history_empty(
        self, mock_monitor, mock_get_backend, mock_pm
    ):
        mock_get_backend.return_value = self._backend(history="")
        mock_pm.get_provider.return_value = self._provider()

        result = await wait_for_shell("t1", timeout=0.4, stable_duration=0.2, polling_interval=0.1)

        assert result is False

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.manager.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_tmux_backend_still_uses_status_monitor(
        self, mock_monitor, mock_get_backend, mock_pm
    ):
        # Pipe-pane backend: behavior unchanged — read the StatusMonitor buffer,
        # never touch backend.get_history.
        mock_monitor.get_buffer.return_value = "prompt $"
        backend = self._backend(history="ignored", event_inbox=False)
        mock_get_backend.return_value = backend

        result = await wait_for_shell("t1", timeout=2.0, stable_duration=0.3, polling_interval=0.1)

        assert result is True
        backend.get_history.assert_not_called()
        mock_pm.get_provider.assert_not_called()


class TestWaitUntilStatus:
    """Tests for wait_until_status function."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_until_status_success(self, mock_monitor):
        """Test successful status wait."""
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        result = await wait_until_status(
            "test-terminal", TerminalStatus.IDLE, timeout=1.0, polling_interval=0.1
        )

        assert result is True

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_until_status_timeout(self, mock_monitor):
        """Test status wait timeout."""
        mock_monitor.get_status.return_value = TerminalStatus.PROCESSING

        result = await wait_until_status(
            "test-terminal", TerminalStatus.IDLE, timeout=0.5, polling_interval=0.1
        )

        assert result is False

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_until_status_with_set(self, mock_monitor):
        """Test status wait accepts a set of target statuses."""
        mock_monitor.get_status.return_value = TerminalStatus.COMPLETED

        result = await wait_until_status(
            "test-terminal",
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=1.0,
            polling_interval=0.1,
        )

        assert result is True

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_until_status_eventually_succeeds(self, mock_monitor):
        """Test status wait that eventually succeeds."""
        mock_monitor.get_status.side_effect = [
            TerminalStatus.PROCESSING,
            TerminalStatus.PROCESSING,
            TerminalStatus.IDLE,
        ]

        result = await wait_until_status(
            "test-terminal", TerminalStatus.IDLE, timeout=2.0, polling_interval=0.1
        )

        assert result is True


class TestWaitUntilTerminalStatus:
    """Tests for wait_until_terminal_status function."""

    @patch("cli_agent_orchestrator.utils.terminal.requests.get")
    def test_wait_until_terminal_status_success(self, mock_get):
        """Test successful terminal status wait."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": TerminalStatus.IDLE.value}
        mock_get.return_value = mock_response

        result = wait_until_terminal_status(
            "test-terminal", TerminalStatus.IDLE, timeout=1.0, polling_interval=0.1
        )

        assert result is True

    @patch("cli_agent_orchestrator.utils.terminal.requests.get")
    def test_wait_until_terminal_status_timeout(self, mock_get):
        """Test terminal status wait timeout."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "PROCESSING"}
        mock_get.return_value = mock_response

        result = wait_until_terminal_status(
            "test-terminal", TerminalStatus.IDLE, timeout=0.5, polling_interval=0.1
        )

        assert result is False

    @patch("cli_agent_orchestrator.utils.terminal.requests.get")
    def test_wait_until_terminal_status_api_error(self, mock_get):
        """Test terminal status wait with API error."""
        mock_get.side_effect = Exception("Connection error")

        result = wait_until_terminal_status(
            "test-terminal", TerminalStatus.IDLE, timeout=0.5, polling_interval=0.1
        )

        assert result is False

    @patch("cli_agent_orchestrator.utils.terminal.requests.get")
    def test_wait_until_terminal_status_non_200(self, mock_get):
        """Test terminal status wait with non-200 response."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = wait_until_terminal_status(
            "test-terminal", TerminalStatus.IDLE, timeout=0.5, polling_interval=0.1
        )

        assert result is False

    @patch("cli_agent_orchestrator.utils.terminal.requests.get")
    def test_wait_until_terminal_status_multi_status_set(self, mock_get):
        """Test waiting for multiple target statuses (set)."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": TerminalStatus.COMPLETED.value}
        mock_get.return_value = mock_response

        result = wait_until_terminal_status(
            "test-terminal",
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=1.0,
            polling_interval=0.1,
        )

        assert result is True

    @patch("cli_agent_orchestrator.utils.terminal.requests.get")
    def test_wait_until_terminal_status_multi_status_no_match(self, mock_get):
        """Test multi-status wait times out when status doesn't match any target."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": TerminalStatus.PROCESSING.value}
        mock_get.return_value = mock_response

        result = wait_until_terminal_status(
            "test-terminal",
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=0.5,
            polling_interval=0.1,
        )

        assert result is False


# ── sync_backend_from_server (issue #308) ────────────────────────────


class TestSyncBackendFromServer:
    """Tests for sync_backend_from_server() helper."""

    def test_syncs_herdr_backend_from_health(self):
        """When /health reports terminal_backend='herdr', set_backend is called with herdr."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"terminal_backend": "herdr"}
        mock_resp.raise_for_status.return_value = None

        with (
            patch("cli_agent_orchestrator.utils.terminal.requests.get", return_value=mock_resp),
            patch("cli_agent_orchestrator.backends.factory.BackendFactory.create") as mock_create,
            patch("cli_agent_orchestrator.backends.registry.set_backend") as mock_set,
        ):
            mock_backend = MagicMock()
            mock_create.return_value = mock_backend

            sync_backend_from_server()

            mock_create.assert_called_once_with(backend_override="herdr")
            mock_set.assert_called_once_with(mock_backend)

    def test_syncs_tmux_backend_from_health(self):
        """When /health reports terminal_backend='tmux', set_backend is called with tmux."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"terminal_backend": "tmux"}
        mock_resp.raise_for_status.return_value = None

        with (
            patch("cli_agent_orchestrator.utils.terminal.requests.get", return_value=mock_resp),
            patch("cli_agent_orchestrator.backends.factory.BackendFactory.create") as mock_create,
            patch("cli_agent_orchestrator.backends.registry.set_backend") as mock_set,
        ):
            mock_backend = MagicMock()
            mock_create.return_value = mock_backend

            sync_backend_from_server()

            mock_create.assert_called_once_with(backend_override="tmux")
            mock_set.assert_called_once_with(mock_backend)

    def test_silently_handles_connection_error(self):
        """When server is unreachable, no exception is raised."""
        import requests as _requests

        with patch(
            "cli_agent_orchestrator.utils.terminal.requests.get",
            side_effect=_requests.exceptions.ConnectionError("refused"),
        ):
            # Must not raise
            sync_backend_from_server()

    def test_silently_handles_missing_field(self):
        """When /health response lacks terminal_backend, no set_backend call."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.raise_for_status.return_value = None

        with (
            patch("cli_agent_orchestrator.utils.terminal.requests.get", return_value=mock_resp),
            patch("cli_agent_orchestrator.backends.registry.set_backend") as mock_set,
        ):
            sync_backend_from_server()
            mock_set.assert_not_called()


class TestPollUntilDone:
    """poll_until_done: COMPLETED returns immediately; IDLE needs a stable window.

    Regression: kiro-cli 2.11 finishes many turns at IDLE (no Credits marker),
    so requiring COMPLETED caused `cao launch` / `cao session send` to hang and
    time out on a kiro agent that had actually completed. COMPLETED behaviour is
    unchanged (single reading returns); IDLE is accepted only after a short
    stable window since idle is ambiguous mid-turn.
    """

    def _resp(self, status):
        m = MagicMock()
        m.raise_for_status.return_value = None
        m.json.return_value = {"status": status}
        return m

    def test_completed_returns_immediately(self):
        """A single COMPLETED reading returns at once (unchanged behaviour)."""
        from cli_agent_orchestrator.utils.terminal import poll_until_done

        with (
            patch("cli_agent_orchestrator.utils.terminal.requests.get") as g,
            patch("cli_agent_orchestrator.utils.terminal.time.sleep"),
        ):
            g.return_value = self._resp("completed")
            poll_until_done("abcd1234", timeout=60, polling_interval=0)
            assert g.call_count == 1

    def test_returns_on_stable_idle_after_working(self):
        """IDLE completes only after the agent has been observed working, then
        stays idle for idle_stable_polls reads."""
        from cli_agent_orchestrator.utils.terminal import poll_until_done

        seq = [
            self._resp("processing"),  # agent started
            self._resp("idle"),
            self._resp("idle"),
            self._resp("idle"),  # 3rd stable idle -> return
        ]
        with (
            patch("cli_agent_orchestrator.utils.terminal.requests.get") as g,
            patch("cli_agent_orchestrator.utils.terminal.time.sleep"),
        ):
            g.side_effect = seq
            poll_until_done("abcd1234", timeout=60, polling_interval=0, idle_stable_polls=3)
            assert g.call_count == 4

    def test_idle_before_processing_does_not_return_early(self):
        """Regression (PR #390 review): idle right after a send, before the
        agent has begun processing, must NOT be treated as done — that would
        yield empty/partial output. Only after a working reading does idle
        count. Times out here (never sees working) rather than returning early.
        """
        import click

        from cli_agent_orchestrator.utils.terminal import poll_until_done

        # Always idle, never working; timeout guard uses time.time, so make it
        # advance to force a timeout after a few polls.
        times = iter([0, 0.5, 1.0, 1.5, 2.0, 2.5, 100.0])
        with (
            patch("cli_agent_orchestrator.utils.terminal.requests.get") as g,
            patch("cli_agent_orchestrator.utils.terminal.time.sleep"),
            patch(
                "cli_agent_orchestrator.utils.terminal.time.time", side_effect=lambda: next(times)
            ),
        ):
            g.return_value = self._resp("idle")
            with pytest.raises(click.ClickException, match="Timed out"):
                poll_until_done("abcd1234", timeout=10, polling_interval=0, idle_stable_polls=3)

    def test_transient_idle_does_not_return_early(self):
        """A single idle poll surrounded by processing must NOT satisfy the
        stable-idle gate — otherwise we'd return mid-turn."""
        from cli_agent_orchestrator.utils.terminal import poll_until_done

        seq = [
            self._resp("processing"),
            self._resp("idle"),  # transient
            self._resp("processing"),
            self._resp("idle"),
            self._resp("idle"),
            self._resp("idle"),  # now stable -> return
        ]
        with (
            patch("cli_agent_orchestrator.utils.terminal.requests.get") as g,
            patch("cli_agent_orchestrator.utils.terminal.time.sleep"),
        ):
            g.side_effect = seq
            poll_until_done("abcd1234", timeout=60, polling_interval=0, idle_stable_polls=3)
            # All 6 responses consumed => the transient idle did not trigger return.
            assert g.call_count == 6

    def test_completed_after_idle_returns_without_full_idle_window(self):
        """COMPLETED short-circuits even if fewer than idle_stable_polls idles
        preceded it — the definitive marker wins."""
        from cli_agent_orchestrator.utils.terminal import poll_until_done

        seq = [
            self._resp("idle"),  # 1 idle, not yet stable
            self._resp("completed"),  # definitive -> return now
        ]
        with (
            patch("cli_agent_orchestrator.utils.terminal.requests.get") as g,
            patch("cli_agent_orchestrator.utils.terminal.time.sleep"),
        ):
            g.side_effect = seq
            poll_until_done("abcd1234", timeout=60, polling_interval=0, idle_stable_polls=3)
            assert g.call_count == 2

    def test_unknown_does_not_count_as_working(self):
        """Regression (PR #390 review): UNKNOWN must NOT flip observed_working —
        a terminal can report UNKNOWN before it starts (no output yet / provider
        not registered / deferred init). If UNKNOWN counted as "started", a
        following stable idle would satisfy the gate and return early with empty
        output. Here: unknown then all-idle must time out, not return.
        """
        import click

        from cli_agent_orchestrator.utils.terminal import poll_until_done

        times = iter([0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 100.0])
        with (
            patch("cli_agent_orchestrator.utils.terminal.requests.get") as g,
            patch("cli_agent_orchestrator.utils.terminal.time.sleep"),
            patch(
                "cli_agent_orchestrator.utils.terminal.time.time", side_effect=lambda: next(times)
            ),
        ):
            # unknown first, then idle forever — never a working reading.
            g.side_effect = [self._resp("unknown")] + [self._resp("idle")] * 10
            with pytest.raises(click.ClickException, match="Timed out"):
                poll_until_done("abcd1234", timeout=10, polling_interval=0, idle_stable_polls=3)

    def test_processing_then_idle_still_returns(self):
        """PROCESSING (unlike UNKNOWN) does flip observed_working, so a stable
        idle after it returns normally — guards against over-tightening."""
        from cli_agent_orchestrator.utils.terminal import poll_until_done

        seq = [
            self._resp("unknown"),  # not working
            self._resp("processing"),  # now working
            self._resp("idle"),
            self._resp("idle"),
            self._resp("idle"),  # stable -> return
        ]
        with (
            patch("cli_agent_orchestrator.utils.terminal.requests.get") as g,
            patch("cli_agent_orchestrator.utils.terminal.time.sleep"),
        ):
            g.side_effect = seq
            poll_until_done("abcd1234", timeout=60, polling_interval=0, idle_stable_polls=3)
            assert g.call_count == 5

    def test_error_raises(self):
        import click

        from cli_agent_orchestrator.utils.terminal import poll_until_done

        with (
            patch("cli_agent_orchestrator.utils.terminal.requests.get") as g,
            patch("cli_agent_orchestrator.utils.terminal.time.sleep"),
        ):
            g.return_value = self._resp("error")
            with pytest.raises(click.ClickException):
                poll_until_done("abcd1234", timeout=60, polling_interval=0)
