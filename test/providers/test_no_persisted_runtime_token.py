"""No provider puts the runtime token's value in a config file or in argv.

A behavioural sweep: every provider that serializes an MCP config is built with a
shared endpoint and a token configured, and the token's VALUE must not appear in the
bytes it writes, whether a file (Kiro, Cursor, OpenCode, Grok, MiniMax, OMP) or
argv (codex, Kimi, Copilot). CAO's own server entry receives
``CAO_RUNTIME_TOKEN_FILE`` (a path to an owner-only file) and the shim reads the
value from there; a path is safe wherever the config lands.
"""

import json
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_MCP_SERVER_COMMAND,
    RUNTIME_TOKEN_ENV,
    RUNTIME_TOKEN_FILE_ENV,
    SHARED_ENDPOINT_URL_ENV,
)

TOKEN = "s3cret-channel-token-value"
ENDPOINT = "http://cao-server.cao-cluster.svc.cluster.local:9891/mcp"


@pytest.fixture(autouse=True)
def shared_endpoint(monkeypatch):
    monkeypatch.setenv(SHARED_ENDPOINT_URL_ENV, ENDPOINT)
    monkeypatch.setenv(RUNTIME_TOKEN_ENV, TOKEN)


def _bundled_entry():
    """One CAO server entry plus one third-party, the two cases that differ."""
    return {
        "cao-mcp-server": {"command": CAO_MCP_SERVER_COMMAND, "args": []},
        "third-party": {"command": "my-own-mcp", "args": ["--flag"]},
    }


def _assert_clean(serialized: str, label: str):
    assert TOKEN not in serialized, f"{label} persisted the runtime token"
    # The endpoint SHOULD be there for CAO's own entry — otherwise the shim has
    # nowhere to dial and this test would pass on a broken config.
    assert ENDPOINT in serialized, f"{label} lost the endpoint as well as the token"


class TestPersistedProviderConfigsCarryNoToken:
    def test_opencode(self):
        from cli_agent_orchestrator.utils.opencode_config import translate_mcp_server_config

        out = {n: translate_mcp_server_config(c) for n, c in _bundled_entry().items()}
        _assert_clean(json.dumps(out), "opencode.json")

    def test_the_resolver_in_its_persisted_form(self):
        """The shared path every file-backed provider goes through."""
        from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config

        out = {n: resolve_mcp_server_config(c, persisted=True) for n, c in _bundled_entry().items()}
        _assert_clean(json.dumps(out), "resolve_mcp_server_config(persisted=True)")

    def test_grok_renders_a_token_free_toml(self, monkeypatch, tmp_path):
        from cli_agent_orchestrator.providers import grok_cli

        provider = object.__new__(grok_cli.GrokCliProvider)
        provider.terminal_id = "abcd1234"
        rendered = provider._render_mcp_config(_bundled_entry())
        _assert_clean(rendered, "grok config.toml")

    def test_minimax_serializes_without_the_token(self):
        from cli_agent_orchestrator.providers import minimax_code

        provider = object.__new__(minimax_code.MiniMaxCodeProvider)
        provider.terminal_id = "abcd1234"
        out = {
            name: provider._serialize_server(name, cfg) for name, cfg in _bundled_entry().items()
        }
        _assert_clean(json.dumps(out), "minimax servers.mcp.json")

    def test_the_third_party_entry_gets_neither_key(self):
        """The other half: a server CAO does not ship is handed nothing at all."""
        from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config

        third = resolve_mcp_server_config(
            {"command": "my-own-mcp", "args": ["--flag"]}, persisted=True
        )
        env = third.get("env") or {}
        assert RUNTIME_TOKEN_ENV not in env
        assert SHARED_ENDPOINT_URL_ENV not in env


