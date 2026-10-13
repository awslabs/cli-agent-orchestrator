"""Terminal-targeting MCP tools are bound to the caller's assign/handoff tree.

``delete_terminal``, ``answer_user_prompt`` and ``send_message`` (explicit
receiver) used to forward any terminal ID the agent named straight to the
HTTP API under the shared operator bearer. ``_target_binding_denial`` now
refuses, in the MCP server process, any target that is not the caller, a
terminal the caller created, or (for ``send_message``) the caller's creator.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.utils.orchestration import (
    _delete_terminal_impl,
    _mcp_timeout,
    _send_message_impl,
    _target_binding_denial,
)

OWN = "abcd1234"
CHILD = "c0ffee01"
PARENT = "0badf00d"
SIBLING = "deadbeef"


def _terminal(terminal_id: str, caller_id=None, status="idle"):
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {"id": terminal_id, "caller_id": caller_id, "status": status}
    return response


def _not_found():
    response = MagicMock()
    response.status_code = 404
    return response


class TestTargetBindingDenial:
    def test_no_caller_identity_is_passthrough(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
                assert _target_binding_denial(SIBLING) is None
                mock_requests.get.assert_not_called()

    def test_own_terminal_allowed_without_lookup(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
                assert _target_binding_denial(OWN) is None
                mock_requests.get.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_child_allowed(self, mock_requests):
        mock_requests.get.return_value = _terminal(CHILD, caller_id=OWN)
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            assert _target_binding_denial(CHILD) is None
        mock_requests.get.assert_called_once_with(
            f"{API_BASE_URL}/terminals/{CHILD}", headers=None, timeout=_mcp_timeout()
        )

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_sibling_refused(self, mock_requests):
        mock_requests.get.return_value = _terminal(SIBLING, caller_id=PARENT)
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            denial = _target_binding_denial(SIBLING)
        assert denial is not None
        assert OWN in denial and SIBLING in denial
        assert "created it" not in denial

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_parent_refused_unless_allow_parent(self, mock_requests):
        # Target row (the parent) was created by nobody we know; own row names it.
        mock_requests.get.side_effect = [
            _terminal(PARENT, caller_id=None),
            _terminal(PARENT, caller_id=None),
            _terminal(OWN, caller_id=PARENT),
        ]
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            assert _target_binding_denial(PARENT) is not None
            assert _target_binding_denial(PARENT, allow_parent=True) is None

    def test_remote_callback_parent_allowed_without_lookup(self):
        env = {
            "CAO_TERMINAL_ID": OWN,
            "CAO_CALLBACK_URL": "http://supervisor:9889",
            "CAO_CALLBACK_TERMINAL_ID": PARENT,
        }
        with patch.dict(os.environ, env):
            with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
                assert _target_binding_denial(PARENT, allow_parent=True) is None
                mock_requests.get.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_unknown_target_is_left_to_the_tool(self, mock_requests):
        mock_requests.get.return_value = _not_found()
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            assert _target_binding_denial(SIBLING) is None

    def test_lookup_failure_fails_closed(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            with patch(
                "cli_agent_orchestrator.utils.orchestration.requests.get",
                side_effect=requests.ConnectionError("down"),
            ):
                denial = _target_binding_denial(SIBLING)
        assert denial is not None
        assert "Could not verify" in denial

    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    def test_server_error_on_lookup_fails_closed(self, mock_get):
        response = MagicMock()
        response.status_code = 503
        response.raise_for_status.side_effect = requests.HTTPError("503 Server Error")
        mock_get.return_value = response
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            denial = _target_binding_denial(SIBLING)
        assert denial is not None
        assert "Could not verify" in denial

    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    def test_non_json_body_on_lookup_fails_closed(self, mock_get):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("not json")
        mock_get.return_value = response
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            denial = _target_binding_denial(SIBLING)
        assert denial is not None
        assert "Could not verify" in denial

    def test_supplied_meta_skips_lookup(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
                assert _target_binding_denial(CHILD, target_meta={"caller_id": OWN}) is None
                assert _target_binding_denial(SIBLING, target_meta={"caller_id": PARENT})
                mock_requests.get.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_remote_target_is_looked_up_on_its_node_without_the_local_bearer(self, mock_requests):
        mock_requests.get.return_value = _terminal(CHILD, caller_id=OWN)
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN, "CAO_AUTH_LOCAL_TOKEN": "tok"}):
            assert _target_binding_denial(CHILD, base_url="http://node-b:9889") is None
        url = mock_requests.get.call_args[0][0]
        assert url == "http://node-b:9889/terminals/" + CHILD
        assert mock_requests.get.call_args[1]["headers"] is None


class TestDeleteTerminalBinding:
    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_worker_cannot_delete_a_sibling(self, mock_requests):
        mock_requests.get.return_value = _terminal(SIBLING, caller_id=PARENT)
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            result = _delete_terminal_impl(SIBLING)
        assert result["success"] is False
        assert "may only act on" in result["message"]
        mock_requests.delete.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_supervisor_can_delete_its_own_worker(self, mock_requests):
        mock_requests.get.return_value = _terminal(CHILD, caller_id=OWN)
        delete_response = MagicMock()
        delete_response.status_code = 200
        delete_response.raise_for_status.return_value = None
        delete_response.json.return_value = {"success": True}
        mock_requests.delete.return_value = delete_response
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            result = _delete_terminal_impl(CHILD)
        assert result["success"] is True
        mock_requests.delete.assert_called_once()

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_operator_shell_is_unchanged(self, mock_requests):
        delete_response = MagicMock()
        delete_response.status_code = 200
        delete_response.raise_for_status.return_value = None
        delete_response.json.return_value = {"success": True}
        mock_requests.delete.return_value = delete_response
        with patch.dict(os.environ, {}, clear=True):
            result = _delete_terminal_impl(SIBLING)
        assert result["success"] is True
        mock_requests.get.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_internal_caller_can_opt_out(self, mock_requests):
        delete_response = MagicMock()
        delete_response.status_code = 200
        delete_response.raise_for_status.return_value = None
        delete_response.json.return_value = {"success": True}
        mock_requests.delete.return_value = delete_response
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            result = _delete_terminal_impl(SIBLING, check_binding=False)
        assert result["success"] is True
        mock_requests.get.assert_not_called()


class TestSendMessageBinding:
    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_explicit_sibling_refused(self, mock_requests, mock_inbox):
        mock_requests.get.side_effect = [
            _terminal(SIBLING, caller_id=PARENT),
            _terminal(OWN, caller_id=PARENT),
        ]
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            result = _send_message_impl(SIBLING, "psst")
        assert result["success"] is False
        assert "created it" in result["error"]
        mock_inbox.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_explicit_parent_allowed(self, mock_requests, mock_inbox):
        mock_inbox.return_value = {"success": True}
        mock_requests.get.side_effect = [
            _terminal(PARENT, caller_id=None),
            _terminal(OWN, caller_id=PARENT),
        ]
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            _send_message_impl(PARENT, "done")
        mock_inbox.assert_called_once()
        assert mock_inbox.call_args[0][0] == PARENT

    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_explicit_child_allowed(self, mock_requests, mock_inbox):
        mock_inbox.return_value = {"success": True}
        mock_requests.get.return_value = _terminal(CHILD, caller_id=OWN)
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            _send_message_impl(CHILD, "more work")
        mock_inbox.assert_called_once()
        assert mock_inbox.call_args[0][0] == CHILD

    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_omitted_receiver_uses_recorded_caller_with_no_extra_check(
        self, mock_requests, mock_inbox
    ):
        mock_inbox.return_value = {"success": True}
        mock_requests.get.return_value = _terminal(OWN, caller_id=PARENT)
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            _send_message_impl(None, "done")
        # One GET: the own-row lookup that resolves the recorded caller.
        assert mock_requests.get.call_count == 1
        assert mock_inbox.call_args[0][0] == PARENT


class TestAnswerUserPromptBinding:
    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    def test_worker_cannot_answer_a_siblings_prompt(self, mock_requests):
        from cli_agent_orchestrator.mcp_server.server import _send_user_prompt_answer

        mock_requests.get.return_value = _terminal(
            SIBLING, caller_id=PARENT, status="waiting_user_answer"
        )
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            result = _send_user_prompt_answer(SIBLING, "1")
        assert result["success"] is False
        assert "may only act on" in result["error"]
        mock_requests.post.assert_not_called()

    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    def test_in_terminal_supervisor_can_answer_its_workers_prompt(self, mock_requests):
        from cli_agent_orchestrator.mcp_server.server import _send_user_prompt_answer

        mock_requests.get.return_value = _terminal(
            CHILD, caller_id=OWN, status="waiting_user_answer"
        )
        input_response = MagicMock()
        input_response.raise_for_status.return_value = None
        mock_requests.post.return_value = input_response
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            result = _send_user_prompt_answer(CHILD, "1")
        assert result["success"] is True
        mock_requests.post.assert_called_once_with(
            f"{API_BASE_URL}/terminals/{CHILD}/input",
            params={"message": "1", "sender_id": OWN},
            timeout=_mcp_timeout(),
        )

    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    def test_external_client_without_identity_is_unchanged(self, mock_requests):
        from cli_agent_orchestrator.mcp_server.server import _send_user_prompt_answer

        mock_requests.get.return_value = _terminal(
            SIBLING, caller_id=PARENT, status="waiting_user_answer"
        )
        input_response = MagicMock()
        input_response.raise_for_status.return_value = None
        mock_requests.post.return_value = input_response
        with patch.dict(os.environ, {}, clear=True):
            result = _send_user_prompt_answer(SIBLING, "1")
        assert result["success"] is True
        assert mock_requests.post.call_args[1]["params"]["sender_id"] == "supervisor"
