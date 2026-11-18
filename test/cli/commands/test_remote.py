"""Tests for the experimental Remote CAO CLI."""

import stat

from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.services.remote import registry
from cli_agent_orchestrator.services.remote.checks import Check
from cli_agent_orchestrator.services.remote.registry import RemoteBox


def test_remote_add_list_remove_and_private_atomic_file(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "CAO_HOME_DIR", tmp_path)
    runner = CliRunner()
    added = runner.invoke(
        cli,
        [
            "remote",
            "add",
            "box-1",
            "--instance-id",
            "i-0123456789abcdef",
            "--region",
            "us-east-1",
            "--profile",
            "sso",
        ],
    )
    assert added.exit_code == 0
    path = tmp_path / "remote" / "box-1.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "box-1" in runner.invoke(cli, ["remote", "list"]).output
    assert runner.invoke(cli, ["remote", "remove", "box-1"]).exit_code == 0


def test_remote_add_validates_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "CAO_HOME_DIR", tmp_path)
    result = CliRunner().invoke(
        cli,
        ["remote", "add", "BAD", "--instance-id", "bad", "--region", "nowhere", "--profile", "p"],
    )
    assert result.exit_code == 2
    assert "box must match" in result.output


def test_remote_check_json_and_exit_codes(monkeypatch):
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.remote.load",
        lambda _: RemoteBox("box", "i-0123456789abcdef", "us-east-1", "p"),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.remote.check_box",
        lambda _: [Check("OK", "aws-cli", "2.x"), Check("FIX", "ssh", "install")],
    )
    result = CliRunner().invoke(cli, ["remote", "check", "box", "--json"])
    assert result.exit_code == 1
    assert '"status": "FIX"' in result.output


def test_remote_help_is_registered():
    result = CliRunner().invoke(cli, ["remote", "--help"])
    assert result.exit_code == 0
    assert "check" in result.output
