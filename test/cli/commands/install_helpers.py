"""Plain helpers for driving ``cao install`` against the ``workspace`` fixture."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.install import install


def _write_profile(path: Path, *, name: str, body: str = "You are a helpful agent.") -> None:
    path.write_text(f"---\nname: {name}\ndescription: Test agent\n---\n{body}\n", encoding="utf-8")


def _install(runner: CliRunner, stem: str):
    return runner.invoke(install, [stem, "--provider", "opencode_cli"])


def _install_for(runner: CliRunner, stem: str, provider: str):
    return runner.invoke(install, [stem, "--provider", provider])


def _ok(result) -> None:
    assert result.exit_code == 0 and "Error:" not in result.output, result.output


def _refused(result) -> None:
    # A refused install exits 1 through click (#1270); an exception other than
    # click's SystemExit would be a crash, not a refusal.
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert "Error:" in result.output, result.output
