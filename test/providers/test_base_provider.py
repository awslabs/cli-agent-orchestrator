"""Tests for base provider."""

import time
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.agent_profile import (
    AgentProfile,
    ContainerConfig,
    ContainerPathMap,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider


class ConcreteProvider(BaseProvider):
    """Concrete implementation of BaseProvider for testing."""

    async def initialize(self) -> bool:
        return True

    def get_status(self, buffer: str) -> TerminalStatus:
        if not buffer:
            return TerminalStatus.UNKNOWN
        return TerminalStatus.IDLE

    def extract_last_message_from_script(self, script_output: str) -> str:
        return "extracted message"

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        pass


class TestBaseProvider:
    """Tests for BaseProvider abstract class."""

    def test_init(self):
        """Test provider initialization."""
        provider = ConcreteProvider("term-123", "session-1", "window-0")

        assert provider.terminal_id == "term-123"
        assert provider.session_name == "session-1"
        assert provider.window_name == "window-0"

    def test_apply_skill_prompt_appends(self):
        """Test _apply_skill_prompt appends skill text to base prompt."""
        provider = ConcreteProvider(
            "term-123", "session-1", "window-0", skill_prompt="## Skills\n- skill1"
        )
        result = provider._apply_skill_prompt("Base prompt")
        assert result == "Base prompt\n\n## Skills\n- skill1"

    def test_apply_skill_prompt_no_skill(self):
        """Test _apply_skill_prompt returns original when no skill_prompt."""
        provider = ConcreteProvider("term-123", "session-1", "window-0")
        result = provider._apply_skill_prompt("Base prompt")
        assert result == "Base prompt"

    def test_apply_skill_prompt_empty_base(self):
        """Test _apply_skill_prompt with empty base and skill_prompt present."""
        provider = ConcreteProvider("term-123", "session-1", "window-0", skill_prompt="## Skills")
        result = provider._apply_skill_prompt("")
        assert result == "## Skills"

    def test_abstract_methods_implemented(self):
        """Test that concrete implementation works."""
        provider = ConcreteProvider("term-123", "session-1", "window-0")

        assert provider.get_status("some output") == TerminalStatus.IDLE
        assert provider.extract_last_message_from_script("test") == "extracted message"
        assert provider.exit_cli() == "/exit"
        provider.cleanup()  # Should not raise


def _profile(*pairs: tuple[str, str]) -> AgentProfile:
    """Build a profile whose container declares the given host->guest maps."""
    return AgentProfile(
        name="a",
        description="d",
        container=ContainerConfig(path_maps=[ContainerPathMap(host=h, guest=g) for h, g in pairs]),
    )


class TestTranslatePath:
    """Tests for BaseProvider._translate_path."""

    def setup_method(self):
        self.provider = ConcreteProvider("term-123", "session-1", "window-0")

    def test_no_profile_returns_unchanged(self):
        assert self.provider._translate_path("/host/x.txt") == "/host/x.txt"

    def test_no_container_returns_unchanged(self):
        profile = AgentProfile(name="a", description="d")
        assert self.provider._translate_path("/host/x.txt", profile) == "/host/x.txt"

    def test_empty_path_maps_returns_unchanged(self):
        profile = AgentProfile(name="a", description="d", container=ContainerConfig())
        assert self.provider._translate_path("/host/x.txt", profile) == "/host/x.txt"

    def test_no_matching_prefix_returns_unchanged(self):
        profile = _profile(("/host", "/guest"))
        assert self.provider._translate_path("/other/x.txt", profile) == "/other/x.txt"

    def test_simple_prefix_substitution(self):
        profile = _profile(("/host", "/guest"))
        assert self.provider._translate_path("/host/x.txt", profile) == "/guest/x.txt"

    def test_longest_prefix_wins(self):
        profile = _profile(("/host", "/guest"), ("/host/sub", "/deep"))
        assert self.provider._translate_path("/host/sub/x.txt", profile) == "/deep/x.txt"

    def test_exact_host_match(self):
        profile = _profile(("/host", "/guest"))
        assert self.provider._translate_path("/host", profile) == "/guest"

    def test_trailing_slashes_normalized(self):
        profile = _profile(("/host/", "/guest/"))
        assert self.provider._translate_path("/host/x.txt", profile) == "/guest/x.txt"

    def test_prefix_boundary_not_substring(self):
        """A host prefix must match a path segment, not a bare substring."""
        profile = _profile(("/host", "/guest"))
        assert self.provider._translate_path("/hostile/x.txt", profile) == "/hostile/x.txt"


# Absolute path to base.time so staleness tests can pin time.time() deterministically.
_BASE_TIME = "cli_agent_orchestrator.providers.base.time.time"


class TestResolveNativeStatus:
    """Tests for BaseProvider._resolve_native_status when the backend reports None.

    Covers the native-None resolution matrix (see the method docstring). The
    backend's get_native_status() returns None both on tmux (buffer populated)
    and on the herdr 'unknown' agent_status (empty buffer); this method
    disambiguates via ``buffer`` and dispatch state:

    | buffer      | _task_dispatched     | Result                  |
    |-------------|----------------------|-------------------------|
    | non-empty   | *                    | None (fall to buffer)   |
    | empty       | False                | IDLE                    |
    | empty       | True, fresh (<120s)  | PROCESSING (optimistic) |
    | empty       | True, stale (>=120s) | ERROR (+ warning log)   |

    Called directly (not via get_status) because row 1 — non-empty buffer
    returning None — is only observable at this method's boundary; through
    get_status() it continues into provider buffer parsing. No provider
    overrides _resolve_native_status, so ConcreteProvider exercises the real
    shared implementation.
    """

    def _provider(self):
        return ConcreteProvider("term-123", "session-1", "window-0")

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_none_empty_buffer_not_dispatched_returns_idle(self, mock_backend):
        """Row: empty buffer, no task dispatched -> IDLE (unblocks init)."""
        mock_backend.get_native_status.return_value = None
        provider = self._provider()
        assert provider._task_dispatched is False  # precondition
        assert provider._resolve_native_status("") == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_none_empty_buffer_none_arg_not_dispatched_returns_idle(self, mock_backend):
        """A None buffer is treated like an empty buffer (herdr passes no buffer)."""
        mock_backend.get_native_status.return_value = None
        provider = self._provider()
        assert provider._resolve_native_status(None) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_none_empty_buffer_dispatched_fresh_returns_processing(self, mock_backend):
        """Row: empty buffer, dispatched, 119s elapsed (<120s) -> PROCESSING.

        Boundary value: just inside the freshness window.
        """
        mock_backend.get_native_status.return_value = None
        provider = self._provider()
        provider._task_dispatched = True
        provider._last_dispatch_time = 1000.0
        with patch(_BASE_TIME, return_value=1000.0 + 119.0):
            assert provider._resolve_native_status("") == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_none_dispatched_at_staleness_boundary_returns_error(self, mock_backend):
        """Boundary value: elapsed == 120s is NOT < timeout, so -> ERROR (not PROCESSING)."""
        mock_backend.get_native_status.return_value = None
        provider = self._provider()
        provider._task_dispatched = True
        provider._last_dispatch_time = 1000.0
        with patch(_BASE_TIME, return_value=1000.0 + 120.0):
            assert provider._resolve_native_status("") == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_none_empty_buffer_dispatched_stale_returns_error(self, mock_backend, caplog):
        """Row: empty buffer, dispatched, 121s elapsed (>120s) -> ERROR + warning log."""
        import logging

        mock_backend.get_native_status.return_value = None
        provider = self._provider()
        provider._task_dispatched = True
        provider._last_dispatch_time = 1000.0
        with patch(_BASE_TIME, return_value=1000.0 + 121.0):
            with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.providers.base"):
                result = provider._resolve_native_status("")
        assert result == TerminalStatus.ERROR
        assert any(
            "staleness timeout" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        ), "stale native-None resolution must log a WARNING"

    @pytest.mark.parametrize("dispatched", [False, True], ids=["not_dispatched", "dispatched"])
    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_none_nonempty_buffer_falls_through(self, mock_backend, dispatched):
        """Row: non-empty buffer -> None regardless of dispatch state (defer to buffer path)."""
        mock_backend.get_native_status.return_value = None
        provider = self._provider()
        provider._task_dispatched = dispatched
        provider._last_dispatch_time = time.time()
        assert provider._resolve_native_status("some content") is None


# Where get_init_timeout reads the server default from.
_SETTINGS_FN = "cli_agent_orchestrator.services.settings_service.get_server_settings"


class TestGetInitTimeout:
    """Tests for BaseProvider.get_init_timeout (per-profile init-timeout override)."""

    def _provider(self):
        return ConcreteProvider("term-123", "session-1", "window-0")

    @patch(_SETTINGS_FN, return_value={"provider_init_timeout": 60})
    def test_no_profile_uses_server_default(self, _):
        assert self._provider().get_init_timeout() == 60

    @patch(_SETTINGS_FN, return_value={"provider_init_timeout": 60})
    def test_profile_without_override_uses_server_default(self, _):
        profile = AgentProfile(name="a", description="d")
        assert self._provider().get_init_timeout(profile) == 60

    @patch(_SETTINGS_FN, return_value={"provider_init_timeout": 60})
    def test_profile_override_wins(self, _):
        profile = AgentProfile(name="a", description="d", provider_init_timeout=180)
        assert self._provider().get_init_timeout(profile) == 180
