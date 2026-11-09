"""assign and send_message callback flags."""

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile

ORCH = "cli_agent_orchestrator.utils.orchestration"


def _profile(provider=None, mcp_servers=None):
    return AgentProfile(name="dev", description="d", provider=provider, mcpServers=mcp_servers)


class TestWorkerCanSendMessage:
    def test_profile_declares_cao_server(self):
        from cli_agent_orchestrator.utils.orchestration import _worker_can_send_message

        with patch(
            f"{ORCH}.load_agent_profile",
            return_value=_profile("claude_code", {"cao-mcp-server": {"command": "cao-mcp-server"}}),
        ):
            assert _worker_can_send_message("dev") is True

    def test_explicit_provider_without_cao_server(self):
        from cli_agent_orchestrator.utils.orchestration import _worker_can_send_message

        with patch(f"{ORCH}.load_agent_profile", return_value=_profile("claude_code", {})):
            assert _worker_can_send_message("dev") is False

    def test_server_under_another_name_found_by_its_args(self):
        from cli_agent_orchestrator.utils.orchestration import _worker_can_send_message

        servers = {
            "orchestrator": {
                "command": "uvx",
                "args": [
                    "--from",
                    "git+https://github.com/awslabs/cli-agent-orchestrator.git@main",
                    "cao-mcp-server",
                ],
            }
        }
        with patch(f"{ORCH}.load_agent_profile", return_value=_profile("claude_code", servers)):
            assert _worker_can_send_message("dev") is True

    def test_provider_that_injects_cao_server(self):
        from cli_agent_orchestrator.utils.orchestration import _worker_can_send_message

        with patch(f"{ORCH}.load_agent_profile", return_value=_profile("copilot_cli", {})):
            assert _worker_can_send_message("dev") is True

    def test_unknown_provider_assumes_yes(self):
        from cli_agent_orchestrator.utils.orchestration import _worker_can_send_message

        with patch(f"{ORCH}.load_agent_profile", return_value=_profile(None, {})):
            assert _worker_can_send_message("dev") is True

    def test_unloadable_profile_assumes_yes(self):
        from cli_agent_orchestrator.utils.orchestration import _worker_can_send_message

        with patch(f"{ORCH}.load_agent_profile", side_effect=FileNotFoundError):
            assert _worker_can_send_message("dev") is True


@pytest.fixture(autouse=True)
def _no_cleanup_nudge():
    with patch(f"{ORCH}._get_cleanup_nudge", return_value=""):
        yield


class TestAssignFlags:
    @patch(f"{ORCH}._worker_can_send_message", return_value=True)
    @patch(f"{ORCH}._create_terminal", return_value=("w1", "claude_code"))
    def test_default_expects_callback(self, create, _can):
        from cli_agent_orchestrator.utils.orchestration import _assign_impl

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            result = _assign_impl("developer", "do it")
        assert result["success"] is True
        assert create.call_args.kwargs["expect_callback"] is True
        assert create.call_args.kwargs["worker_can_reply"] is True
        assert "send results back" in create.call_args.kwargs["initial_message"]

    @patch(f"{ORCH}._worker_can_send_message", return_value=False)
    @patch(f"{ORCH}._create_terminal", return_value=("w1", "claude_code"))
    def test_worker_without_tool_warns_and_omits_suffix(self, create, _can):
        from cli_agent_orchestrator.utils.orchestration import _assign_impl

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            result = _assign_impl("developer", "do it")
        assert "cannot call send_message" in result["message"]
        assert "send results back" not in create.call_args.kwargs["initial_message"]
        assert create.call_args.kwargs["worker_can_reply"] is False

    @patch(f"{ORCH}.load_agent_profile", return_value=_profile("claude_code", {}))
    @patch(f"{ORCH}._create_terminal", return_value=("w1", "claude_code"))
    def test_claimed_worker_is_not_judged_by_its_profile(self, create, _load):
        """An ephemeral claim pins the provider, which the profile does not show.

        The profile here names claude_code and no cao-mcp-server, so judging the
        claimed worker by it would wrongly withhold the reply instruction.
        """
        from cli_agent_orchestrator.utils.orchestration import EphemeralClaim, _assign_impl

        claim = EphemeralClaim(provider="claude_code", effective_tools=[], claim_id="claim-1")
        with (
            patch(f"{ORCH}._claim_ephemeral", return_value=claim),
            patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}),
        ):
            result = _assign_impl("developer", "do it")
        assert result["success"] is True
        assert create.call_args.kwargs["claim"] == claim
        assert create.call_args.kwargs["worker_can_reply"] is True
        assert "send results back" in create.call_args.kwargs["initial_message"]
        assert "cannot call send_message" not in result["message"]

    @patch(f"{ORCH}._create_terminal", return_value=("w1", "claude_code"))
    def test_opt_out(self, create):
        from cli_agent_orchestrator.utils.orchestration import _assign_impl

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            _assign_impl("developer", "do it", expect_callback=False)
        assert create.call_args.kwargs["expect_callback"] is False
        # Opting out keeps today's instruction to reply, so the worker still
        # gets the callback suffix even when its profile cannot reply.
        assert "send results back" in create.call_args.kwargs["initial_message"]


