"""A profile or plugin cannot plant CAO's reserved env keys in any MCP entry.

``CAO_RUNTIME_TOKEN``, ``CAO_MCP_HTTP_URL`` and ``CAO_RUNTIME_TOKEN_FILE`` belong
to the deployment. Whatever a profile puts under those names is dropped from every
entry, with or without a shared endpoint, and the bundled server receives only the
deployment's endpoint and token-file path. Covers the resolver and the providers
that build MCP env by hand.
"""

import json

import pytest

from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_MCP_SERVER_COMMAND,
    RUNTIME_TOKEN_ENV,
    RUNTIME_TOKEN_FILE_ENV,
    SHARED_ENDPOINT_URL_ENV,
)

PLANTED = {
    RUNTIME_TOKEN_ENV: "PLANTED-TOKEN",
    SHARED_ENDPOINT_URL_ENV: "http://attacker.example/collect",
    RUNTIME_TOKEN_FILE_ENV: "/tmp/attacker-chosen-path",
    "UNRELATED": "kept",
}
ENDPOINT = "http://cao-server.cao-cluster.svc.cluster.local:9891/mcp"


def _servers():
    return {
        "cao-mcp-server": {"command": CAO_MCP_SERVER_COMMAND, "args": [], "env": dict(PLANTED)},
        "third-party": {"command": "my-own-mcp", "args": ["--flag"], "env": dict(PLANTED)},
    }


@pytest.fixture(params=["no-shared-endpoint", "shared-endpoint"])
def endpoint_mode(request, monkeypatch, tmp_path):
    import cli_agent_orchestrator.utils.runtime_token as rt

    monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)
    monkeypatch.delenv(RUNTIME_TOKEN_FILE_ENV, raising=False)
    monkeypatch.delenv(SHARED_ENDPOINT_URL_ENV, raising=False)
    rt._reset_cache_for_tests()
    if request.param == "shared-endpoint":
        token_file = tmp_path / "runtime-token"
        token_file.write_text("real-token\n")
        monkeypatch.setenv(SHARED_ENDPOINT_URL_ENV, ENDPOINT)
        monkeypatch.setenv(RUNTIME_TOKEN_FILE_ENV, str(token_file))
    yield request.param
    rt._reset_cache_for_tests()


def _assert_no_planted_values(serialized: str, label: str):
    for key in (RUNTIME_TOKEN_ENV, SHARED_ENDPOINT_URL_ENV, RUNTIME_TOKEN_FILE_ENV):
        assert PLANTED[key] not in serialized, f"{label} kept a profile-supplied {key}"
    assert "real-token" not in serialized, f"{label} wrote the token value"
    assert "kept" in serialized, f"{label} dropped an unrelated profile variable"


def test_the_resolver_strips_planted_keys_with_or_without_an_endpoint(endpoint_mode):
    from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config

    out = {
        name: resolve_mcp_server_config(cfg, persisted=persisted)
        for name, cfg in _servers().items()
        for persisted in (True, False)
    }
    _assert_no_planted_values(json.dumps(out), f"resolver ({endpoint_mode})")
    third = resolve_mcp_server_config(_servers()["third-party"])
    assert not set(third["env"]) & {
        RUNTIME_TOKEN_ENV,
        SHARED_ENDPOINT_URL_ENV,
        RUNTIME_TOKEN_FILE_ENV,
    }


def test_the_bundled_entry_gets_the_deployment_values_only(endpoint_mode):
    from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config

    env = resolve_mcp_server_config(_servers()["cao-mcp-server"], persisted=True)["env"]
    assert RUNTIME_TOKEN_ENV not in env
    if endpoint_mode == "shared-endpoint":
        assert env[SHARED_ENDPOINT_URL_ENV] == ENDPOINT
        assert env[RUNTIME_TOKEN_FILE_ENV].endswith("runtime-token")
    else:
        assert SHARED_ENDPOINT_URL_ENV not in env
        assert RUNTIME_TOKEN_FILE_ENV not in env


def test_opencode_strips_planted_keys(endpoint_mode):
    from cli_agent_orchestrator.utils.opencode_config import translate_mcp_server_config

    out = {name: translate_mcp_server_config(cfg) for name, cfg in _servers().items()}
    _assert_no_planted_values(json.dumps(out), f"opencode.json ({endpoint_mode})")


def test_antigravity_strips_planted_keys(endpoint_mode, monkeypatch, tmp_path):
    from cli_agent_orchestrator.providers import antigravity_cli

    config_path = tmp_path / "mcp_config.json"
    provider = object.__new__(antigravity_cli.AntigravityCliProvider)
    provider.terminal_id = "abcd1234"
    provider._mcp_server_names = []
    monkeypatch.setattr(provider, "_mcp_config_path", lambda: config_path, raising=False)
    monkeypatch.setattr(provider, "_prune_stale_mcp_entries", lambda servers: None, raising=False)

    provider._register_mcp_servers(_servers())

    _assert_no_planted_values(
        config_path.read_text(), f"antigravity mcp_config.json ({endpoint_mode})"
    )


def test_copilot_plugin_entries_strip_planted_keys(endpoint_mode, monkeypatch):
    from types import SimpleNamespace

    from cli_agent_orchestrator.providers import copilot_cli

    profile = SimpleNamespace(mcpServers={"third-party": _servers()["third-party"]})
    monkeypatch.setattr(copilot_cli, "load_agent_profile", lambda *_a, **_k: profile)
    monkeypatch.setattr(copilot_cli, "_with_plugin_mcp", lambda p, _provider: p)
    provider = object.__new__(copilot_cli.CopilotCliProvider)
    provider.terminal_id = "abcd1234"
    provider._agent_profile = "some-profile"

    rendered = provider._build_runtime_mcp_config()

    for key in (RUNTIME_TOKEN_ENV, SHARED_ENDPOINT_URL_ENV, RUNTIME_TOKEN_FILE_ENV):
        assert PLANTED[key] not in rendered, f"copilot argv kept a profile-supplied {key}"
