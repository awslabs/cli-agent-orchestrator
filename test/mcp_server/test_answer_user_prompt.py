"""Tests for answer_user_prompt MCP helper."""

from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.constants import API_BASE_URL, MCP_REQUEST_TIMEOUT


class TestAnswerUserPrompt:
    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    def test_delivers_answer_when_terminal_waits_for_user_answer(self, mock_requests):
        from cli_agent_orchestrator.mcp_server.server import _send_user_prompt_answer

        status_response = MagicMock()
        status_response.json.return_value = {"status": "waiting_user_answer"}
        status_response.raise_for_status.return_value = None
        input_response = MagicMock()
        input_response.raise_for_status.return_value = None
        mock_requests.get.return_value = status_response
        mock_requests.post.return_value = input_response

        result = _send_user_prompt_answer("abcd1234", "1")

        assert result["success"] is True
        mock_requests.get.assert_called_once_with(
            f"{API_BASE_URL}/terminals/abcd1234", timeout=MCP_REQUEST_TIMEOUT
        )
        mock_requests.post.assert_called_once_with(
            f"{API_BASE_URL}/terminals/abcd1234/input",
            params={
                "message": "1",
                "sender_id": "supervisor",
            },
            timeout=MCP_REQUEST_TIMEOUT,
        )

    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    def test_rejects_answer_when_terminal_is_not_waiting(self, mock_requests):
        from cli_agent_orchestrator.mcp_server.server import _send_user_prompt_answer

        status_response = MagicMock()
        status_response.json.return_value = {"status": "idle"}
        status_response.raise_for_status.return_value = None
        mock_requests.get.return_value = status_response

        result = _send_user_prompt_answer("abcd1234", "1")

        assert result["success"] is False
        assert result["status"] == "idle"
        assert "not waiting for a user answer" in result["message"]
        mock_requests.post.assert_not_called()

    @patch("cli_agent_orchestrator.mcp_server.server.time.sleep")
    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    def test_hermes_clarify_numeric_answer_uses_selection_keys(self, mock_requests, mock_sleep):
        from cli_agent_orchestrator.mcp_server.server import _send_user_prompt_answer

        status_response = MagicMock()
        status_response.json.return_value = {"status": "waiting_user_answer", "provider": "hermes"}
        status_response.raise_for_status.return_value = None
        output_response = MagicMock()
        output_response.json.return_value = {
            "output": "Hermes needs your input\nOther (type your answer)\n↑/↓ to select"
        }
        output_response.raise_for_status.return_value = None
        key_response = MagicMock()
        key_response.raise_for_status.return_value = None
        mock_requests.get.side_effect = [status_response, output_response]
        mock_requests.post.return_value = key_response

        result = _send_user_prompt_answer("abcd1234", "2")

        assert result["success"] is True
        assert result["message"] == "Hermes clarify option 2 selected."
        assert [call.kwargs["params"]["key"] for call in mock_requests.post.call_args_list] == [
            "Down",
            "Enter",
        ]
        mock_sleep.assert_called_once_with(0.05)

    @patch("cli_agent_orchestrator.mcp_server.server.time.sleep")
    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    def test_hermes_clarify_custom_answer_uses_other_then_text(self, mock_requests, mock_sleep):
        from cli_agent_orchestrator.mcp_server.server import _send_user_prompt_answer

        status_response = MagicMock()
        status_response.json.return_value = {"status": "waiting_user_answer", "provider": "hermes"}
        status_response.raise_for_status.return_value = None
        output_response = MagicMock()
        output_response.json.return_value = {
            "output": "Hermes needs your input\nOther (type your answer)\n↑/↓ to select"
        }
        output_response.raise_for_status.return_value = None
        post_response = MagicMock()
        post_response.raise_for_status.return_value = None
        mock_requests.get.side_effect = [status_response, output_response]
        mock_requests.post.return_value = post_response

        result = _send_user_prompt_answer("abcd1234", "自定义答案")

        assert result["success"] is True
        assert result["message"] == "Hermes clarify custom answer delivered."
        assert [call.kwargs["params"].get("key") for call in mock_requests.post.call_args_list[:4]] == [
            "Down",
            "Down",
            "Down",
            "Enter",
        ]
        assert mock_requests.post.call_args_list[-1].kwargs["params"] == {
            "message": "自定义答案",
            "sender_id": "supervisor",
        }
        assert mock_sleep.call_count == 4
