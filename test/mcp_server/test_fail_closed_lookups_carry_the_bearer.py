"""The fail-closed authorization lookups authenticate their own API call (#990).

Two of the bearer-less call sites #990 found were not merely noisy -- they
were *silent*. ``_require_discovery_marker`` and the ``_target_binding_denial``
fetch inside ``_send_user_prompt_answer`` are documented fail-closed lookups:
they wrap their ``GET /terminals/{id}`` in a broad ``except`` and, on any
failure, return a refusal. On an auth-enabled server the bearer-less GET came
back ``401 missing bearer token``, which landed in that ``except`` and was
re-emitted as "this agent profile is not granted the 'discovery' tool" or a
generic delivery error -- a denial of function for exactly the callers the
gates exist to admit, with no visible transport error anywhere.

The rest of the suite asserts ``headers=None`` (the auth-disabled shape), which
cannot distinguish "sends the bearer" from "sends nothing". These tests turn
auth ON and assert on the header that actually goes out, plus the outcome the
401 used to corrupt.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.constants import API_BASE_URL

_SRV = "cli_agent_orchestrator.mcp_server.server"
TOKEN = "local-machine-token"


@pytest.fixture
def auth_enabled():
    """Enable the auth layer with a provisioned local token.

    ``get_local_bearer`` is default-off and reads ``CAO_AUTH_LOCAL_TOKEN``, so
    that is the whole switch -- no server needs to be running for a header
    assertion.
    """
    with patch.dict(
        os.environ,
        {"CAO_AUTH_LOCAL_TOKEN": TOKEN},
    ):
        from cli_agent_orchestrator.security.auth import get_local_bearer

        if get_local_bearer() != TOKEN:
            pytest.skip("auth layer did not accept CAO_AUTH_LOCAL_TOKEN in this environment")
        yield


def _ok(payload):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def _unauthorized():
    """What the API returns to a bearer-less request on a scope-guarded route."""
    import requests

    response = MagicMock()
    response.status_code = 401
    response.json.return_value = {"detail": "missing bearer token"}
    response.raise_for_status.side_effect = requests.HTTPError(
        "401 Client Error: Unauthorized", response=response
    )
    return response


class TestDiscoveryMarkerLookup:
    """``_require_discovery_marker`` -- the ``allowed_tools`` gate for #432's tools."""

    @patch(f"{_SRV}.requests.get")
    def test_lookup_sends_the_bearer(self, mock_get, auth_enabled):
        from cli_agent_orchestrator.mcp_server.server import _require_discovery_marker

        mock_get.return_value = _ok({"allowed_tools": "*"})

        assert _require_discovery_marker("caller-abc", "list siblings") is None

        headers = mock_get.call_args.kwargs["headers"]
        assert headers == {"Authorization": f"Bearer {TOKEN}"}

    @patch(f"{_SRV}.requests.get")
    def test_a_401_would_be_misreported_as_a_missing_discovery_grant(self, mock_get, auth_enabled):
        """Pin the symptom this fix removes, so the fail-closed branch stays honest.

        If the bearer ever stops being attached, the gate does not report a
        transport problem -- it reports the caller as ungranted. That is the
        whole reason these two sites were called out separately in #990.
        """
        from cli_agent_orchestrator.mcp_server.server import _require_discovery_marker

        mock_get.return_value = _unauthorized()

        denial = _require_discovery_marker("caller-abc", "list siblings")

        assert denial is not None
        assert denial["success"] is False
        assert "could not resolve this terminal's allowed_tools" in denial["error"]

    @patch(f"{_SRV}.requests.get")
    def test_list_siblings_sends_the_bearer_on_both_hops(self, mock_get, auth_enabled):
        """The marker lookup AND the /siblings fetch -- both routes are scope-guarded."""
        from cli_agent_orchestrator.mcp_server.server import _list_siblings_impl

        mock_get.side_effect = [_ok({"allowed_tools": "*"}), _ok([])]

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _list_siblings_impl(None)

        assert result["success"] is True
        assert [call.kwargs["headers"] for call in mock_get.call_args_list] == [
            {"Authorization": f"Bearer {TOKEN}"},
            {"Authorization": f"Bearer {TOKEN}"},
        ]

    @patch(f"{_SRV}.requests.patch")
    @patch(f"{_SRV}.requests.get")
    def test_update_metadata_sends_the_bearer_on_both_hops(
        self, mock_get, mock_patch, auth_enabled
    ):
        from cli_agent_orchestrator.mcp_server.server import _update_metadata_impl

        mock_get.return_value = _ok({"allowed_tools": "*"})
        mock_patch.return_value = _ok({"metadata": {"task": "reviewing"}})

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _update_metadata_impl({"task": "reviewing"})

        assert result["success"] is True
        assert mock_get.call_args.kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
        assert mock_patch.call_args.kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}


class TestAnswerUserPromptLookup:
    """``_send_user_prompt_answer`` -- the fetch that feeds ``_target_binding_denial``."""

    # Terminal IDs are validated as 8-char lowercase hex, so use real shapes.
    SUPERVISOR = "abcd1234"
    WORKER = "c0ffee01"

    @patch(f"{_SRV}.requests.post")
    @patch(f"{_SRV}.requests.get")
    def test_status_fetch_and_input_post_send_the_bearer(self, mock_get, mock_post, auth_enabled):
        from cli_agent_orchestrator.mcp_server.server import _send_user_prompt_answer
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        mock_get.return_value = _ok(
            {
                "id": self.WORKER,
                "caller_id": self.SUPERVISOR,
                "status": TerminalStatus.WAITING_USER_ANSWER.value,
                "provider": "mock_cli",
            }
        )
        mock_post.return_value = _ok({})

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": self.SUPERVISOR}):
            result = _send_user_prompt_answer(self.WORKER, "yes")

        assert result["success"] is True, result
        assert mock_get.call_args.kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
        assert mock_post.call_args.kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
        assert mock_get.call_args.args[0] == f"{API_BASE_URL}/terminals/{self.WORKER}"


class TestLoadSkill:
    """``GET /skills/{name}`` is scope-guarded; load_skill is on every agent's hot path."""

    @patch(f"{_SRV}.requests.get")
    def test_sends_the_bearer(self, mock_get, auth_enabled):
        from cli_agent_orchestrator.mcp_server.server import _load_skill_impl

        mock_get.return_value = _ok({"content": "# skill"})

        assert _load_skill_impl("python-testing") == "# skill"
        assert mock_get.call_args.kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}


class TestRemoteSitesDoNotLeakTheLocalBearer:
    """The orchestration sites take ``_auth_headers_for``, not ``_auth_headers`` (#822)."""

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_remote_profile_lookup_sends_no_local_bearer(self, mock_requests, auth_enabled):
        from cli_agent_orchestrator.utils.orchestration import _resolve_remote_provider

        mock_requests.get.return_value = _ok({"provider": "mock_cli"})
        mock_requests.get.return_value.status_code = 200

        _resolve_remote_provider("http://cao-worker-0:9889", "developer")

        assert mock_requests.get.call_args.kwargs["headers"] is None

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    def test_local_profile_lookup_does_send_the_bearer(self, mock_requests, auth_enabled):
        """Same call site, local base URL -- ``_auth_headers_for`` must still attach it."""
        from cli_agent_orchestrator.utils.orchestration import _resolve_remote_provider

        mock_requests.get.return_value = _ok({"provider": "mock_cli"})
        mock_requests.get.return_value.status_code = 200

        _resolve_remote_provider(API_BASE_URL, "developer")

        assert mock_requests.get.call_args.kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
