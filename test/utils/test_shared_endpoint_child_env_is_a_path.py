"""The forwarded MCP child env carries a token PATH, never the value (#802).

After the structural fix, ``shared_endpoint_child_env`` returns only the
endpoint URL and ``CAO_RUNTIME_TOKEN_FILE`` — in both the persisted and the
live form. The value never appears in a child's environment, so a provider pane
and any MCP child it spawns cannot read the credential out of the env.
"""

import pytest

from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_MCP_SERVER_COMMAND,
    RUNTIME_TOKEN_ENV,
    RUNTIME_TOKEN_FILE_ENV,
    SHARED_ENDPOINT_URL_ENV,
    shared_endpoint_child_env,
    shared_endpoint_child_env_for,
)

TOKEN = "s3cret-channel-token-value"
ENDPOINT = "http://cao-server:9891/mcp"


@pytest.fixture(autouse=True)
def _endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.CAO_HOME_DIR", str(tmp_path), raising=False
    )
    monkeypatch.setenv(SHARED_ENDPOINT_URL_ENV, ENDPOINT)
    monkeypatch.setenv(RUNTIME_TOKEN_ENV, TOKEN)


@pytest.mark.parametrize("persisted", [True, False])
def test_the_value_is_never_in_the_child_env(persisted):
    env = shared_endpoint_child_env(persisted=persisted)
    assert RUNTIME_TOKEN_ENV not in env
    assert TOKEN not in env.values()
    assert env[RUNTIME_TOKEN_FILE_ENV]  # a path is delivered instead
    assert env[SHARED_ENDPOINT_URL_ENV] == ENDPOINT


def test_a_third_party_command_gets_neither_key():
    assert shared_endpoint_child_env_for("my-own-mcp") == {}
    assert shared_endpoint_child_env_for("") == {}


@pytest.mark.parametrize("persisted", [True, False])
def test_the_bundled_command_gets_only_the_path_form(persisted):
    env = shared_endpoint_child_env_for(CAO_MCP_SERVER_COMMAND, persisted=persisted)
    assert RUNTIME_TOKEN_ENV not in env
    assert TOKEN not in env.values()
    assert env[RUNTIME_TOKEN_FILE_ENV]