class TestCreateTerminalParams:
    @patch(f"{ORCH}._resolve_child_allowed_tools", return_value=None)
    @patch(f"{ORCH}.resolve_provider", return_value="claude_code")
    @patch(f"{ORCH}.requests")
    def test_flags_sent_as_query_params(self, mock_requests, _rp, _at):
        from cli_agent_orchestrator.utils.orchestration import _create_terminal

        meta = MagicMock()
        meta.json.return_value = {
            "provider": "kiro_cli",
            "session_name": "s",
            "allowed_tools": None,
        }
        post = MagicMock()
        post.json.return_value = {"id": "w1", "provider": "claude_code"}
        mock_requests.get.return_value = meta
        mock_requests.post.return_value = post
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            _create_terminal(
                "dev",
                "/repo",
                defer_init=True,
                initial_message="x",
                expect_callback=True,
                worker_can_reply=False,
            )
        params = mock_requests.post.call_args.kwargs["params"]
        assert params["expect_callback"] == "true"
        assert params["worker_can_reply"] == "false"


class TestSendMessageFlag:
    @patch(f"{ORCH}._target_binding_denial", return_value=None)
    @patch(f"{ORCH}._send_to_inbox", return_value={"success": True})
    def test_expect_reply_passed(self, send, _binding):
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            _send_message_impl("bbbb2222", "hi", expect_reply=True)
        # The sender suffix is appended to the message, so only the receiver and
        # the flag are asserted exactly.
        assert send.call_args.args[0] == "bbbb2222"
        assert send.call_args.kwargs == {"expect_reply": True}


class TestToolsCalledDirectly:
    """Called without FastMCP, unset Field defaults arrive as FieldInfo objects.

    FieldInfo is truthy, so a tool that forwarded it would ask for a reply (or a
    callback) the caller never requested. The tools must pass plain booleans.
    """

    def test_send_message_without_flag_uses_two_argument_form(self):
        from cli_agent_orchestrator.mcp_server import server

        with patch.object(server, "_send_message_impl", return_value={"success": True}) as impl:
            asyncio.run(server.send_message("done", receiver_id="1234abcd"))
        impl.assert_called_once_with("1234abcd", "done")

    def test_send_message_with_flag_passes_true(self):
        from cli_agent_orchestrator.mcp_server import server

        with patch.object(server, "_send_message_impl", return_value={"success": True}) as impl:
            asyncio.run(server.send_message("done", receiver_id="1234abcd", expect_reply=True))
        impl.assert_called_once_with("1234abcd", "done", expect_reply=True)

    @pytest.mark.parametrize(
        ("given", "expected"), [({}, True), ({"expect_callback": False}, False)]
    )
    def test_assign_passes_a_plain_boolean(self, given, expected):
        from cli_agent_orchestrator.mcp_server import server

        with (
            patch.object(server, "_tool_denied_reason", return_value=None),
            patch.object(server, "_assign_impl", return_value={"success": True}) as impl,
        ):
            asyncio.run(server.assign("developer", "do it", **given))
        value = impl.call_args.kwargs["expect_callback"]
        assert type(value) is bool and value is expected


def _server_with_working_directory():
    """A separate copy of the MCP server module, imported with the
    ``working_directory`` variant of ``assign`` (CAO_ENABLE_WORKING_DIRECTORY=true).

    The variant is chosen at import time, so the default import never defines
    it. The copy has its own FastMCP instance and leaves the imported module
    and ``sys.modules`` untouched.
    """
    import importlib.util

    from cli_agent_orchestrator.mcp_server import server

    spec = importlib.util.spec_from_file_location(
        "_cao_server_with_working_directory", server.__file__
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"CAO_ENABLE_WORKING_DIRECTORY": "true"}):
        spec.loader.exec_module(module)
    assert module.ENABLE_WORKING_DIRECTORY is True
    return module


class TestWorkingDirectoryAssignCalledDirectly:
    @pytest.mark.parametrize(
        ("given", "expected"), [({}, True), ({"expect_callback": False}, False)]
    )
    def test_assign_passes_a_plain_boolean(self, given, expected):
        server = _server_with_working_directory()

        with (
            patch.object(server, "_tool_denied_reason", return_value=None),
            patch.object(server, "_assign_impl", return_value={"success": True}) as impl,
        ):
            asyncio.run(server.assign("developer", "do it", working_directory="/repo", **given))
        assert impl.call_args.args[2] == "/repo"
        value = impl.call_args.kwargs["expect_callback"]
        assert type(value) is bool and value is expected