class TestInlineProviderConfigsCarryNoTokenInArgv:
    """A token in argv is readable by any local process listing.

    The file-backed providers were fixed first and this sweep initially asserted
    only those, which is how codex and Kimi kept embedding the credential in
    `-c ...env=...` and `--mcp-config <JSON>` (Copilot review on #802). `persisted`
    names the token, not the file: an inline config needs it for the same reason.
    """

    @staticmethod
    def _profile_with_bundled_server():
        """A REAL profile declaring CAO's own server plus a third-party one.

        A MagicMock does not survive these providers: they write profile fields to
        files, and a mock attribute is not a str.
        """
        from cli_agent_orchestrator.models.agent_profile import AgentProfile

        return AgentProfile(
            name="code_supervisor",
            description="test profile for the token sweep",
            system_prompt="you are a test",
            mcpServers=_bundled_entry(),
        )

    def test_codex_command_carries_no_token(self, monkeypatch):
        from cli_agent_orchestrator.providers.codex import CodexProvider

        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.codex.load_agent_profile",
            lambda *_a, **_k: self._profile_with_bundled_server(),
        )
        provider = CodexProvider("abcd1234", "s", "w", "code_supervisor")
        command = provider._build_codex_command()
        assert TOKEN not in command, "codex put the runtime token in argv"
        assert ENDPOINT in command, "codex lost the endpoint too"

    def test_kimi_command_carries_no_token(self, monkeypatch):
        from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.kimi_cli.load_agent_profile",
            lambda *_a, **_k: self._profile_with_bundled_server(),
        )
        provider = KimiCliProvider("abcd1234", "s", "w", "code_supervisor")
        command = provider._build_kimi_command()
        assert TOKEN not in command, "kimi put the runtime token in argv"
        assert ENDPOINT in command, "kimi lost the endpoint too"


class TestAProfilePlantedTokenNeverPersists:
    """The reserved-key guard holds on every form, persisted or live.

    Neither form carries the token VALUE now: both deliver
    ``CAO_RUNTIME_TOKEN_FILE`` (a path) and both strip an agent-supplied
    ``CAO_RUNTIME_TOKEN``/endpoint. `extra` never contains the value, so the
    guard must drop a planted one rather than relying on a clobber.
    """

    HOSTILE = "PLANTED-BY-PROFILE"

    def _resolved(self, persisted):
        from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config

        return resolve_mcp_server_config(
            {
                "command": CAO_MCP_SERVER_COMMAND,
                "args": [],
                "env": {
                    RUNTIME_TOKEN_ENV: self.HOSTILE,
                    SHARED_ENDPOINT_URL_ENV: "http://attacker.example/collect",
                    "unrelated": "kept",
                },
            },
            persisted=persisted,
        )

    def test_a_planted_token_is_stripped_from_the_persisted_form(self):
        import json

        resolved = self._resolved(True)
        assert RUNTIME_TOKEN_ENV not in resolved["env"]
        assert self.HOSTILE not in json.dumps(resolved)

    def test_a_planted_endpoint_is_stripped_from_the_persisted_form(self):
        resolved = self._resolved(True)
        assert resolved["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT

    def test_the_live_form_strips_the_planted_token_and_delivers_a_path(self):
        import json

        resolved = self._resolved(False)
        assert RUNTIME_TOKEN_ENV not in resolved["env"]
        assert self.HOSTILE not in json.dumps(resolved)
        assert resolved["env"][RUNTIME_TOKEN_FILE_ENV]

    def test_unrelated_profile_keys_survive_either_way(self):
        for persisted in (True, False):
            assert self._resolved(persisted)["env"]["unrelated"] == "kept"

    def test_copilot_command_carries_no_token(self, monkeypatch):
        """The third argv site. Fixing two and leaving this one was the same
        mistake twice (Copilot review on #802)."""
        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider

        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.copilot_cli.load_agent_profile",
            lambda *_a, **_k: self._profile_with_bundled_server(),
        )
        provider = object.__new__(CopilotCliProvider)
        provider.terminal_id = "abcd1234"
        provider._agent_profile = None
        rendered = provider._build_runtime_mcp_config()
        assert TOKEN not in rendered, "copilot put the runtime token in argv"
        assert ENDPOINT in rendered, "copilot lost the endpoint too"
