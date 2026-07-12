"""Tests for assign MCP tool."""

import os
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.mcp_server.server import _build_assign_description, _mcp_timeout


class TestCreateTerminalProviderResolution:
    """Tests for provider resolution used by dispatched worker terminals."""

    @patch(
        "cli_agent_orchestrator.mcp_server.server._resolve_child_allowed_tools", return_value=None
    )
    @patch("cli_agent_orchestrator.mcp_server.server.resolve_provider", return_value="claude_code")
    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    def test_existing_session_respects_child_profile_provider(
        self, mock_requests, mock_resolve_provider, mock_allowed_tools
    ):
        """Worker profile provider should override the supervisor provider."""
        from cli_agent_orchestrator.mcp_server.server import _create_terminal

        metadata_response = MagicMock()
        metadata_response.json.return_value = {
            "provider": "kiro_cli",
            "session_name": "cao-session",
            "allowed_tools": None,
        }
        metadata_response.raise_for_status.return_value = None

        post_response = MagicMock()
        post_response.json.return_value = {"id": "worker-1", "provider": "claude_code"}
        post_response.raise_for_status.return_value = None

        mock_requests.get.return_value = metadata_response
        mock_requests.post.return_value = post_response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-1"}):
            terminal_id, provider = _create_terminal("reviewer", "/repo")

        assert terminal_id == "worker-1"
        assert provider == "claude_code"
        mock_resolve_provider.assert_called_once_with("reviewer", fallback_provider="kiro_cli")
        mock_requests.post.assert_called_once_with(
            f"{API_BASE_URL}/sessions/cao-session/terminals",
            params={
                "provider": "claude_code",
                "agent_profile": "reviewer",
                "caller_id": "supervisor-1",
                "working_directory": "/repo",
            },
            json=None,
            timeout=_mcp_timeout(),
        )

    @patch(
        "cli_agent_orchestrator.mcp_server.server._resolve_child_allowed_tools", return_value=None
    )
    @patch("cli_agent_orchestrator.mcp_server.server.resolve_provider", return_value="kiro_cli")
    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    def test_existing_session_falls_back_to_supervisor_provider(
        self, mock_requests, mock_resolve_provider, mock_allowed_tools
    ):
        """Worker without a provider should inherit the supervisor provider."""
        from cli_agent_orchestrator.mcp_server.server import _create_terminal

        metadata_response = MagicMock()
        metadata_response.json.return_value = {
            "provider": "kiro_cli",
            "session_name": "cao-session",
            "allowed_tools": None,
        }
        metadata_response.raise_for_status.return_value = None

        post_response = MagicMock()
        post_response.json.return_value = {"id": "worker-2", "provider": "kiro_cli"}
        post_response.raise_for_status.return_value = None

        mock_requests.get.return_value = metadata_response
        mock_requests.post.return_value = post_response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-1"}):
            terminal_id, provider = _create_terminal("reviewer", "/repo")

        assert terminal_id == "worker-2"
        assert provider == "kiro_cli"
        mock_resolve_provider.assert_called_once_with("reviewer", fallback_provider="kiro_cli")
        mock_requests.post.assert_called_once_with(
            f"{API_BASE_URL}/sessions/cao-session/terminals",
            params={
                "provider": "kiro_cli",
                "agent_profile": "reviewer",
                "caller_id": "supervisor-1",
                "working_directory": "/repo",
            },
            json=None,
            timeout=_mcp_timeout(),
        )

    @patch(
        "cli_agent_orchestrator.mcp_server.server._resolve_child_allowed_tools", return_value=None
    )
    @patch("cli_agent_orchestrator.mcp_server.server.resolve_provider", return_value="kiro_cli")
    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    def test_deferred_init_sends_message_in_json_body_not_params(
        self, mock_requests, mock_resolve_provider, mock_allowed_tools
    ):
        """defer_init must carry the prompt in the JSON body (not the query
        string) so prompt content isn't logged in HTTP access logs and isn't
        subject to URL-length limits."""
        from cli_agent_orchestrator.mcp_server.server import _create_terminal
        from cli_agent_orchestrator.models.inbox import OrchestrationType

        metadata_response = MagicMock()
        metadata_response.json.return_value = {
            "provider": "kiro_cli",
            "session_name": "cao-session",
            "allowed_tools": None,
        }
        metadata_response.raise_for_status.return_value = None
        post_response = MagicMock()
        post_response.json.return_value = {"id": "worker-1", "provider": "kiro_cli"}
        post_response.raise_for_status.return_value = None
        mock_requests.get.return_value = metadata_response
        mock_requests.post.return_value = post_response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-1"}):
            _create_terminal(
                "reviewer",
                working_directory=None,
                defer_init=True,
                initial_message="Analyze the sensitive logs at /secret/path",
                initial_message_orchestration_type=OrchestrationType.ASSIGN,
            )

        _, kwargs = mock_requests.post.call_args
        # Routing flag stays in params; message payload is in the body.
        assert kwargs["params"].get("defer_init") == "true"
        assert "initial_message" not in kwargs["params"]
        assert kwargs["json"]["initial_message"] == "Analyze the sensitive logs at /secret/path"
        assert kwargs["json"]["initial_message_orchestration_type"] == "assign"

    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    def test_defer_init_on_new_session_branch_raises(self, mock_requests):
        """PR #390 must-fix #2: the new-session branch can't honor defer_init
        (POST /sessions has no deferred-init support), so _create_terminal must
        raise rather than silently create a worker whose task is never
        delivered. This is the branch taken when CAO_TERMINAL_ID is unset."""
        from cli_agent_orchestrator.mcp_server.server import _create_terminal
        from cli_agent_orchestrator.models.inbox import OrchestrationType

        with patch.dict(os.environ, {}, clear=True):  # no CAO_TERMINAL_ID
            with pytest.raises(ValueError, match="not supported when creating a new session"):
                _create_terminal(
                    "reviewer",
                    defer_init=True,
                    initial_message="do work",
                    initial_message_orchestration_type=OrchestrationType.ASSIGN,
                )
        # Must raise BEFORE creating anything.
        mock_requests.post.assert_not_called()


