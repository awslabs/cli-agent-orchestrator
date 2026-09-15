"""Regression for #710: every MCP->API HTTP call honours server.mcp_request_timeout.

``server.mcp_request_timeout`` is documented (docs/configuration.md) as the number
of seconds to wait for HTTP calls between the MCP server process and the CAO API,
and #318 made it configurable by routing ``mcp_server/server.py`` through
``_mcp_timeout()``. The sibling helper modules in the same package kept the
hard-coded ``MCP_REQUEST_TIMEOUT`` default, so raising the setting left them at 30s.

The setting is written to the per-test settings file the ``_hermetic_cao_env``
fixture installs, so these exercise the real settings.json -> get_server_settings
-> call-site chain rather than a patched helper.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.mcp_server import app_tools
from cli_agent_orchestrator.mcp_server import utils as mcp_utils
from cli_agent_orchestrator.services import settings_service

# Deliberately not 30: the whole point is that the default is what the broken
# call sites returned, so a test asserting 30 would pass against the defect.
CONFIGURED_TIMEOUT = 120


@pytest.fixture
def configured_timeout():
    """Persist server.mcp_request_timeout to the hermetic per-test settings file."""

    settings_service.SETTINGS_FILE.write_text(
        json.dumps({"server": {"mcp_request_timeout": CONFIGURED_TIMEOUT}})
    )
    # The cache keys on the file's mtime and the fixture already cleared it, so
    # the next read picks the new file up without any further invalidation.
    assert settings_service.get_server_settings()["mcp_request_timeout"] == CONFIGURED_TIMEOUT
    yield CONFIGURED_TIMEOUT


def _ok_response():
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {}
    return response


class TestMcpServerUtilsHonourTheSetting:
    """``mcp_server/utils.py``'s three call sites read the configured value."""

    def test_get_json_uses_the_configured_timeout(self, configured_timeout):
        with patch.object(mcp_utils.requests, "get", return_value=_ok_response()) as get:
            mcp_utils.get_json("/terminals")

        assert get.call_args.kwargs["timeout"] == configured_timeout

    def test_post_body_json_uses_the_configured_timeout(self, configured_timeout):
        with patch.object(mcp_utils.requests, "post", return_value=_ok_response()) as post:
            mcp_utils.post_body_json("/terminals/t1/outcomes", {"note": "x"})

        assert post.call_args.kwargs["timeout"] == configured_timeout

    def test_get_terminal_record_uses_the_configured_timeout(self, configured_timeout):
        with patch.object(mcp_utils.requests, "get", return_value=_ok_response()) as get:
            mcp_utils.get_terminal_record("term-123")

        assert get.call_args.kwargs["timeout"] == configured_timeout

    def test_an_explicit_timeout_argument_still_wins(self, configured_timeout):
        """The per-call override keeps precedence over the setting.

        ``server.py`` passes ``timeout=_mcp_timeout()`` explicitly at one call
        site and the workflow tools pass the long blocking timeouts, so the
        argument has to keep beating the default.
        """

        with patch.object(mcp_utils.requests, "get", return_value=_ok_response()) as get:
            mcp_utils.get_json("/workflows/runs/r1", timeout=900.0)

        assert get.call_args.kwargs["timeout"] == 900.0


class TestAppToolsHonourTheSetting:
    """``mcp_server/app_tools.py``'s three call sites read the configured value."""

    def test_get_json_uses_the_configured_timeout(self, configured_timeout):
        with patch.object(app_tools.requests, "get", return_value=_ok_response()) as get:
            app_tools._get_json("/sessions")

        assert get.call_args.kwargs["timeout"] == configured_timeout

    def test_post_json_uses_the_configured_timeout(self, configured_timeout):
        with patch.object(app_tools.requests, "post", return_value=_ok_response()) as post:
            app_tools._post_json("/sessions", {"name": "s1"})

        assert post.call_args.kwargs["timeout"] == configured_timeout

    def test_delete_json_uses_the_configured_timeout(self, configured_timeout):
        with patch.object(app_tools.requests, "delete", return_value=_ok_response()) as delete:
            app_tools._delete_json("/terminals/term-123")

        assert delete.call_args.kwargs["timeout"] == configured_timeout


class TestTheDefaultIsUnchanged:
    """With no setting written, every call site keeps the documented 30s default."""

    def test_call_sites_still_default_to_thirty(self):
        assert settings_service.get_server_settings()["mcp_request_timeout"] == 30

        with patch.object(mcp_utils.requests, "get", return_value=_ok_response()) as get:
            mcp_utils.get_json("/terminals")
        assert get.call_args.kwargs["timeout"] == 30

        with patch.object(app_tools.requests, "get", return_value=_ok_response()) as get:
            app_tools._get_json("/sessions")
        assert get.call_args.kwargs["timeout"] == 30
