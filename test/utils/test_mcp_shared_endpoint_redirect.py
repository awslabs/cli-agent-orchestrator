"""A configured shared endpoint redirects the bundled MCP server (#745).

Found by validating acceptance criterion 12 on EKS: the supervisor could not
delegate at all. `assign_elastic` resolves the broker from
`CAO_ELASTIC_BROKER_URL`/`_TOKEN`, which the example deliberately puts ONLY on
the central server — an agent pod with a broker URL and a route to port 9890
would undo the boundary the broker exists to be. But the supervisor still
launched its provider against a local stdio `cao-mcp-server`, so the tool ran in
the pod that has no broker credentials and answered:

    elastic workers are not configured: set CAO_ELASTIC_BROKER_URL and
    CAO_ELASTIC_BROKER_TOKEN on the supervisor

The shim and the shared HTTP endpoint were both built and tested, and nothing
ever pointed a provider at them. This is that one wire: when `CAO_MCP_HTTP_URL`
is set, the bundled `cao-mcp-server` becomes `cao-mcp-stdio-bridge` with the
endpoint and token in the child env, so the tools execute in the server pod
where the credentials and the central state already are — and the agent pod
needs neither.

The deliberate non-behaviours, each with a test here: nothing changes when no
endpoint is configured (local installs are untouched); a command that is not the
bundled one is never redirected; and the substitution does not wait for a token
to be present, because the alternative to a shim that fails loudly is a full MCP
server running in the agent's own pod.
"""

import json

import pytest

from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_MCP_SERVER_COMMAND,
    CAO_MCP_STDIO_BRIDGE_COMMAND,
    RUNTIME_TOKEN_ENV,
    SHARED_ENDPOINT_URL_ENV,
    resolve_cao_mcp_command,
    resolve_mcp_server_config,
    shared_endpoint_child_env,
)

ENDPOINT = "http://cao-server.cao-cluster.svc.cluster.local:9891/mcp"


@pytest.fixture()
def shared_endpoint(monkeypatch):
    monkeypatch.setenv(SHARED_ENDPOINT_URL_ENV, ENDPOINT)
    monkeypatch.setenv(RUNTIME_TOKEN_ENV, "runtime-token-value")


@pytest.fixture(autouse=True)
def no_endpoint_by_default(monkeypatch):
    """Every test states its own posture; none inherits the developer's env."""
    monkeypatch.delenv(SHARED_ENDPOINT_URL_ENV, raising=False)
    monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)


def _is_shim(command, args):
    """The shim, however it resolved: script path or module entrypoint."""
    return command.endswith(CAO_MCP_STDIO_BRIDGE_COMMAND) or (
        "-m" in args and any("stdio_bridge" in a for a in args)
    )


def _is_local_server(command, args):
    return command.endswith(CAO_MCP_SERVER_COMMAND) or (
        "-m" in args and any(a.endswith("mcp_server.server") for a in args)
    )


class TestRedirectWhenConfigured:
    def test_bundled_command_becomes_the_shim(self, shared_endpoint):
        command, args = resolve_cao_mcp_command(CAO_MCP_SERVER_COMMAND, [])
        assert _is_shim(command, args), (command, args)
        assert not _is_local_server(command, args), (command, args)

    def test_the_child_env_carries_endpoint_and_token(self, shared_endpoint):
        resolved = resolve_mcp_server_config({"command": CAO_MCP_SERVER_COMMAND, "args": []})
        assert resolved["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT
        assert resolved["env"][RUNTIME_TOKEN_ENV] == "runtime-token-value"

    def test_a_profiles_own_env_is_not_overwritten(self, shared_endpoint):
        """An explicit value in the profile is a deliberate override."""
        resolved = resolve_mcp_server_config(
            {
                "command": CAO_MCP_SERVER_COMMAND,
                "args": [],
                "env": {SHARED_ENDPOINT_URL_ENV: "http://elsewhere/mcp"},
            }
        )
        assert resolved["env"][SHARED_ENDPOINT_URL_ENV] == "http://elsewhere/mcp"

    def test_other_config_keys_survive_the_redirect(self, shared_endpoint):
        resolved = resolve_mcp_server_config(
            {"command": CAO_MCP_SERVER_COMMAND, "args": [], "type": "stdio", "disabled": False}
        )
        assert resolved["type"] == "stdio"
        assert resolved["disabled"] is False

    def test_substitution_does_not_wait_for_a_token(self, monkeypatch):
        """A missing token must surface as the shim's own fatal startup error,
        not be routed around by starting a full MCP server in the agent's pod —
        that server is exactly what this topology removes."""
        monkeypatch.setenv(SHARED_ENDPOINT_URL_ENV, ENDPOINT)
        monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)

        command, args = resolve_cao_mcp_command(CAO_MCP_SERVER_COMMAND, [])
        assert _is_shim(command, args), (command, args)
        # And the token is omitted rather than sent empty, so the shim reports
        # it as absent instead of presenting a credential of "".
        assert RUNTIME_TOKEN_ENV not in shared_endpoint_child_env()