class TestAssignSenderIdInjection:
    """Tests for sender ID injection in _assign_impl.

    _assign_impl now uses the deferred-init path: it composes the callback-
    instructions suffix on the MCP-server side and passes the full message
    to ``_create_terminal`` via ``initial_message`` so cao-server can deliver
    it in the background once the worker's provider finishes initializing.
    The tool-call itself returns as soon as the tmux window/DB row exist.
    """

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    def test_assign_appends_sender_id_when_injection_enabled(self, mock_create):
        """When injection is enabled, assign should pass a message with the
        sender ID suffix as ``initial_message`` to _create_terminal."""
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        mock_create.return_value = ("worker-1", "claude_code")

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-abc123"}):
            result = _assign_impl("developer", "Analyze the logs")

        assert result["success"] is True
        # _create_terminal is called with defer_init=True and the composed message
        _, kwargs = mock_create.call_args
        assert kwargs["defer_init"] is True
        sent_message = kwargs["initial_message"]
        assert sent_message.startswith("Analyze the logs")
        assert "[Assigned by terminal supervisor-abc123" in sent_message
        assert "send results back to terminal supervisor-abc123 using send_message]" in sent_message
        # And the orchestration_type is ASSIGN so plugin events see it
        from cli_agent_orchestrator.models.inbox import OrchestrationType

        assert kwargs["initial_message_orchestration_type"] == OrchestrationType.ASSIGN

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    def test_assign_no_suffix_when_injection_disabled(self, mock_create):
        """When injection is disabled, assign should pass the message unchanged."""
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        mock_create.return_value = ("worker-2", "claude_code")

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-abc123"}):
            result = _assign_impl("developer", "Analyze the logs")

        assert result["success"] is True
        _, kwargs = mock_create.call_args
        assert kwargs["initial_message"] == "Analyze the logs"

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    def test_assign_missing_terminal_id_errors_before_creating_terminal(self, mock_create):
        """When CAO_TERMINAL_ID is not set, assign must fail fast (issue #284) —
        never tell a worker to reply to terminal 'unknown', and never leave an
        orphan worker terminal behind."""
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        with patch.dict(os.environ, {}, clear=True):
            result = _assign_impl("developer", "Build feature X")

        assert result["success"] is False
        assert result["terminal_id"] is None
        assert "CAO_TERMINAL_ID not set" in result["message"]
        mock_create.assert_not_called()

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    def test_assign_missing_terminal_id_fails_fast_even_with_injection_off(self, mock_create):
        """PR #390 must-fix #2: the CAO_TERMINAL_ID fail-fast must be
        UNCONDITIONAL (not gated on sender-ID injection). With injection off and
        no terminal id, the deferred path would otherwise take the new-session
        branch, which can't deliver the task — assign would create a worker,
        drop the task, and still return success. Guard fires regardless."""
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        with patch.dict(os.environ, {}, clear=True):
            result = _assign_impl("developer", "Build feature X")

        assert result["success"] is False
        assert result["terminal_id"] is None
        assert "CAO_TERMINAL_ID not set" in result["message"]
        mock_create.assert_not_called()

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    def test_assign_surfaces_terminal_id_when_create_fails(self, mock_create):
        """If _create_terminal fails, the returned dict should carry
        ``terminal_id=None`` and a failure message."""
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        mock_create.side_effect = Exception("connection refused")

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-abc123"}):
            result = _assign_impl("developer", "Analyze the logs")

        assert result["success"] is False
        assert result["terminal_id"] is None
        assert "Assignment failed" in result["message"]

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    def test_assign_suffix_is_appended_not_prepended(self, mock_create):
        """The sender ID should be a suffix, not a prefix."""
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        mock_create.return_value = ("worker-4", "claude_code")
        original = "Do the task described in /path/to/task.md"

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "sup-111"}):
            _assign_impl("developer", original)

        _, kwargs = mock_create.call_args
        sent_message = kwargs["initial_message"]
        assert sent_message.startswith(original)
        assert sent_message.index("[Assigned by terminal") > len(original)

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    def test_assign_returns_fast_success_message(self, mock_create):
        """Regression: assign() should tell the LLM the worker is initializing
        in the background, not claim the message has been delivered."""
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        mock_create.return_value = ("worker-fast", "kiro_cli")

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-abc123"}):
            result = _assign_impl("developer", "Do work")

        assert result["success"] is True
        assert result["terminal_id"] == "worker-fast"
        # The message must reflect deferred delivery so the LLM does not
        # falsely conclude the worker has already received the task.
        assert "initializing" in result["message"].lower()


