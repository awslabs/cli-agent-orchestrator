"""A profile cannot choose the ``CAO_TERMINAL_ID`` its MCP servers are launched with.

``CAO_TERMINAL_ID`` stopped being a convenience for handoff/assign when #671/#972
made it an AUTHORIZATION input: the MCP gates read it to decide whether the caller
is a bound terminal, gated on its recorded ``allowed_tools``, or an operator
context that is not gated at all. Four providers still carried the older rule --
``if "CAO_TERMINAL_ID" not in env`` ("do not override an explicit preset") -- so a
profile could declare the value its own authorization decision would be taken on:

* ``env: {CAO_TERMINAL_ID: ""}`` was an operator context before the companion fix
  in ``security.mcp_tool_gate.caller_is_bound``, reaching every ops tool with no
  row lookup at all;
* a preset naming ANOTHER terminal's id borrows that terminal's allowlist, and
  (#940) attributes the calls to it.

Found by independent review of #1062. The rule now: the launching terminal's own
id always wins, and a profile that tried to pin one is logged. These tests ask it
of each provider's real config writer, not of the helper alone -- a provider that
keeps its own inline injection would pass a helper test and still be bypassable.

The gate's half of this is ``test/security/test_mcp_tool_gate_boundness.py``.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_TERMINAL_ID_ENV,
    forward_terminal_identity,
)

#: Values a profile might declare, and must not get: the empty string that used
#: to buy an operator context, and another terminal's id.
PINNED = ["", "   ", "deadbeef", "someone-elses-id"]


class TestTheSharedHelper:
    def test_an_absent_value_is_injected(self):
        entry = forward_terminal_identity({"command": "x"}, "a1b2c3d4", server_name="s")
        assert entry["env"] == {CAO_TERMINAL_ID_ENV: "a1b2c3d4"}

    def test_other_env_vars_are_preserved(self):
        entry = forward_terminal_identity(
            {"command": "x", "env": {"KEEP": "yes"}}, "a1b2c3d4", server_name="s"
        )
        assert entry["env"] == {"KEEP": "yes", CAO_TERMINAL_ID_ENV: "a1b2c3d4"}

    @pytest.mark.parametrize("pinned", PINNED)
    def test_a_declared_value_is_overridden_and_logged(self, pinned, caplog):
        with caplog.at_level("WARNING"):
            entry = forward_terminal_identity(
                {"command": "x", "env": {CAO_TERMINAL_ID_ENV: pinned}},
                "a1b2c3d4",
                server_name="ops",
            )
        assert entry["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"
        assert "ops" in caplog.text and CAO_TERMINAL_ID_ENV in caplog.text

    def test_declaring_the_real_id_is_not_worth_a_warning(self, caplog):
        with caplog.at_level("WARNING"):
            entry = forward_terminal_identity(
                {"command": "x", "env": {CAO_TERMINAL_ID_ENV: "a1b2c3d4"}},
                "a1b2c3d4",
                server_name="s",
            )
        assert entry["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"
        assert caplog.text == ""

    def test_the_profile_s_own_env_dict_is_not_mutated(self):
        """A profile's nested ``env`` is shared with the loaded model; copy it."""
        original = {"KEEP": "yes"}
        config = {"command": "x", "env": original}
        forward_terminal_identity(config, "a1b2c3d4", server_name="s")
        assert original == {"KEEP": "yes"}
        assert config == {"command": "x", "env": {"KEEP": "yes"}}


def _profile(mcp_servers):
    """A real profile, not a MagicMock: these drive each provider's real writer."""
    return AgentProfile(name="worker", description="test profile", mcpServers=mcp_servers)


@pytest.mark.parametrize("pinned", PINNED)
class TestEachProviderInjectsTheRealId:
    """Asked of each provider's real MCP config writer."""

    def test_claude_code(self, pinned):
        from test.providers.test_claude_code_unit import _extract_mcp_config

        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        servers = {"my-server": {"command": "my-server", "env": {CAO_TERMINAL_ID_ENV: pinned}}}
        with patch(
            "cli_agent_orchestrator.providers.claude_code.agent_profiles.load_agent_profile",
            return_value=_profile(servers),
        ):
            provider = ClaudeCodeProvider("a1b2c3d4", "s", "w", "worker")
            config = _extract_mcp_config(provider._build_claude_command())
        assert config["mcpServers"]["my-server"]["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"

    def test_kimi_cli(self, pinned):
        from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

        servers = {"my-server": {"command": "npx", "env": {CAO_TERMINAL_ID_ENV: pinned}}}
        with patch(
            "cli_agent_orchestrator.providers.kimi_cli.load_agent_profile",
            return_value=_profile(servers),
        ):
            provider = KimiCliProvider("a1b2c3d4", "s", "w", agent_profile="worker")
            command = provider._build_kimi_command()
        config = json.loads(command.split("--mcp-config ")[1].strip().strip("'"))
        assert config["my-server"]["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"

    def test_cursor_cli(self, pinned):
        from cli_agent_orchestrator.providers.cursor_cli import CursorCliProvider

        servers = {"my-server": {"command": "my-server", "env": {CAO_TERMINAL_ID_ENV: pinned}}}
        with (
            patch(
                "cli_agent_orchestrator.providers.cursor_cli.load_agent_profile",
                return_value=_profile(servers),
            ),
            patch(
                "cli_agent_orchestrator.providers.cursor_cli.shutil.which",
                return_value="/usr/local/bin/cursor-agent",
            ),
        ):
            provider = CursorCliProvider("a1b2c3d4", "s", "w", agent_profile="worker")
            command = provider._build_cursor_command()
        match = re.search(r"--plugin-dir\s+(\S+)", command)
        assert match is not None
        manifest = json.loads((Path(match.group(1)) / "plugin.json").read_text(encoding="utf-8"))
        assert manifest["mcpServers"]["my-server"]["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"

    def test_omp(self, pinned, tmp_path, monkeypatch):
        from cli_agent_orchestrator.providers.omp import OmpProvider

        monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
        servers = {"my-server": {"command": "my-server", "env": {CAO_TERMINAL_ID_ENV: pinned}}}
        with (
            patch(
                "cli_agent_orchestrator.providers.omp.load_agent_profile",
                return_value=_profile(servers),
            ),
            patch(
                "cli_agent_orchestrator.providers.omp.shutil.which",
                return_value="/usr/local/bin/omp",
            ),
        ):
            provider = OmpProvider("a1b2c3d4", "s", "w", agent_profile="worker")
            command = provider._build_omp_command()
        extension_dir = Path(command.split("--extension ")[1].split()[0])
        config = json.loads((extension_dir / ".mcp.json").read_text(encoding="utf-8"))
        servers_written = config["mcpServers"]
        assert servers_written["my-server"]["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"