class TestUntouchedWithoutAnEndpoint:
    def test_local_resolution_is_unchanged(self):
        command, args = resolve_cao_mcp_command(CAO_MCP_SERVER_COMMAND, [])
        assert _is_local_server(command, args), (command, args)
        assert not _is_shim(command, args), (command, args)

    def test_no_env_is_injected(self):
        resolved = resolve_mcp_server_config({"command": CAO_MCP_SERVER_COMMAND, "args": []})
        assert "env" not in resolved
        assert shared_endpoint_child_env() == {}

    def test_an_empty_url_is_not_an_endpoint(self, monkeypatch):
        monkeypatch.setenv(SHARED_ENDPOINT_URL_ENV, "   ")
        command, args = resolve_cao_mcp_command(CAO_MCP_SERVER_COMMAND, [])
        assert _is_local_server(command, args), (command, args)


class TestOnlyTheBundledCommand:
    def test_a_third_party_server_is_never_redirected(self, shared_endpoint):
        """Redirecting someone else's MCP server would point it at an endpoint
        that does not advertise its tools, silently removing them."""
        assert resolve_cao_mcp_command("my-own-mcp", ["--flag"]) == ("my-own-mcp", ["--flag"])
        resolved = resolve_mcp_server_config({"command": "my-own-mcp", "args": ["--flag"]})
        assert resolved == {"command": "my-own-mcp", "args": ["--flag"]}
        assert "env" not in resolved

    def test_a_command_less_entry_is_untouched(self, shared_endpoint):
        resolved = resolve_mcp_server_config({"type": "http", "url": "http://x/mcp"})
        assert resolved == {"type": "http", "url": "http://x/mcp"}

    def test_an_entry_already_naming_the_shim_is_not_double_handled(self, shared_endpoint):
        command, args = resolve_cao_mcp_command(CAO_MCP_STDIO_BRIDGE_COMMAND, [])
        assert _is_shim(command, args), (command, args)


class TestProvidersInheritIt:
    """The point of doing this in the resolver: no provider knows about it."""

    def test_claude_code_writes_a_shim_entry(self, shared_endpoint, monkeypatch, tmp_path):
        from cli_agent_orchestrator.providers import claude_code as cc

        monkeypatch.setattr(cc, "CAO_HOME_DIR", tmp_path)

        entry = resolve_mcp_server_config(
            {"command": CAO_MCP_SERVER_COMMAND, "args": [], "type": "stdio"}
        )
        # Same call the provider makes at line 478, then its own env additions.
        env = entry.get("env", {})
        env["CAO_TERMINAL_ID"] = "abcd1234"
        entry["env"] = env
        written = json.loads(json.dumps({"mcpServers": {"cao-mcp-server": entry}}))

        wired = written["mcpServers"]["cao-mcp-server"]
        assert _is_shim(wired["command"], wired["args"])
        assert wired["env"]["CAO_TERMINAL_ID"] == "abcd1234"
        assert wired["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT

    def test_copilot_runtime_config_carries_the_endpoint(self, shared_endpoint):
        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider

        provider = CopilotCliProvider.__new__(CopilotCliProvider)
        provider.terminal_id = "abcd1234"
        config = json.loads(provider._build_runtime_mcp_config())

        entry = config["mcpServers"]["cao-mcp-server"]
        assert _is_shim(entry["command"], entry["args"])
        assert entry["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT
        assert entry["env"]["CAO_TERMINAL_ID"] == "abcd1234"

    def test_opencode_translation_carries_the_endpoint(self, shared_endpoint):
        from cli_agent_orchestrator.utils.opencode_config import translate_mcp_server_config

        translated = translate_mcp_server_config({"command": CAO_MCP_SERVER_COMMAND, "args": []})
        assert any("stdio_bridge" in part for part in translated["command"]) or translated[
            "command"
        ][0].endswith(CAO_MCP_STDIO_BRIDGE_COMMAND)
        assert translated["environment"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT

    def test_opencode_gains_no_environment_key_when_unconfigured(self):
        from cli_agent_orchestrator.utils.opencode_config import translate_mcp_server_config

        translated = translate_mcp_server_config({"command": CAO_MCP_SERVER_COMMAND, "args": []})
        assert "environment" not in translated
