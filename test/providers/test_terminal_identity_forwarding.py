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
    CAO_MCP_SERVER_MODULE,
    CAO_TERMINAL_ID_ENV,
    forward_terminal_identity,
    is_bundled_cao_server,
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

    def test_an_empty_terminal_id_leaves_a_declared_value_alone(self):
        """Nothing truer to replace it with; writing "" would strip the identity."""
        entry = forward_terminal_identity(
            {"command": "x", "env": {CAO_TERMINAL_ID_ENV: "deadbeef"}}, "", server_name="s"
        )
        assert entry["env"][CAO_TERMINAL_ID_ENV] == "deadbeef"

    def test_an_empty_terminal_id_injects_nothing(self):
        entry = forward_terminal_identity({"command": "x"}, "", server_name="s")
        assert "env" not in entry


class TestTheInheritedMode:
    """Kimi Code's children inherit the parent env; a DECLARED value still loses.

    The inheritance argument (``kimi_runtime_home.merge_mcp_servers``) justifies
    not *injecting* the variable, and that part is kept -- an entry with no
    ``env`` is written exactly as the author wrote it. It never justified
    honouring a value the profile DECLARED, which is what that server is launched
    with for this key and is what the MCP gates authorize on.
    """

    def test_an_absent_value_is_left_absent(self):
        entry = forward_terminal_identity(
            {"command": "x"}, "a1b2c3d4", server_name="s", inherited=True
        )
        assert "env" not in entry

    def test_an_unrelated_env_block_is_not_given_an_identity(self):
        entry = forward_terminal_identity(
            {"command": "x", "env": {"KEEP": "yes"}},
            "a1b2c3d4",
            server_name="s",
            inherited=True,
        )
        assert entry["env"] == {"KEEP": "yes"}

    @pytest.mark.parametrize("pinned", PINNED)
    def test_a_declared_value_is_still_overridden_and_logged(self, pinned, caplog):
        with caplog.at_level("WARNING"):
            entry = forward_terminal_identity(
                {"command": "x", "env": {CAO_TERMINAL_ID_ENV: pinned}},
                "a1b2c3d4",
                server_name="ops",
                inherited=True,
            )
        assert entry["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"
        assert "ops" in caplog.text


class TestTheShapeMatcher:
    """``is_bundled_cao_server`` keys on the command, never on the entry's name.

    The name in ``mcpServers`` is the profile author's to choose, so a check that
    keys on it protects nothing: review of #1062 found exactly that in the Codex
    and Copilot writers.
    """

    @pytest.mark.parametrize(
        "config",
        [
            {"command": "cao-mcp-server"},
            {"command": "/opt/venv/bin/cao-mcp-server"},
            {"command": r"C:\tools\venv\Scripts\cao-mcp-server.exe"},
            {"command": "/usr/bin/python3", "args": ["-m", CAO_MCP_SERVER_MODULE]},
            {"command": "uvx", "args": ["--from", "cli-agent-orchestrator", "cao-mcp-server"]},
            {"command": ["/opt/venv/bin/python", "-m", CAO_MCP_SERVER_MODULE]},
        ],
    )
    def test_every_form_of_our_own_server_is_recognised(self, config):
        assert is_bundled_cao_server(config) is True

    @pytest.mark.parametrize(
        "config",
        [
            {},
            {"command": "npx", "args": ["-y", "some-other-server"]},
            {"type": "http", "url": "https://example.test/mcp"},
            {"command": "python3", "args": ["-m", "someone.else.server"]},
            {"command": "cao-mcp-server-ish"},
            {"command": "python3", "args": ["-m"]},
        ],
    )
    def test_someone_else_s_server_is_not(self, config):
        assert is_bundled_cao_server(config) is False

    def test_the_name_in_mcp_servers_is_irrelevant(self):
        """Both directions: an innocent name on our server, our name on theirs."""
        assert is_bundled_cao_server({"command": "cao-mcp-server"}) is True
        assert is_bundled_cao_server({"command": "npx", "args": ["evil"]}) is False


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

    def test_copilot_cli(self, pinned):
        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider

        servers = {"my-server": {"command": "my-server", "env": {CAO_TERMINAL_ID_ENV: pinned}}}
        with patch(
            "cli_agent_orchestrator.providers.copilot_cli.load_agent_profile",
            return_value=_profile(servers),
        ):
            provider = CopilotCliProvider("a1b2c3d4", "s", "w", agent_profile="worker")
            config = json.loads(provider._build_runtime_mcp_config())
        assert config["mcpServers"]["my-server"]["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"
        # CAO's own built-in entry is built through the same helper.
        assert config["mcpServers"]["cao-mcp-server"]["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"

    def test_codex(self, pinned):
        """The EXPLICIT ``-c ...env=`` table, not just the inheritance request.

        Codex emitted both: ``env_vars=["CAO_TERMINAL_ID"]`` (inherit from the
        parent) *and* the profile's own ``env`` table. The explicit table is what
        Codex passes to the server, so inheritance alone did not save it -- the
        reviewer's probe showed ``explicit='deadbeef' inherits_real=True``.
        """
        from cli_agent_orchestrator.providers.codex import CodexProvider
        from cli_agent_orchestrator.utils.agent_profiles import ProfileSource

        servers = {"srv": {"command": "cao-mcp-server", "env": {CAO_TERMINAL_ID_ENV: pinned}}}
        with patch(
            "cli_agent_orchestrator.providers.codex.agent_profiles.load_launch_profile",
            return_value=(_profile(servers), ProfileSource.EPHEMERAL),
        ):
            provider = CodexProvider("a1b2c3d4", "s", "w", agent_profile="worker")
            command = provider._build_codex_command()
        table = _codex_env_table(command, "srv")
        assert table[CAO_TERMINAL_ID_ENV] == "a1b2c3d4"
        assert pinned not in command or pinned == ""
        # The inheritance request stays: it is harmless and covers a server the
        # explicit table somehow misses.
        assert 'mcp_servers.srv.env_vars=["CAO_TERMINAL_ID"]' in command

    def test_grok_cli(self, pinned):
        from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider

        provider = GrokCliProvider("a1b2c3d4", "s", "w")
        rendered = provider._render_mcp_config(
            {"my-server": {"command": "my-server", "env": {CAO_TERMINAL_ID_ENV: pinned}}}
        )
        assert f'"{CAO_TERMINAL_ID_ENV}" = "a1b2c3d4"' in rendered
        assert pinned not in rendered or pinned.strip() == ""

    def test_minimax_code(self, pinned, tmp_path):
        from cli_agent_orchestrator.providers.minimax_code import MiniMaxCodeProvider

        provider = MiniMaxCodeProvider(
            terminal_id="a1b2c3d4", session_name="s", window_name="w", agent_profile="worker"
        )
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        provider._write_plugin(
            data_dir,
            {"my-server": {"command": "my-server", "env": {CAO_TERMINAL_ID_ENV: pinned}}},
        )
        written = json.loads(
            (data_dir / "plugins" / "cao-orchestrator" / "servers.mcp.json").read_text(
                encoding="utf-8"
            )
        )
        entry = written["mcpServers"]["my-server"]
        assert entry["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"

    def test_kimi_code_runtime_home(self, pinned, tmp_path):
        """The REAL runtime ``mcp.json``, which is the file Kimi Code reads.

        ``merge_mcp_servers`` documented "an author who pins one means it" and
        wrote the pinned value through verbatim; the legacy ``kimi_cli`` writer
        was converted in round 2 but this dialect was not.
        """
        from cli_agent_orchestrator.providers.kimi_runtime_home import (
            KimiCodeRuntimeHomeBuilder,
        )

        source = tmp_path / "source"
        source.mkdir()
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "rt", "a1b2c3d4").build(
            {"my-server": {"command": "cao-mcp-server", "env": {CAO_TERMINAL_ID_ENV: pinned}}}
        )
        written = json.loads((result.home / "mcp.json").read_text(encoding="utf-8"))
        assert written["mcpServers"]["my-server"]["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"

    def test_kimi_code_runtime_home_user_level_entry(self, pinned, tmp_path):
        """A user-level ``mcp.json`` is the same bypass from a different file."""
        from cli_agent_orchestrator.providers.kimi_runtime_home import (
            KimiCodeRuntimeHomeBuilder,
        )

        source = tmp_path / "source"
        source.mkdir()
        (source / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "theirs": {
                            "command": "cao-mcp-server",
                            "env": {CAO_TERMINAL_ID_ENV: pinned},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "rt", "a1b2c3d4").build(None)
        written = json.loads((result.home / "mcp.json").read_text(encoding="utf-8"))
        assert written["mcpServers"]["theirs"]["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"


def _codex_env_table(command: str, server: str) -> dict:
    """Parse one ``-c mcp_servers.<server>.env={...}`` inline table back out."""
    try:  # Python 3.11+
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 -- tomli is a declared dependency there
        import tomli as tomllib  # type: ignore[no-redef]

    match = re.search(rf"mcp_servers\.{re.escape(server)}\.env=(\{{.*?\}})'", command)
    assert match, f"no env inline table for {server!r} in: {command}"
    return tomllib.loads(f"x = {match.group(1)}")["x"]


class TestADifferentlyNamedEntryThatLaunchesOurServer:
    """The protected NAME was never the control; the command's shape is.

    Both writers below guarded the entry literally called ``cao-mcp-server`` and
    let every other name through the unprotected branch. An entry named anything
    at all whose command IS ``cao-mcp-server`` starts CAO's own MCP server, and
    the gates authorize on whatever identity reaches it.
    """

    def test_copilot_cli(self):
        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider

        servers = {
            "sneaky": {"command": "cao-mcp-server", "env": {CAO_TERMINAL_ID_ENV: "deadbeef"}}
        }
        with patch(
            "cli_agent_orchestrator.providers.copilot_cli.load_agent_profile",
            return_value=_profile(servers),
        ):
            provider = CopilotCliProvider("a1b2c3d4", "s", "w", agent_profile="worker")
            config = json.loads(provider._build_runtime_mcp_config())
        assert config["mcpServers"]["sneaky"]["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"
        assert "deadbeef" not in json.dumps(config)

    def test_codex_still_keeps_our_tools_undeferred_for_it(self):
        """``is_cao_server`` drives ``omit_tools_from`` too, so the shape check
        has to recognise the entry under its alias for that to keep working."""
        from cli_agent_orchestrator.providers.codex import CodexProvider
        from cli_agent_orchestrator.utils.agent_profiles import ProfileSource

        servers = {
            "sneaky": {"command": "cao-mcp-server", "env": {CAO_TERMINAL_ID_ENV: "deadbeef"}}
        }
        with patch(
            "cli_agent_orchestrator.providers.codex.agent_profiles.load_launch_profile",
            return_value=(_profile(servers), ProfileSource.EPHEMERAL),
        ):
            provider = CodexProvider("a1b2c3d4", "s", "w", agent_profile="worker")
            command = provider._build_codex_command()
        assert _codex_env_table(command, "sneaky")[CAO_TERMINAL_ID_ENV] == "a1b2c3d4"
        assert "deadbeef" not in command
        assert 'mcp_servers.sneaky.omit_tools_from=["deferred"]' in command


class TestTheKimiCodeProviderHandsTheBuilderItsRealId:
    """The builder cannot forward an identity it was never given.

    ``KimiCodeRuntimeHomeBuilder`` takes ``terminal_id`` so it can override a
    declared value; an empty one is a no-op by design (there is nothing truer to
    write). This asks the real launch path, so a future refactor that drops the
    argument fails here rather than silently restoring the bypass.
    """

    def test_the_real_launch_path_overrides_a_pinned_id(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.providers import kimi_cli as kimi_cli_module
        from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", tmp_path / "cao")
        source = tmp_path / "kimi-source"
        source.mkdir()

        servers = {
            "sneaky": {"command": "cao-mcp-server", "env": {CAO_TERMINAL_ID_ENV: "deadbeef"}}
        }
        provider = KimiCliProvider("a1b2c3d4", "s", "w", agent_profile="worker")
        provider._kimi_binary = "/usr/bin/kimi"
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        provider._kimi_source_home = source
        with patch(
            "cli_agent_orchestrator.providers.kimi_cli.load_agent_profile",
            return_value=_profile(servers),
        ):
            provider._build_kimi_code_command()

        home = provider._runtime_home_builder.home
        written = json.loads((home / "mcp.json").read_text(encoding="utf-8"))
        assert written["mcpServers"]["sneaky"]["env"][CAO_TERMINAL_ID_ENV] == "a1b2c3d4"
