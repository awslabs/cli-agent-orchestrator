"""Tests for MCP server handoff logic.

Single-seam refactor (issue #312, N0): ``_handoff_impl`` was rewritten from a
six-call client-side loop into ONE call to ``POST /terminals/run-step``. These
tests preserve every OBSERVABLE behavior of the old suite (BR-8) — codex banner
content, no-banner for other providers, supervisor id from env, codex fast-fail
when CAO_TERMINAL_ID is unset, terminal_id surfacing, success on completion —
but assert them against the new single-call design rather than the old internal
mocks. (BR-8 explicitly makes observable behavior, not caller code, the
contract; the caller is deliberately rewritten.)
"""

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.mcp_server.server import (
    _handoff_impl,
    _shape_handoff_message,
)


def _ok_run_step_response(terminal_id="dev-term", last_message="task done"):
    """Build a mocked 200 response from POST /terminals/run-step."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "terminal_id": terminal_id,
        "last_message": last_message,
        "status": "completed",
    }
    resp.raise_for_status.return_value = None
    return resp


class TestShapeHandoffMessage:
    """The codex prompt-shaping that stays caller-side (was _send_direct_input_handoff)."""

    def test_codex_prepends_banner_with_supervisor_id(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-abc123"}):
            shaped = _shape_handoff_message("codex", "Implement hello world")
        assert shaped.startswith("[CAO Handoff]")
        assert "supervisor-abc123" in shaped
        assert "Implement hello world" in shaped
        assert "Do NOT use send_message" in shaped
        # Original message must appear in full AFTER the banner.
        assert shaped.endswith("Implement hello world")

    def test_non_codex_message_unchanged(self):
        for provider in ("claude_code", "kiro_cli"):
            assert _shape_handoff_message(provider, "Implement hello world") == (
                "Implement hello world"
            )

    def test_codex_no_env_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="CAO_TERMINAL_ID not set"):
                _shape_handoff_message("codex", "Do task")


class TestHandoffMessageContext:
    """Handoff sends the shaped prompt to the run-step endpoint."""

    @patch("cli_agent_orchestrator.mcp_server.server._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_codex_provider_sends_banner_to_endpoint(self, mock_provider, _nudge):
        """Codex handoff posts the [CAO Handoff] banner as the prompt."""
        mock_provider.return_value = "codex"

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-abc123"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
                mock_requests.post.return_value = _ok_run_step_response()
                mock_requests.Timeout = Exception

                result = asyncio.run(_handoff_impl("developer", "Implement hello world"))

        assert result.success is True
        # Exactly one combined call replaces the former six round-trips.
        mock_requests.post.assert_called_once()
        url = mock_requests.post.call_args[0][0]
        assert url.endswith("/terminals/run-step")
        sent_prompt = mock_requests.post.call_args[1]["json"]["prompt"]
        assert sent_prompt.startswith("[CAO Handoff]")
        assert "supervisor-abc123" in sent_prompt
        assert "Implement hello world" in sent_prompt
        assert "Do NOT use send_message" in sent_prompt

    @patch("cli_agent_orchestrator.mcp_server.server._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_claude_code_provider_no_banner(self, mock_provider, _nudge):
        mock_provider.return_value = "claude_code"

        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response()
            mock_requests.Timeout = Exception

            result = asyncio.run(_handoff_impl("developer", "Implement hello world"))

        assert result.success is True
        sent_prompt = mock_requests.post.call_args[1]["json"]["prompt"]
        assert sent_prompt == "Implement hello world"

    @patch("cli_agent_orchestrator.mcp_server.server._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_kiro_cli_provider_no_banner(self, mock_provider, _nudge):
        mock_provider.return_value = "kiro_cli"

        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response()
            mock_requests.Timeout = Exception

            result = asyncio.run(_handoff_impl("developer", "Implement hello world"))

        assert result.success is True
        sent_prompt = mock_requests.post.call_args[1]["json"]["prompt"]
        assert sent_prompt == "Implement hello world"

    @patch("cli_agent_orchestrator.mcp_server.server._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_codex_banner_supervisor_id_from_env(self, mock_provider, _nudge):
        mock_provider.return_value = "codex"

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "sup-xyz789"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
                mock_requests.post.return_value = _ok_run_step_response()
                mock_requests.Timeout = Exception

                asyncio.run(_handoff_impl("developer", "Build feature X"))

        sent_prompt = mock_requests.post.call_args[1]["json"]["prompt"]
        assert "sup-xyz789" in sent_prompt
        assert "Build feature X" in sent_prompt

    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_codex_fast_fail_when_no_env(self, mock_provider):
        """Codex handoff with no CAO_TERMINAL_ID fails visibly and never posts a
        step (issue #284) — never tell a worker its supervisor is 'unknown'."""
        mock_provider.return_value = "codex"

        with patch.dict(os.environ, {}, clear=True):
            with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
                mock_requests.Timeout = Exception
                result = asyncio.run(_handoff_impl("developer", "Do task"))

        assert result.success is False
        assert "CAO_TERMINAL_ID not set" in result.message
        # Fast-fail: no step is run at all.
        mock_requests.post.assert_not_called()
        # No terminal was created, so none to surface.
        assert result.terminal_id is None

    @patch("cli_agent_orchestrator.mcp_server.server._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_codex_original_message_preserved(self, mock_provider, _nudge):
        mock_provider.return_value = "codex"
        original = "Implement the task described in /path/to/task.md. Write tests."

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "sup-111"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
                mock_requests.post.return_value = _ok_run_step_response()
                mock_requests.Timeout = Exception
                asyncio.run(_handoff_impl("developer", original))

        sent_prompt = mock_requests.post.call_args[1]["json"]["prompt"]
        assert sent_prompt.endswith(original)

    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_terminal_id_none_when_provider_resolution_fails(self, mock_provider):
        """When provider resolution fails (no terminal created), report none."""
        mock_provider.side_effect = Exception("session not found")

        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task"))

        assert result.success is False
        assert "Handoff failed" in result.message
        assert result.terminal_id is None
        mock_requests.post.assert_not_called()


class TestHandoffOutcomes:
    """Success/failure outcome semantics preserved through the single endpoint."""

    @patch("cli_agent_orchestrator.mcp_server.server._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_success_returns_output_and_terminal_id(self, mock_provider, _nudge):
        """On success the worker output + terminal id are surfaced; the server
        owns teardown (the request asks for teardown=True)."""
        mock_provider.return_value = "kiro_cli"

        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response(
                terminal_id="dev-t1", last_message="done"
            )
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task"))

        assert result.success is True
        assert result.output == "done"
        assert result.terminal_id == "dev-t1"
        # The single combined call requests server-side teardown.
        assert mock_requests.post.call_args[1]["json"]["teardown"] is True

    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_endpoint_504_maps_to_timeout_result(self, mock_provider):
        """A 504 (step timeout / ERROR end-state) becomes a timeout failure and
        surfaces the live terminal id from the detail for cleanup."""
        mock_provider.return_value = "kiro_cli"

        timeout_resp = MagicMock()
        timeout_resp.status_code = 504
        timeout_resp.json.return_value = {
            "detail": "step on terminal a1b2c3d4 did not complete within 600s"
        }
        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
            mock_requests.post.return_value = timeout_resp
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task", timeout=600))

        assert result.success is False
        assert "timed out after 600 seconds" in result.message
        assert result.terminal_id == "a1b2c3d4"

    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_endpoint_500_maps_to_failure_result(self, mock_provider):
        mock_provider.return_value = "kiro_cli"

        err_resp = MagicMock()
        err_resp.status_code = 500
        err_resp.json.return_value = {"detail": "Failed to run step: boom"}
        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
            mock_requests.post.return_value = err_resp
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task"))

        assert result.success is False
        assert "Handoff failed" in result.message
        assert "boom" in result.message
