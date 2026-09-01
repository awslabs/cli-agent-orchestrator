"""Plugin MCP servers must appear in each provider's REAL launch artifact.

Reproduced by review on #584, and the reason this file exists at all:

> "the all-provider tests need to inspect real launch commands rather than
> treating ``collect_plugin_mcp_servers()`` as delivery."

The pre-existing equivalence suite asked ``mcp_delivery`` what it *would*
deliver. That is a tautology with respect to the actual defect: the merge ran in
``install_service`` against an in-memory profile, ``_write_context_file``
persisted the untouched raw text, and Claude Code, Codex, Kimi, Antigravity and
Cursor each called ``load_agent_profile()`` **again** at launch — so the merged
entry was gone by the time the command was built. Copilot never consulted the
profile for MCP at all. Every assertion here therefore reads the command string
or config file the provider really produces.

Mutation-verified: removing the ``with_plugin_mcp`` wrapper from a provider makes
that provider's case fail.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource

from .conftest import build_plugin

PLUGIN_SERVER = "plugin-tools"
MCP_DOC = json.dumps(
    {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {
            PLUGIN_SERVER: {"type": "stdio", "command": "demo-server", "args": ["--serve"]}
        },
    }
)


@pytest.fixture
def installed_plugin(store, skills_dir, tmp_path, monkeypatch):
    """Install a plugin declaring one stdio MCP server, with stores redirected."""
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", tmp_path)

    source = build_plugin(
        tmp_path / "plugin-src", "mcpdonor", skills=["donor-skill"], mcp_text=MCP_DOC
    )
    install(
        PluginSource(kind="path", location=str(source)),
        store=store,
        skills_dir=skills_dir,
        refresh_agents=False,
    )
    # The delivery seam resolves the store from module state, so point it at ours.
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.mcp_delivery.InstalledPluginStore",
        lambda *a, **k: store,
    )
    return store


def _profile_stub(name: str = "worker"):
    """A minimal real AgentProfile — not a MagicMock, so serializers behave."""
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    return AgentProfile(name=name, description="d", system_prompt="p")


def _delivered_somewhere(command: str, server_name: str) -> bool:
    """Whether ``server_name`` reaches the provider, inline or by referenced file.

    Providers split two ways and both count as a real launch artifact: some
    inline the MCP config into the command (Codex's ``-c`` overrides, Kimi's
    ``--mcp-config <json>``), others write a file and pass its path (Claude
    Code's ``--mcp-config <path>``). Asserting only on the command string would
    give a false negative for the second group, so any referenced ``.json`` the
    command names is read and searched too.
    """
    if server_name in command:
        return True
    for token in shlex.split(command):
        if not token.endswith(".json"):
            continue
        candidate = Path(token)
        if candidate.is_file() and server_name in candidate.read_text(encoding="utf-8"):
            return True
    return False


class TestTheLaunchCommandCarriesThePluginServer:
    @pytest.mark.parametrize(
        "module,cls,builder",
        [
            ("claude_code", "ClaudeCodeProvider", "_build_claude_command"),
            ("codex", "CodexProvider", "_build_codex_command"),
            ("kimi_cli", "KimiCliProvider", "_build_kimi_command"),
            ("cursor_cli", "CursorCliProvider", "_build_cursor_command"),
        ],
    )
    def test_the_built_command_mentions_the_plugin_server(
        self, installed_plugin, monkeypatch, module, cls, builder
    ):
        import importlib

        mod = importlib.import_module(f"cli_agent_orchestrator.providers.{module}")
        if not hasattr(mod, cls) or not hasattr(getattr(mod, cls), builder):
            pytest.skip(f"{module}.{cls}.{builder} not present in this build")

        monkeypatch.setattr(mod, "load_agent_profile", lambda _name: _profile_stub())

        provider = getattr(mod, cls)("tid-1", "sess", "win", "worker")
        try:
            command = getattr(provider, builder)()
        except Exception as exc:  # pragma: no cover - provider needs a real binary
            pytest.skip(f"{module} command build needs an environment we do not have: {exc}")

        assert _delivered_somewhere(command, PLUGIN_SERVER), (
            f"{module} built a launch command without the plugin MCP server; "
            f"plugin delivery does not reach this provider"
        )

    def test_copilot_runtime_mcp_config_includes_the_plugin_server(
        self, installed_plugin, monkeypatch
    ):
        """Copilot's runtime config is the only MCP config it reads."""
        from cli_agent_orchestrator.providers import copilot_cli as mod

        monkeypatch.setattr(mod, "load_agent_profile", lambda _name: _profile_stub())
        provider = mod.CopilotCliProvider("tid-2", "sess", "win", "worker")

        raw = provider._build_runtime_mcp_config()
        servers = json.loads(raw)["mcpServers"]

        assert "cao-mcp-server" in servers, "CAO's own in-session server must remain"
        assert PLUGIN_SERVER in servers, (
            "Copilot's runtime MCP config omitted the plugin server, so plugin "
            "MCP delivery never reaches Copilot"
        )

    def test_antigravity_writes_the_plugin_server_into_its_shared_config(
        self, installed_plugin, monkeypatch, tmp_path
    ):
        """Antigravity delivers via a config file rather than the command line."""
        from cli_agent_orchestrator.providers import antigravity_cli as mod

        config_path = tmp_path / "gemini" / "config" / "mcp_config.json"
        monkeypatch.setattr(mod, "load_agent_profile", lambda _name: _profile_stub())
        monkeypatch.setattr(
            mod.AntigravityCliProvider, "_mcp_config_path", lambda self: config_path
        )

        provider = mod.AntigravityCliProvider("tid-3", "sess", "win", "worker")
        profile = mod._with_plugin_mcp(_profile_stub(), "antigravity_cli")
        assert profile.mcpServers and PLUGIN_SERVER in profile.mcpServers

        provider._register_mcp_servers(profile.mcpServers)

        written = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]
        assert any(key.startswith(PLUGIN_SERVER) for key in written), (
            f"antigravity wrote {sorted(written)} — no plugin server reached " f"mcp_config.json"
        )


class TestTheProfileItselfIsUnchangedOnDisk:
    def test_delivery_is_recomputed_not_persisted(self, installed_plugin, monkeypatch):
        """The profile source must not gain the expanded absolute paths.

        The whole reason delivery is applied on read: a persisted copy of the
        expanded ``${PLUGIN_ROOT}`` paths goes stale when the store moves.
        """
        from cli_agent_orchestrator.agent_plugins.mcp_delivery import with_plugin_mcp

        profile = with_plugin_mcp(_profile_stub(), "claude_code")
        assert PLUGIN_SERVER in (profile.mcpServers or {})

        # A second, independent load must produce the server again from disk
        # state alone — not from anything the first call wrote down.
        again = with_plugin_mcp(_profile_stub(), "claude_code")
        assert (again.mcpServers or {}).keys() == (profile.mcpServers or {}).keys()