class TestBuildAssignDescription:
    """Tests for the _build_assign_description helper.

    Covers all four combinations of (enable_sender_id, enable_workdir) flags.
    """

    # ------------------------------------------------------------------
    # Shared content assertions
    # ------------------------------------------------------------------

    def test_always_starts_with_action_sentence(self):
        """All combinations begin with the same one-liner action summary."""
        for sender_id in (True, False):
            for workdir in (True, False):
                desc = _build_assign_description(sender_id, workdir)
                assert desc.startswith("Assigns a task to another agent without blocking.")

    def test_always_contains_args_section(self):
        """All combinations include an Args section with agent_profile and message."""
        for sender_id in (True, False):
            for workdir in (True, False):
                desc = _build_assign_description(sender_id, workdir)
                assert "Args:" in desc
                assert "agent_profile:" in desc
                assert "message:" in desc

    def test_always_contains_returns_section(self):
        """All combinations include a Returns section."""
        for sender_id in (True, False):
            for workdir in (True, False):
                desc = _build_assign_description(sender_id, workdir)
                assert "Returns:" in desc
                assert "Dict with success status" in desc

    # ------------------------------------------------------------------
    # Sender ID injection flag
    # ------------------------------------------------------------------

    def test_sender_id_enabled_uses_auto_injection_overview(self):
        """When sender ID injection is on, overview says ID is automatically appended."""
        desc = _build_assign_description(enable_sender_id=True, enable_workdir=False)
        assert "automatically be appended" in desc

    def test_sender_id_enabled_omits_manual_callback_instructions(self):
        """When injection is on, no manual CAO_TERMINAL_ID instructions are included."""
        desc = _build_assign_description(enable_sender_id=True, enable_workdir=False)
        assert "CAO_TERMINAL_ID" not in desc
        assert "send results back" not in desc

    def test_sender_id_disabled_includes_manual_callback_instructions(self):
        """When injection is off, the description instructs the caller to include callback info."""
        desc = _build_assign_description(enable_sender_id=False, enable_workdir=False)
        assert "CAO_TERMINAL_ID" in desc
        assert "send results back" in desc
        assert "Example message:" in desc

    def test_sender_id_disabled_omits_auto_injection_mention(self):
        """When injection is off, no mention of automatic appending."""
        desc = _build_assign_description(enable_sender_id=False, enable_workdir=False)
        assert "automatically be appended" not in desc

    # ------------------------------------------------------------------
    # Working directory flag
    # ------------------------------------------------------------------

    def test_workdir_enabled_includes_working_directory_section(self):
        """When workdir is enabled, a '## Working Directory' section is present."""
        desc = _build_assign_description(enable_sender_id=False, enable_workdir=True)
        assert "## Working Directory" in desc
        assert "supervisor's current working directory" in desc

    def test_workdir_enabled_includes_working_directory_arg(self):
        """When workdir is on, working_directory appears in the Args section."""
        desc = _build_assign_description(enable_sender_id=False, enable_workdir=True)
        assert "working_directory:" in desc

    def test_workdir_disabled_omits_working_directory_section(self):
        """When workdir is off, no Working Directory section."""
        desc = _build_assign_description(enable_sender_id=False, enable_workdir=False)
        assert "## Working Directory" not in desc

    def test_workdir_disabled_omits_working_directory_arg(self):
        """When workdir is off, working_directory does not appear in Args."""
        desc = _build_assign_description(enable_sender_id=False, enable_workdir=False)
        assert "working_directory:" not in desc

    # ------------------------------------------------------------------
    # All four flag combinations
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "enable_sender_id, enable_workdir",
        [
            (False, False),
            (False, True),
            (True, False),
            (True, True),
        ],
    )
    def test_returns_non_empty_string(self, enable_sender_id, enable_workdir):
        """All combinations produce a non-empty string."""
        desc = _build_assign_description(enable_sender_id, enable_workdir)
        assert isinstance(desc, str)
        assert len(desc) > 0

    def test_sender_id_true_workdir_true(self):
        """Both flags on: auto-injection overview + Working Directory section present."""
        desc = _build_assign_description(enable_sender_id=True, enable_workdir=True)
        assert "automatically be appended" in desc
        assert "## Working Directory" in desc
        assert "working_directory:" in desc
        assert "CAO_TERMINAL_ID" not in desc

    def test_sender_id_true_workdir_false(self):
        """Injection on, workdir off: no Working Directory section."""
        desc = _build_assign_description(enable_sender_id=True, enable_workdir=False)
        assert "automatically be appended" in desc
        assert "## Working Directory" not in desc
        assert "working_directory:" not in desc

    def test_sender_id_false_workdir_true(self):
        """Injection off, workdir on: manual callback instructions + Working Directory."""
        desc = _build_assign_description(enable_sender_id=False, enable_workdir=True)
        assert "CAO_TERMINAL_ID" in desc
        assert "## Working Directory" in desc
        assert "working_directory:" in desc

    def test_sender_id_false_workdir_false(self):
        """Both flags off: manual callback instructions, no Working Directory section."""
        desc = _build_assign_description(enable_sender_id=False, enable_workdir=False)
        assert "CAO_TERMINAL_ID" in desc
        assert "## Working Directory" not in desc
        assert "working_directory:" not in desc

    # ------------------------------------------------------------------
    # Structural ordering
    # ------------------------------------------------------------------

    def test_args_section_appears_after_overview(self):
        """The Args section should come after the overview text."""
        desc = _build_assign_description(enable_sender_id=False, enable_workdir=False)
        overview_pos = desc.index("Assigns a task")
        args_pos = desc.index("Args:")
        assert overview_pos < args_pos

    def test_working_directory_section_appears_before_args(self):
        """The Working Directory section should come before the Args section."""
        desc = _build_assign_description(enable_sender_id=False, enable_workdir=True)
        workdir_pos = desc.index("## Working Directory")
        args_pos = desc.index("Args:")
        assert workdir_pos < args_pos

    def test_returns_section_appears_after_args(self):
        """The Returns section should come after the Args section."""
        desc = _build_assign_description(enable_sender_id=False, enable_workdir=False)
        args_pos = desc.index("Args:")
        returns_pos = desc.index("Returns:")
        assert args_pos < returns_pos
