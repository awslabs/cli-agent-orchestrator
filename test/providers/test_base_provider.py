"""Tests for base provider."""

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

    def test_root_mapping_alone_is_applied(self):
        """A host="/" mapping must not silently no-op (nit fix, PR #428 review).

        rstrip("/") reduces "/" to "" (length 0); the prior best_len=0 seed
        made `len("") > 0` always false, so a root mapping never won even when
        it was the only entry -- the guest translation was silently dropped.
        """
        profile = _profile(("/", "/guest"))
        assert self.provider._translate_path("/host/x.txt", profile) == "/guest/host/x.txt"

    def test_root_mapping_loses_to_more_specific_prefix(self):
        """A root mapping still only wins when nothing more specific matches."""
        profile = _profile(("/", "/guest"), ("/host", "/deep"))
        assert self.provider._translate_path("/host/x.txt", profile) == "/deep/x.txt"
        assert self.provider._translate_path("/other/x.txt", profile) == "/guest/other/x.txt"


class TestResolveNativeStatus:
    """Tests for BaseProvider._resolve_native_status.

    ``get_native_status()`` returns None whenever the backend cannot resolve a
    real agent state -- always on tmux, and on herdr for the "unknown"
    agent_status (a wrapped exec launch hides the agent CLI). Both cases must
    return None unconditionally: no guessing from dispatch state. A prior
    version of this method inferred IDLE/PROCESSING/ERROR from
    ``_task_dispatched`` timing when native was unresolvable and the buffer was
    empty; that traded fail-fast init detection for optimistic guessing (a dead
    herdr launch reported false-success IDLE, and a genuinely wedged wrapped
    pane could never reach a real COMPLETED). This test class guards the
    restored invariant: native unresolvable always falls through to buffer
    analysis (via ``_resolve_buffer``, tested separately below), regardless of
    dispatch state or buffer emptiness. No provider overrides
    ``_resolve_native_status``, so ConcreteProvider exercises the real shared
    implementation.
    """

    def _provider(self):
        return ConcreteProvider("term-123", "session-1", "window-0")

    @pytest.mark.parametrize(
        "buffer, dispatched",
        [("", False), ("", True), (None, False), (None, True), ("some content", False)],
        ids=[
            "empty_not_dispatched",
            "empty_dispatched",
            "none_not_dispatched",
            "none_dispatched",
            "nonempty_buffer",
        ],
    )
    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_none_always_falls_through(self, mock_backend, buffer, dispatched):
        """native=None -> always None, regardless of buffer content or dispatch state.

        No guessing: the caller must fall through to buffer analysis on real
        pane content in every case -- this is the fail-fast/real-COMPLETED fix.
        """
        mock_backend.get_native_status.return_value = None
        provider = self._provider()
        provider._task_dispatched = dispatched
        assert provider._resolve_native_status(buffer) is None

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_none_default_buffer_arg_falls_through(self, mock_backend):
        """buffer defaults to None when omitted -- still falls through, not a guess."""
        mock_backend.get_native_status.return_value = None
        provider = self._provider()
        assert provider._resolve_native_status() is None


class TestResolveBuffer:
    """Tests for BaseProvider._resolve_buffer -- the herdr live-read fallback.

    On tmux the pushed StatusMonitor buffer is always populated by the FIFO
    pipeline, so this is a pass-through. On herdr, ``pipe_pane`` is a no-op and
    the pushed buffer is always empty; an empty pushed buffer there means "the
    push pipeline was never fed", not "the pane has no content" -- so this
    reads the backend's live pane content instead, letting the provider's own
    pattern matching run against real output rather than nothing.
    """

    def _provider(self):
        return ConcreteProvider("term-123", "session-1", "window-0")

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_nonempty_buffer_passthrough(self, mock_backend):
        """A non-empty buffer is returned unchanged -- no backend read at all."""
        provider = self._provider()
        assert provider._resolve_buffer("some content") == "some content"
        mock_backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_empty_buffer_tmux_passthrough(self, mock_backend):
        """tmux (supports_event_inbox=False): empty buffer stays empty, no live read."""
        mock_backend.supports_event_inbox.return_value = False
        provider = self._provider()
        assert provider._resolve_buffer("") == ""
        mock_backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_none_buffer_tmux_returns_empty_string(self, mock_backend):
        """tmux: a None buffer resolves to "" (never leaks None to callers)."""
        mock_backend.supports_event_inbox.return_value = False
        provider = self._provider()
        assert provider._resolve_buffer(None) == ""

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_empty_buffer_herdr_reads_live_history(self, mock_backend):
        """herdr (supports_event_inbox=True): empty pushed buffer -> live get_history() read."""
        mock_backend.supports_event_inbox.return_value = True
        mock_backend.get_history.return_value = "live pane content"
        provider = self._provider()
        assert provider._resolve_buffer("") == "live pane content"
        mock_backend.get_history.assert_called_once_with("session-1", "window-0")

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_none_buffer_herdr_reads_live_history(self, mock_backend):
        """herdr: a None pushed buffer also triggers the live read."""
        mock_backend.supports_event_inbox.return_value = True
        mock_backend.get_history.return_value = "live pane content"
        provider = self._provider()
        assert provider._resolve_buffer(None) == "live pane content"

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_herdr_live_read_failure_falls_back_to_empty(self, mock_backend):
        """A live read failure (backend hiccup) falls back to the pushed buffer, not a raise."""
        mock_backend.supports_event_inbox.return_value = True
        mock_backend.get_history.side_effect = RuntimeError("herdr socket closed")
        provider = self._provider()
        assert provider._resolve_buffer("") == ""


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
