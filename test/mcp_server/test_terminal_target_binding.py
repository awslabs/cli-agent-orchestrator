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


NO_DISCOVERY = ["@cao-mcp-server"]
WITH_DISCOVERY = ["@cao-mcp-server", "discovery"]


def _terminal(
    terminal_id: str,
    caller_id=None,
    status="idle",
    *,
    allowed_tools=NO_DISCOVERY,
    callback_terminal_id=None,
):
    """A GET /terminals/{id} response in the shape production writes.

    ``allowed_tools`` defaults to a restricted set WITHOUT ``discovery``: None
    means unrestricted (which includes the grant), so a test that wants the
    no-grant case must not leave it unset by accident.
    """
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "id": terminal_id,
        "caller_id": caller_id,
        "callback_terminal_id": callback_terminal_id,
        "allowed_tools": allowed_tools,
        "status": status,
    }
    return response


def _siblings(*terminal_ids: str):
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = [
        {"id": tid, "group": ["tenant", "project"], "metadata": None, "status": "idle"}
        for tid in terminal_ids
    ]
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
        # The row shape a remote node really writes: caller_id is None (the
        # supervisor has no row there); the creator is callback_terminal_id.
        mock_requests.get.return_value = _terminal(CHILD, caller_id=None, callback_terminal_id=OWN)
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN, "CAO_AUTH_LOCAL_TOKEN": "tok"}):
            assert _target_binding_denial(CHILD, base_url="http://node-b:9889") is None
        url = mock_requests.get.call_args[0][0]
        assert url == "http://node-b:9889/terminals/" + CHILD
        assert mock_requests.get.call_args[1]["headers"] is None

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_remote_row_created_by_someone_else_is_refused(self, mock_requests):
        mock_requests.get.return_value = _terminal(
            SIBLING, caller_id=None, callback_terminal_id=PARENT
        )
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            assert _target_binding_denial(SIBLING, base_url="http://node-b:9889") is not None

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_remote_node_that_reports_no_creator_is_refused(self, mock_requests):
        """An older remote node, or one restarted since creation, omits the
        field or returns None: fail closed rather than guess."""
        old_node = MagicMock()
        old_node.status_code = 200
        old_node.raise_for_status.return_value = None
        old_node.json.return_value = {"id": CHILD, "caller_id": None, "status": "idle"}
        mock_requests.get.return_value = old_node
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            assert _target_binding_denial(CHILD, base_url="http://node-b:9889") is not None

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_callback_field_is_ignored_on_a_local_row(self, mock_requests):
        """A LOCAL row's callback_terminal_id names a supervisor on another
        node; a local caller matching it would be a coincidence, not creation."""
        mock_requests.get.return_value = _terminal(
            SIBLING, caller_id=PARENT, callback_terminal_id=OWN
        )
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            assert _target_binding_denial(SIBLING) is not None


