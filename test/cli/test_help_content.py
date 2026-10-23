"""Help text hygiene for AI agents reading `cao --help`."""

import re

import click
from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli

# Help is read by agents without the repo, so repo doc paths are rejected on
# purpose; point to `cao <cmd> --help` or `cao --skill` instead.
NOISE = re.compile(r"#\d{2,}\b|\b(?:N?FR|VR|SR)-\d|``|docs/[\w./-]+\.md|maintainer decision")


def _walk(group, path):
    for name in group.list_commands(None):
        cmd = group.get_command(None, name)
        if cmd is None or cmd.hidden or name == "plugin":
            continue
        yield path + [name], cmd
        if isinstance(cmd, click.Group):
            yield from _walk(cmd, path + [name])


def _help(path):
    return CliRunner().invoke(cli, path + ["--help"]).output


def test_no_internal_references_in_visible_help():
    offenders = [" ".join(p) for p, _ in _walk(cli, []) if NOISE.search(_help(p))]
    assert offenders == []


def test_short_help_is_not_truncated():
    groups = [[]] + [p for p, cmd in _walk(cli, []) if isinstance(cmd, click.Group)]
    for p in groups:
        listing = _help(p).split("Commands:")[-1]
        assert "..." not in listing, " ".join(p) or "cao"


def test_destructive_commands_warn():
    for p in (
        ["shutdown"],
        ["agent", "cancel"],
        ["worker", "release"],
        ["schedule", "remove"],
        ["skills", "remove"],
        ["memory", "compact"],
        ["memory", "import"],
        ["update"],
    ):
        assert "no confirmation prompt" in _help(p), " ".join(p)


def test_key_groups_have_examples():
    for p in (["agent"], ["launch"], ["session"], ["terminal"], ["profile"], ["install"]):
        assert "Examples:" in _help(p), " ".join(p)
