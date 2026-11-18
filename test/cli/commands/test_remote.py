"""Tests for the experimental, read-only Remote CAO CLI."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Sequence

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.services.remote import registry
from cli_agent_orchestrator.services.remote.checks import Check, check_box
from cli_agent_orchestrator.services.remote.registry import (
    RemoteBox,
    RemoteConfigError,
    load,
    remove,
    save,
)
from cli_agent_orchestrator.services.remote.runner import Runner, RunResult


class FakeRunner:
    """Deterministic command seam which records, but never executes, argv."""

    def __init__(self, responses: dict[tuple[str, ...], RunResult] | None = None) -> None:
        self.responses = responses or {}
        self.argv: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], timeout: float = 15.0) -> RunResult:
        del timeout
        command = tuple(argv)
        self.argv.append(command)
        return self.responses.get(command, RunResult(0))


@pytest.fixture
def box() -> RemoteBox:
    return RemoteBox("box-1", "i-0123456789abcdef", "us-east-1", "work")


def command_responses(box: RemoteBox) -> dict[tuple[str, ...], RunResult]:
    """Responses that satisfy every check, including SSH."""
    host = f"cao-{box.name}"
    return {
        ("aws", "--version"): RunResult(0, "aws-cli/2.17.1 Python/3"),
        ("session-manager-plugin", "--version"): RunResult(0, "1.2.0"),
        ("ssh", "-V"): RunResult(0, "", "OpenSSH_9.8"),
        ("herdr", "--version"): RunResult(0, "herdr 0.9.3"),
        ("aws", "configure", "list-profiles"): RunResult(0, "work\n"),
        ("aws", "sts", "get-caller-identity", "--profile", "work"): RunResult(0, "{}"),
        (
            "aws",
            "ec2",
            "describe-instances",
            "--profile",
            "work",
            "--region",
            "us-east-1",
            "--instance-ids",
            "i-0123456789abcdef",
            "--query",
            "Reservations[].Instances[].State.Name",
            "--output",
            "text",
        ): RunResult(0, "running\n"),
        (
            "aws",
            "ssm",
            "describe-instance-information",
            "--profile",
            "work",
            "--region",
            "us-east-1",
            "--filters",
            "Key=InstanceIds,Values=i-0123456789abcdef",
            "--query",
            "InstanceInformationList[].PingStatus",
            "--output",
            "text",
        ): RunResult(0, "Online\n"),
        ("ssh", "-o", "BatchMode=yes", host, "true"): RunResult(0),
        ("ssh", "-o", "BatchMode=yes", host, "herdr", "--version"): RunResult(0, "herdr 0.9.3"),
        (
            "ssh",
            "-o",
            "BatchMode=yes",
            host,
            "curl",
            "-fsS",
            "http://127.0.0.1:9889/health",
        ): RunResult(0, '{"status":"ok"}'),
    }


def ready_home(tmp_path: Path, box: RemoteBox) -> Path:
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / f"cao-{box.name}_ed25519").touch()
    (ssh / "config").write_text(f"Host cao-{box.name}\n", encoding="utf-8")
    return tmp_path


def checks_by_name(checks: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


def test_every_check_ok_path(box, tmp_path):
    checks = check_box(box, FakeRunner(command_responses(box)), ready_home(tmp_path, box))
    assert {check.status for check in checks} == {"OK"}
    assert [check.name for check in checks] == [
        "aws-cli",
        "session-manager-plugin",
        "ssh",
        "herdr",
        "aws-profile",
        "aws-credentials",
        "aws-login",
        "instance",
        "ssm",
        "ssh-key",
        "ssh-config",
        "ssh",
        "herdr-version",
        "cao-server /health",
    ]


@pytest.mark.parametrize(
    ("command", "expected_name"),
    [
        (("aws", "--version"), "aws-cli"),
        (("session-manager-plugin", "--version"), "session-manager-plugin"),
        (("ssh", "-V"), "ssh"),
        (("herdr", "--version"), "herdr"),
        (("aws", "configure", "list-profiles"), "aws-profile"),
        (("aws", "sts", "get-caller-identity", "--profile", "work"), "aws-login"),
        (
            (
                "aws",
                "ec2",
                "describe-instances",
                "--profile",
                "work",
                "--region",
                "us-east-1",
                "--instance-ids",
                "i-0123456789abcdef",
                "--query",
                "Reservations[].Instances[].State.Name",
                "--output",
                "text",
            ),
            "instance",
        ),
        (
            (
                "aws",
                "ssm",
                "describe-instance-information",
                "--profile",
                "work",
                "--region",
                "us-east-1",
                "--filters",
                "Key=InstanceIds,Values=i-0123456789abcdef",
                "--query",
                "InstanceInformationList[].PingStatus",
                "--output",
                "text",
            ),
            "ssm",
        ),
    ],
)
def test_laptop_and_aws_fix_paths_skip_ssh(box, tmp_path, command, expected_name):
    responses = command_responses(box)
    responses[command] = RunResult(1, stderr="missing")
    results = check_box(box, FakeRunner(responses), ready_home(tmp_path, box))
    checks = checks_by_name(results)
    assert any(check.name == expected_name and check.status == "FIX" for check in results)
    assert checks["cao-server /health"].status == "SKIP"


@pytest.mark.parametrize("missing", ["key", "config"])
def test_local_ssh_fix_paths_skip_ssh_stage(box, tmp_path, missing):
    home = ready_home(tmp_path, box)
    if missing == "key":
        (home / ".ssh" / f"cao-{box.name}_ed25519").unlink()
        expected = "ssh-key"
    else:
        (home / ".ssh" / "config").unlink()
        expected = "ssh-config"
    runner = FakeRunner(command_responses(box))
    checks = checks_by_name(check_box(box, runner, home))
    assert checks[expected].status == "FIX"
    assert checks["cao-server /health"].status == "SKIP"
    assert all("BatchMode=yes" not in argv for argv in runner.argv)


def test_long_lived_key_is_fix_and_canary_never_reaches_text_or_json(box, tmp_path, monkeypatch):
    home = ready_home(tmp_path, box)
    aws = home / ".aws"
    aws.mkdir()
    canary = "CANARY-AWS-SECRET-MUST-NOT-LEAK"
    (aws / "credentials").write_text(f"[work]\naws_access_key_id = {canary}\n", encoding="utf-8")
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.remote.load", lambda _: box)
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.remote.check_box",
        lambda _: check_box(box, FakeRunner(command_responses(box)), home),
    )
    runner = CliRunner()
    text = runner.invoke(cli, ["remote", "check", box.name])
    as_json = runner.invoke(cli, ["remote", "check", box.name, "--json"])
    assert (
        checks_by_name(check_box(box, FakeRunner(command_responses(box)), home))[
            "aws-credentials"
        ].status
        == "FIX"
    )
    assert canary not in text.output
    assert canary not in as_json.output


@pytest.mark.parametrize(
    ("remote_command", "expected_name"),
    [
        (("ssh", "-o", "BatchMode=yes", "cao-box-1", "true"), "ssh"),
        (
            ("ssh", "-o", "BatchMode=yes", "cao-box-1", "herdr", "--version"),
            "herdr-version",
        ),
        (
            (
                "ssh",
                "-o",
                "BatchMode=yes",
                "cao-box-1",
                "curl",
                "-fsS",
                "http://127.0.0.1:9889/health",
            ),
            "cao-server /health",
        ),
    ],
)
def test_remote_fix_paths(box, tmp_path, remote_command, expected_name):
    responses = command_responses(box)
    responses[remote_command] = RunResult(1, stderr="unavailable")
    checks = checks_by_name(check_box(box, FakeRunner(responses), ready_home(tmp_path, box)))
    assert checks[expected_name].status == "FIX"
    if expected_name == "ssh":
        assert checks["cao-server /health"].status == "SKIP"


def test_ssh_stage_skips_when_any_earlier_check_fails(box, tmp_path):
    responses = command_responses(box)
    responses[("aws", "--version")] = RunResult(1)
    runner = FakeRunner(responses)
    checks = checks_by_name(check_box(box, runner, ready_home(tmp_path, box)))
    assert checks["ssh"].status == "SKIP"
    assert checks["cao-server /health"].status == "SKIP"
    assert all("BatchMode=yes" not in argv for argv in runner.argv)


def test_full_check_uses_only_read_only_allowlisted_argv(box, tmp_path):
    runner = FakeRunner(command_responses(box))
    check_box(box, runner, ready_home(tmp_path, box))
    expected = set(command_responses(box))
    assert set(runner.argv) == expected
    flattened = " ".join(" ".join(argv) for argv in runner.argv)
    for forbidden in ("sso login", "ssh-keygen", "send-command", "start-session"):
        assert forbidden not in flattened


def test_tmux_mode_uses_tmux_version_check(box, tmp_path):
    tmux_box = RemoteBox(box.name, box.instance_id, box.region, box.aws_profile, mode="tmux")
    responses = command_responses(box)
    responses.pop(("herdr", "--version"))
    responses.pop(("ssh", "-o", "BatchMode=yes", "cao-box-1", "herdr", "--version"))
    responses[("ssh", "-o", "BatchMode=yes", "cao-box-1", "tmux", "-V")] = RunResult(0, "tmux 3.4")
    checks = checks_by_name(check_box(tmux_box, FakeRunner(responses), ready_home(tmp_path, box)))
    assert checks["tmux"].status == "OK"
    assert "herdr" not in checks


def test_runner_missing_binary_and_timeout_are_fix_without_traceback(box, tmp_path, monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr("cli_agent_orchestrator.services.remote.runner.subprocess.run", missing)
    result = Runner().run(["not-installed"])
    assert result.returncode == 1
    checks = check_box(
        box,
        FakeRunner({("aws", "--version"): result}),
        ready_home(tmp_path, box),
    )
    assert checks_by_name(checks)["aws-cli"].status == "FIX"
    assert "Traceback" not in "\n".join(check.rendered() for check in checks)


def test_remote_add_list_remove_and_private_file(tmp_path, monkeypatch):
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


def test_registry_atomic_write_uses_temporary_file_and_rename(tmp_path, monkeypatch, box):
    monkeypatch.setattr(registry, "CAO_HOME_DIR", tmp_path)
    calls: list[tuple[Path, Path]] = []
    original_replace = os.replace

    def recording_replace(source: str | Path, target: str | Path) -> None:
        calls.append((Path(source), Path(target)))
        original_replace(source, target)

    monkeypatch.setattr(registry.os, "replace", recording_replace)
    target = save(box)
    assert len(calls) == 1
    assert calls[0][1] == target
    assert calls[0][0].name.startswith(".box-1.")
    assert calls[0][0] != target
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_duplicate_add_replaces_registration(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "CAO_HOME_DIR", tmp_path)
    runner = CliRunner()
    base = [
        "remote",
        "add",
        "box-1",
        "--instance-id",
        "i-0123456789abcdef",
        "--region",
    ]
    assert runner.invoke(cli, [*base, "us-east-1", "--profile", "first"]).exit_code == 0
    assert runner.invoke(cli, [*base, "us-west-2", "--profile", "second"]).exit_code == 0
    assert load("box-1").aws_profile == "second"
    assert len(list((tmp_path / "remote").glob("*.json"))) == 1


def test_corrupt_registry_and_remove_missing_are_usage_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "CAO_HOME_DIR", tmp_path)
    directory = tmp_path / "remote"
    directory.mkdir()
    (directory / "box-1.json").write_text("{broken", encoding="utf-8")
    runner = CliRunner()
    corrupt = runner.invoke(cli, ["remote", "check", "box-1"])
    missing = runner.invoke(cli, ["remote", "remove", "box-2"])
    assert corrupt.exit_code == 2
    assert "invalid configuration" in corrupt.output
    assert missing.exit_code == 2
    assert "not registered" in missing.output


def test_remote_add_validates_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "CAO_HOME_DIR", tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "remote",
            "add",
            "BAD",
            "--instance-id",
            "bad",
            "--region",
            "nowhere",
            "--profile",
            "p",
        ],
    )
    assert result.exit_code == 2
    assert "box must match" in result.output


@pytest.mark.parametrize(
    ("checks", "expected_exit"),
    [([Check("OK", "aws-cli", "2.x")], 0), ([Check("FIX", "aws-cli", "install")], 1)],
)
def test_remote_check_text_json_shape_and_exit_codes(monkeypatch, checks, expected_exit):
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.remote.load",
        lambda _: RemoteBox("box", "i-0123456789abcdef", "us-east-1", "p"),
    )
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.remote.check_box", lambda _: checks)
    runner = CliRunner()
    text = runner.invoke(cli, ["remote", "check", "box"])
    result = runner.invoke(cli, ["remote", "check", "box", "--json"])
    assert text.exit_code == expected_exit
    assert result.exit_code == expected_exit
    assert json.loads(result.output) == [
        {"detail": checks[0].detail, "name": checks[0].name, "status": checks[0].status}
    ]


def test_remote_help_is_registered():
    result = CliRunner().invoke(cli, ["remote", "--help"])
    assert result.exit_code == 0
    assert {"add", "check", "list", "remove"} <= set(result.output.split())


def test_load_and_remove_missing_raise_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "CAO_HOME_DIR", tmp_path)
    with pytest.raises(RemoteConfigError, match="not registered"):
        load("box-1")
    with pytest.raises(RemoteConfigError, match="not registered"):
        remove("box-1")