class TestDiscoveredSiblingAllowance:
    """send_message keeps the #432 peer flow: with the discovery grant, a
    receiver that GET /terminals/{own}/siblings returns is allowed."""

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_sibling_allowed_with_grant_and_membership(self, mock_requests):
        mock_requests.get.side_effect = [
            _terminal(SIBLING, caller_id=PARENT),
            _terminal(OWN, caller_id=PARENT, allowed_tools=WITH_DISCOVERY),
            _siblings(SIBLING, "f00dface"),
        ]
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            denial = _target_binding_denial(
                SIBLING, allow_parent=True, allow_discovered_sibling=True
            )
        assert denial is None
        siblings_call = mock_requests.get.call_args_list[2]
        assert siblings_call[0][0] == f"{API_BASE_URL}/terminals/{OWN}/siblings"
        assert siblings_call[1]["params"] == {"cross_session": "true"}

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_unrestricted_caller_holds_the_grant(self, mock_requests):
        mock_requests.get.side_effect = [
            _terminal(SIBLING, caller_id=PARENT),
            _terminal(OWN, caller_id=PARENT, allowed_tools=["*"]),
            _siblings(SIBLING),
        ]
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            assert _target_binding_denial(SIBLING, allow_discovered_sibling=True) is None

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_sibling_refused_without_the_grant_and_siblings_not_consulted(self, mock_requests):
        mock_requests.get.side_effect = [
            _terminal(SIBLING, caller_id=PARENT),
            _terminal(OWN, caller_id=PARENT, allowed_tools=NO_DISCOVERY),
        ]
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            denial = _target_binding_denial(
                SIBLING, allow_parent=True, allow_discovered_sibling=True
            )
        assert denial is not None
        assert "list_siblings" in denial
        assert mock_requests.get.call_count == 2

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_non_member_refused_even_with_the_grant(self, mock_requests):
        mock_requests.get.side_effect = [
            _terminal(SIBLING, caller_id=PARENT),
            _terminal(OWN, caller_id=PARENT, allowed_tools=WITH_DISCOVERY),
            _siblings("f00dface"),
        ]
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            assert _target_binding_denial(SIBLING, allow_discovered_sibling=True) is not None

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_group_less_caller_has_no_siblings_and_is_refused(self, mock_requests):
        mock_requests.get.side_effect = [
            _terminal(SIBLING, caller_id=PARENT),
            _terminal(OWN, caller_id=PARENT, allowed_tools=WITH_DISCOVERY),
            _siblings(),
        ]
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            assert _target_binding_denial(SIBLING, allow_discovered_sibling=True) is not None

    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    def test_siblings_lookup_failure_fails_closed(self, mock_get):
        mock_get.side_effect = [
            _terminal(SIBLING, caller_id=PARENT),
            _terminal(OWN, caller_id=PARENT, allowed_tools=WITH_DISCOVERY),
            requests.ConnectionError("down"),
        ]
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            denial = _target_binding_denial(SIBLING, allow_discovered_sibling=True)
        assert denial is not None
        assert "Could not verify" in denial

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_delete_and_answer_paths_do_not_get_the_allowance(self, mock_requests):
        """Only send_message opts in; the default call refuses a sibling even
        when the caller holds the grant, and never consults the sibling list."""
        mock_requests.get.return_value = _terminal(SIBLING, caller_id=PARENT)
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            assert _target_binding_denial(SIBLING) is not None
        assert mock_requests.get.call_count == 1


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
    def test_supervisor_can_delete_its_own_remote_worker(self, mock_requests):
        """haofeif's repro on 12287870: the remote row has caller_id None and
        the supervisor only as callback_terminal_id; the DELETE must go out."""
        mock_requests.get.return_value = _terminal(CHILD, caller_id=None, callback_terminal_id=OWN)
        delete_response = MagicMock()
        delete_response.status_code = 200
        delete_response.raise_for_status.return_value = None
        delete_response.json.return_value = {"success": True}
        mock_requests.delete.return_value = delete_response
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            result = _delete_terminal_impl(CHILD, target_host="node-b:9889")
        assert result["success"] is True
        mock_requests.delete.assert_called_once()
        assert mock_requests.delete.call_args[0][0] == "http://node-b:9889/terminals/" + CHILD

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_remote_worker_of_another_supervisor_is_refused(self, mock_requests):
        mock_requests.get.return_value = _terminal(
            SIBLING, caller_id=None, callback_terminal_id=PARENT
        )
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            result = _delete_terminal_impl(SIBLING, target_host="node-b:9889")
        assert result["success"] is False
        mock_requests.delete.assert_not_called()

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
    def test_explicit_sibling_refused_without_discovery_grant(self, mock_requests, mock_inbox):
        mock_requests.get.side_effect = [
            _terminal(SIBLING, caller_id=PARENT),
            _terminal(OWN, caller_id=PARENT, allowed_tools=NO_DISCOVERY),
        ]
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            result = _send_message_impl(SIBLING, "psst")
        assert result["success"] is False
        assert "created it" in result["error"]
        assert "list_siblings" in result["error"]
        mock_inbox.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_explicit_discovered_sibling_allowed_with_grant(self, mock_requests, mock_inbox):
        """The #432 contract: list_siblings, then send_message to the returned id."""
        mock_inbox.return_value = {"success": True}
        mock_requests.get.side_effect = [
            _terminal(SIBLING, caller_id=PARENT),
            _terminal(OWN, caller_id=PARENT, allowed_tools=WITH_DISCOVERY),
            _siblings(SIBLING),
        ]
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            result = _send_message_impl(SIBLING, "hello neighbour")
        assert result["success"] is True
        mock_inbox.assert_called_once()
        assert mock_inbox.call_args[0][0] == SIBLING

    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_explicit_non_sibling_refused_with_grant(self, mock_requests, mock_inbox):
        mock_requests.get.side_effect = [
            _terminal(SIBLING, caller_id=PARENT),
            _terminal(OWN, caller_id=PARENT, allowed_tools=WITH_DISCOVERY),
            _siblings("f00dface"),
        ]
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": OWN}):
            result = _send_message_impl(SIBLING, "psst")
        assert result["success"] is False
        mock_inbox.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_explicit_parent_allowed(self, mock_requests, mock_inbox):
        mock_inbox.return_value = {"success": True}
        mock_requests.get.side_effect = [
            _terminal(PARENT, caller_id=None),
            _terminal(OWN, caller_id=PARENT, allowed_tools=NO_DISCOVERY),
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
            headers=None,
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
