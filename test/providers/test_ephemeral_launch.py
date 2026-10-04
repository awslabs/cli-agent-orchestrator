"""Launch paths must not turn an unavailable ephemeral into a native agent."""

import json
from test.utils.test_agent_profiles_ephemeral import DOCUMENT, NAME, stores  # noqa: F401
from unittest.mock import AsyncMock, Mock

import pytest

from cli_agent_orchestrator.providers import claude_code, codex
from cli_agent_orchestrator.utils import agent_profiles as profiles


@pytest.mark.parametrize(
    "module, cls", [(claude_code, claude_code.ClaudeCodeProvider), (codex, codex.CodexProvider)]
)
def test_missing_ephemeral_provider_fails_closed(stores, module, cls):
    provider = cls("abcd1234", "session", "window", NAME)
    if module is codex:
        assert provider._try_load_profile() is None
        with pytest.raises(module.ProviderError):
            provider._build_codex_command()
    else:
        with pytest.raises(module.ProviderError):
            provider._load_profile()


@pytest.mark.parametrize("provider", ["claude_code", "copilot_cli"])
@pytest.mark.asyncio
async def test_server_refuses_before_provider_init(stores, monkeypatch, provider):
    from cli_agent_orchestrator.services import terminal_service as service

    backend = Mock()
    backend.session_exists.return_value = True
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr(service, "get_max_terminals", lambda: None)
    init = AsyncMock()
    factory = Mock(return_value=Mock(initialize=init))
    monkeypatch.setattr(service.ProviderManager, "create_provider", factory)
    with pytest.raises(profiles.EphemeralProfileUnavailable):
        await service.create_terminal(provider, NAME, session_name="session")
    factory.assert_not_called()
    init.assert_not_called()
    backend.create_window.assert_not_called()


@pytest.mark.parametrize(
    "module, cls", [(claude_code, claude_code.ClaudeCodeProvider), (codex, codex.CodexProvider)]
)
def test_ephemeral_skips_installed_plugin_mcp(stores, monkeypatch, tmp_path, module, cls):
    from cli_agent_orchestrator.agent_plugins import mcp_delivery

    _, live = stores
    doc = DOCUMENT.replace(
        "provider: claude_code",
        "provider: claude_code\nmcpServers:\n  cao-mcp-server:\n    command: cao-mcp-server\n    args: []",
    )
    (live / f"{NAME}.md").write_text(doc)
    from test.agent_plugins.conftest import build_plugin

    from cli_agent_orchestrator.agent_plugins.installer import install
    from cli_agent_orchestrator.agent_plugins.models import PluginSource
    from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore

    plugin_store = InstalledPluginStore(
        plugins_dir=tmp_path / "plugins", data_dir=tmp_path / "data"
    )
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setenv("CAO_AGENT_PLUGINS_ENABLED", "1")
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", tmp_path)
    source = build_plugin(
        tmp_path / "plugin-src",
        "mcpdonor",
        skills=[],
        mcp_text=json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {"plugin-server": {"type": "stdio", "command": "plugin"}},
            }
        ),
    )
    install(
        PluginSource(kind="path", location=str(source)),
        store=plugin_store,
        skills_dir=skills_dir,
        refresh_agents=False,
    )
    monkeypatch.setattr(mcp_delivery, "InstalledPluginStore", lambda *a, **k: plugin_store)
    installed = profiles.parse_agent_profile_text(doc, "ordinary")
    assert (
        "plugin-server"
        in mcp_delivery.with_plugin_mcp(installed, module.__name__.split(".")[-1]).mcpServers
    )
    merge = Mock(wraps=module._with_plugin_mcp)
    monkeypatch.setattr(module, "_with_plugin_mcp", merge)
    monkeypatch.setattr(module, "CAO_HOME_DIR", tmp_path)
    provider = cls("abcd1234", "session", "window", NAME)
    if module is claude_code:
        command = provider._build_claude_command()
        config = json.loads((tmp_path / "tmp" / "abcd1234.mcp.json").read_text())
        assert set(config["mcpServers"]) == {"cao-mcp-server"}
        assert "--strict-mcp-config" in command
    else:
        assert provider._try_load_profile().name == NAME
        command = provider._build_codex_command()
        assert "plugin-server" not in command
        assert "mcp_servers.cao-mcp-server" in command
    merge.assert_not_called()
