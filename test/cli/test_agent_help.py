"""`cao --help` and `cao --skill` must be self-contained.

An agent that has never seen CAO runs these first, often on a machine where
CAO is not set up. They must not import application code, create files, or
need cao-server.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import click
from click.testing import CliRunner

from cli_agent_orchestrator.cli import main as cli_main
from cli_agent_orchestrator.cli.agent_guide import AGENT_GUIDE, FOOTER
from cli_agent_orchestrator.cli.main import cli

_PROBE = """
import json, sys
from cli_agent_orchestrator.cli.main import cli
try:
    cli.main(sys.argv[1:], prog_name="cao", standalone_mode=True)
except SystemExit:
    pass
heavy = sorted(
    m for m in sys.modules
    if m.startswith((
        "cli_agent_orchestrator.api",
        "cli_agent_orchestrator.mcp_server",
        "cli_agent_orchestrator.services",
        "cli_agent_orchestrator.clients",
        "cli_agent_orchestrator.constants",
        "fastapi",
        "sqlalchemy",
        "libtmux",
    ))
)
print("\\nHEAVY=" + json.dumps(heavy))
"""


def _probe(args: list[str], home: Path) -> tuple[subprocess.CompletedProcess, list[str]]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CAO_")}
    env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines = proc.stdout.strip().splitlines()
    assert lines and lines[-1].startswith("HEAVY="), proc.stdout + proc.stderr
    return proc, json.loads(lines[-1][len("HEAVY=") :])


class TestRootHelpIsSelfContained:
    def test_root_help_imports_no_app_modules_and_creates_no_files(self, tmp_path):
        proc, heavy = _probe(["--help"], tmp_path)
        assert "Commands:" in proc.stdout
        assert heavy == []
        assert list(tmp_path.rglob("*")) == []

    def test_schedule_subcommand_help_creates_no_database(self, tmp_path):
        proc, _ = _probe(["schedule", "list", "--help"], tmp_path)
        assert "List all flows" in proc.stdout
        assert list(tmp_path.rglob("*.db")) == []


class TestFooter:
    def test_root_help_ends_with_footer(self):
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert result.output.rstrip().endswith("Otherwise run: cao --skill")

    def test_nested_group_help_has_footer(self):
        result = CliRunner().invoke(cli, ["memory", "relationships", "--help"])
        assert result.exit_code == 0
        assert "cao --skill" in result.output

    def test_footer_is_not_duplicated_on_repeat_lookup(self):
        cli.get_command(None, "agent")
        group = cli.get_command(None, "agent")
        assert isinstance(group, click.Group)
        assert group.epilog is not None
        assert group.epilog.count("cao --skill") == 1

    def test_footer_is_appended_to_an_existing_epilog(self):
        group = click.Group("demo", epilog="Existing epilog.")
        group.add_command(click.Group("nested"))
        cli_main._add_footer(group)
        cli_main._add_footer(group)
        assert group.epilog == f"Existing epilog.\n\n{FOOTER}"
        assert group.commands["nested"].epilog == FOOTER


class TestCommandTable:
    def test_table_matches_real_commands(self):
        for name, module, attr, short_help, hidden in cli_main._COMMANDS:
            cmd = cli.get_command(None, name)
            assert cmd is not None, name
            assert cmd.name == name, name
            assert cmd.get_short_help_str(limit=1000) == short_help, name
            assert cmd.hidden == hidden, name

    def test_root_help_lists_every_visible_command(self):
        output = CliRunner().invoke(cli, ["--help"]).output
        for name, _, _, _, hidden in cli_main._COMMANDS:
            assert (f"  {name} " in output) != hidden, name

    def test_unknown_command_still_fails(self):
        assert CliRunner().invoke(cli, ["unknown-command"]).exit_code != 0


# `cao <word> [<word>]` at the start of a line (code blocks) or right after a
# backtick (inline code). Prose never starts a line with "cao ".
_GUIDE_COMMAND = re.compile(r"(?:^|`)cao ([a-z][a-z-]*(?: [a-z][a-z-]*)?)", re.M)


class TestSkillFlag:
    def test_skill_prints_the_guide(self):
        result = CliRunner().invoke(cli, ["--skill"])
        assert result.exit_code == 0
        assert result.output == AGENT_GUIDE

    def test_skill_imports_no_app_modules_and_creates_no_files(self, tmp_path):
        proc, heavy = _probe(["--skill"], tmp_path)
        assert proc.stdout.startswith("---\nname: cao\n")
        assert heavy == []
        assert list(tmp_path.rglob("*")) == []

    def test_skill_wins_over_a_subcommand(self):
        result = CliRunner().invoke(cli, ["--skill", "schedule", "list"])
        assert result.exit_code == 0
        assert result.output == AGENT_GUIDE


class TestGuideContent:
    def test_frontmatter(self):
        head = AGENT_GUIDE.split("---\n")[1]
        assert "name: cao\n" in head
        assert "description: " in head

    def test_size_budget(self):
        # About 2.5k tokens at 4 characters per token.
        assert len(AGENT_GUIDE) <= 10_000

    def test_every_named_command_exists(self):
        named = {m.group(1) for m in _GUIDE_COMMAND.finditer(AGENT_GUIDE)}
        assert named, "drift guard matched nothing; check the regex"
        for path in sorted(named):
            words = path.split()
            cmd = cli.get_command(None, words[0])
            assert cmd is not None, f"cao {path}"
            if isinstance(cmd, click.Group) and len(words) > 1:
                assert cmd.get_command(None, words[1]) is not None, f"cao {path}"

    def test_lists_every_terminal_status(self):
        for status in (
            "unknown",
            "idle",
            "processing",
            "completed",
            "waiting_user_answer",
            "error",
        ):
            assert f"`{status}`" in AGENT_GUIDE, status

    def test_every_named_flag_exists(self):
        spans = re.findall(r"`(cao [^`]+)`", AGENT_GUIDE) + re.findall(
            r"^(cao .+)$", AGENT_GUIDE, re.M
        )
        all_opts: set[str] = {"--help"}
        for span in spans:
            words = span.split()[1:]
            cmd: click.Command | None = cli
            for word in words:
                if not isinstance(cmd, click.Group) or not re.fullmatch(r"[a-z][a-z-]*", word):
                    break
                nxt = cmd.get_command(None, word)
                if nxt is None:
                    break
                cmd = nxt
            assert cmd is not None, span
            opts = {o for p in cmd.params for o in p.opts}
            all_opts |= opts
            for flag in re.findall(r"--[a-z][a-z-]*", span):
                assert flag in opts | {"--help"}, f"{flag} in `{span}`"
        for flag in set(re.findall(r"--[a-z][a-z-]*", AGENT_GUIDE)):
            assert flag in all_opts, f"{flag} is not an option of any command the guide names"
