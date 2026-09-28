"""Kimi and MiniMax forward CAO_API_HOST/PORT/MEMORY into the MCP child env (#802).

Only claude_code and codex used to forward the trio a direct (non-shim)
cao-mcp-server child needs to find the CAO API off localhost. On an
execution-only runtime that left Kimi/MiniMax children defaulting to
127.0.0.1:9889. Both providers must forward the trio when it is set.
"""

import json
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
from cli_agent_orchestrator.providers.minimax_code import MiniMaxCodeProvider

_TRIO = {
    "CAO_API_HOST": "10.0.0.7",
    "CAO_API_PORT": "8443",
    "CAO_MEMORY_API_URL": "http://memory.svc:9100",
}


def _set_trio(monkeypatch):
    for key, value in _TRIO.items():
        monkeypatch.setenv(key, value)


def _kimi_mcp_entry(command):
    parts = command.split("--mcp-config ")
    config = json.loads(parts[1].strip().strip("'"))
    return config["cao-mcp-server"]["env"]


@patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
def test_kimi_forwards_api_host_trio(mock_load, monkeypatch):
    _set_trio(monkeypatch)
    profile = MagicMock()
    profile.model = None
    profile.system_prompt = None
    profile.mcpServers = {
        "cao-mcp-server": {"type": "stdio", "command": "cao-mcp-server", "args": []}
    }
    mock_load.return_value = profile

    provider = KimiCliProvider("abc12345", "session", "window", agent_profile="dev")
    env = _kimi_mcp_entry(provider._build_kimi_command())

    for key, value in _TRIO.items():
        assert env.get(key) == value


def test_minimax_forwards_api_host_trio(tmp_path, monkeypatch):
    _set_trio(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("MINIMAX_DATA_DIR", str(source))
    cao_home = tmp_path / "cao"
    profile = AgentProfile(
        name="developer",
        description="Developer",
        mcpServers={
            "cao-orchestrator": {
                "command": "/opt/cao/bin/cao-mcp-server",
                "args": ["--stdio"],
                "env": {"EXISTING": "value"},
            }
        },
    )
    provider = MiniMaxCodeProvider(
        terminal_id="deadbeef", session_name="s", window_name="w", agent_profile="developer"
    )
    with (
        patch("cli_agent_orchestrator.providers.minimax_code.CAO_HOME_DIR", cao_home),
        patch(
            "cli_agent_orchestrator.providers.minimax_code.load_agent_profile",
            return_value=profile,
        ),
    ):
        data_dir, _ = provider._prepare_runtime()

    plugin = data_dir / "plugins" / "cao-orchestrator"
    env = json.loads((plugin / "servers.mcp.json").read_text())["mcpServers"]["cao-orchestrator"][
        "env"
    ]
    for key, value in _TRIO.items():
        assert env.get(key) == value
