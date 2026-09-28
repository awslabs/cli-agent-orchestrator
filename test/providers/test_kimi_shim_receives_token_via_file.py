"""Kimi delivers the runtime token to the shim via a file, never in argv (#802).

Copilot (4116854565, 4113446177) reads Kimi's config as removing
CAO_RUNTIME_TOKEN without giving the shim any way to obtain it. In fact
``resolve_mcp_server_config(omit_token=True)`` writes CAO_RUNTIME_TOKEN_FILE
(an owner-only file) into the entry's env, and the shim reads the token from
that file. This drives the real command in shared-endpoint mode and asserts
the observable delivery: the entry names a readable file, the token value is
absent from argv, and the shim's header builder produces the runtime-token
header equal to the token when run under only that entry env.
"""

import json
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.mcp_server.http_hosting import RUNTIME_TOKEN_HEADER
from cli_agent_orchestrator.mcp_server.stdio_bridge import build_forward_headers
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

_TOKEN = "s3cret-channel-token-value"


@patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
def test_kimi_shim_receives_token_via_file(mock_load, monkeypatch):
    monkeypatch.setenv("CAO_MCP_HTTP_URL", "http://shared.mcp.svc:9889/mcp")
    monkeypatch.setenv("CAO_RUNTIME_TOKEN", _TOKEN)
    profile = MagicMock()
    profile.model = None
    profile.system_prompt = None
    profile.mcpServers = {
        "cao-mcp-server": {"type": "stdio", "command": "cao-mcp-server", "args": []}
    }
    mock_load.return_value = profile

    provider = KimiCliProvider("abc12345", "session", "window", agent_profile="dev")
    command = provider._build_kimi_command()

    entry_env = json.loads(command.split("--mcp-config ")[1].strip().strip("'"))["cao-mcp-server"][
        "env"
    ]

    # File path delivered, value not; and the value is nowhere in the argv.
    assert "CAO_RUNTIME_TOKEN_FILE" in entry_env
    assert "CAO_RUNTIME_TOKEN" not in entry_env
    assert _TOKEN not in command

    # The shim, run with ONLY the entry env, recovers the token from the file
    # and presents it as the runtime-token header.
    with patch.dict("os.environ", entry_env, clear=True):
        headers = build_forward_headers()
    assert headers[RUNTIME_TOKEN_HEADER] == _TOKEN
