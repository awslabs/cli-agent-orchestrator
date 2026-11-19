"""Consent, SSH-file and argv tests for ``cao remote connect``."""

from __future__ import annotations

import json
import stat
from typing import Sequence

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.services.remote.connect import (
    ConnectError,
    attach_argv,
    connect,
    ensure_host_key,
    install_public_key,
    parse_host_key,
    parse_public_key,
    remove_ssh_artifacts,
    update_ssh_config,
)
from cli_agent_orchestrator.services.remote.registry import RemoteBox
from cli_agent_orchestrator.services.remote.runner import RunResult


class FakeRunner:
    def __init__(self) -> None:
        self.argv: list[tuple[str, ...]] = []
        self.interactive: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], timeout: float = 15.0) -> RunResult:
        del timeout
        command = tuple(argv)
        self.argv.append(command)
        if "send-command" in command:
            return RunResult(0, json.dumps({"Command": {"CommandId": "cmd-1"}}))
        if "get-command-invocation" in command:
            return RunResult(
                0,
                json.dumps(
                    {
                        "Status": "Success",
                        "StandardOutputContent": "ssh-ed25519 c2FmZS1rZXk=\n",
                    }
                ),
            )
        if command == ("aws", "--version"):
            return RunResult(0, "aws-cli/2.1")
        if command == ("aws", "configure", "list-profiles"):
            return RunResult(0, "work\n")
        if command[:3] == ("aws", "ec2", "describe-instances"):
            return RunResult(0, "running\n")
        if command[:3] == ("aws", "ssm", "describe-instance-information"):
            return RunResult(0, "Online\n")
        return RunResult(0, "herdr 1.0")

    def run_interactive(self, argv: Sequence[str]) -> RunResult:
        self.interactive.append(tuple(argv))
        return RunResult(0)


@pytest.fixture
def box() -> RemoteBox:
    return RemoteBox("box-1", "i-0123456789abcdef", "us-east-1", "work")


def test_config_create_update_idempotence_backup_mode_and_preservation(box, tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    config = ssh / "config"
    original = "Host personal\r\n  HostName example.test\r\n"
    config.write_text(original, encoding="utf-8", newline="")
    assert update_ssh_config(box, tmp_path)
    with config.open(encoding="utf-8", newline="") as handle:
        first = handle.read()
    assert original in first
    assert "Host cao-box-1" in first
    assert "Host cao-box-1-dash" in first
    assert "\r\n" in first
    with (ssh / "config.cao-backup").open(encoding="utf-8", newline="") as handle:
        assert handle.read() == original
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert not update_ssh_config(box, tmp_path)
    with config.open(encoding="utf-8", newline="") as handle:
        assert handle.read() == first


def test_config_without_trailing_newline_is_preserved(box, tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    config = ssh / "config"
    config.write_text("Host personal\n  User me", encoding="utf-8")
    update_ssh_config(box, tmp_path)
    assert config.read_text(encoding="utf-8").startswith("Host personal\n  User me\n")


@pytest.mark.parametrize(
    "value",
    [
        "garbage",
        "ssh-rsa c2FmZS1rZXk=",
        "ssh-ed25519 c2FmZS1rZXk=\nssh-ed25519 c2FmZS1rZXk=",
        "ssh-ed25519 !!!",
    ],
)
def test_host_key_validation_rejects_invalid_values(value):
    with pytest.raises(ConnectError, match="invalid ed25519 host key"):
        parse_host_key(value)


def test_host_key_changed_stops_then_rekeys(box, tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    known = ssh / "cao_known_hosts"
    known.write_text("cao-box-1 ssh-ed25519 b2xkLWtleQ==\n", encoding="utf-8")
    with pytest.raises(ConnectError, match="changed"):
        ensure_host_key(box, FakeRunner(), tmp_path)
    assert ensure_host_key(box, FakeRunner(), tmp_path, rekey=True)
    assert "c2FmZS1rZXk=" in known.read_text(encoding="utf-8")


def test_connect_no_consent_leaves_files_unchanged(box, tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: [])
    runner = FakeRunner()
    messages: list[str] = []
    connect(box, runner, tmp_path, lambda _: False, messages.append, no_attach=True)
    assert not (tmp_path / ".ssh").exists()
    assert not runner.interactive


def test_connect_no_attach_writes_and_reports_ready(box, tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: [])
    runner = FakeRunner()
    messages: list[str] = []
    connect(box, runner, tmp_path, lambda _: True, messages.append, no_attach=True)
    assert (tmp_path / ".ssh" / "config").exists()
    assert any(line == "Ready:" for line in messages)
    assert any(line.startswith("Attach: herdr --remote cao-box-1") for line in messages)
    assert runner.interactive[0][0] == "ssh-keygen"
    assert all(argv[0] != "herdr" for argv in runner.interactive)


def test_attach_argv_per_mode(box):
    assert attach_argv(box) == [
        "herdr",
        "--remote",
        "cao-box-1",
        "--session",
        "cao-i-0123456789abcdef",
    ]
    tmux = RemoteBox(box.name, box.instance_id, box.region, box.aws_profile, mode="tmux")
    assert attach_argv(tmux) == ["ssh", "-t", "cao-box-1", "tmux", "attach"]


def test_allowlist_uses_no_private_key_read_or_raw_public_key(box, tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: [])
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    public = "ssh-ed25519 cHVibGljLWtleQ== comment"
    (ssh / "cao-box-1_ed25519.pub").write_text(public, encoding="utf-8")
    runner = FakeRunner()
    connect(box, runner, tmp_path, lambda _: True, lambda _: None, no_attach=True)
    rendered = "\n".join(" ".join(argv) for argv in [*runner.argv, *runner.interactive])
    assert public not in rendered
    keygen = next(argv for argv in runner.interactive if argv[0] == "ssh-keygen")
    assert keygen[keygen.index("-f") + 1].endswith("cao-box-1_ed25519")
    assert "send-command" in rendered
    assert "cat /etc/ssh/ssh_host_ed25519_key.pub" in rendered


def test_canary_is_not_rendered(box, tmp_path, monkeypatch):
    aws = tmp_path / ".aws"
    aws.mkdir()
    canary = "CANARY-NOT-IN-CONNECT-OUTPUT"
    (aws / "credentials").write_text(f"[work]\naws_access_key_id={canary}\n", encoding="utf-8")
    monkeypatch.setattr("cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: [])
    messages: list[str] = []
    connect(box, FakeRunner(), tmp_path, lambda _: False, messages.append, no_attach=True)
    assert canary not in "\n".join(messages)


@pytest.mark.parametrize("value", ["not-a-key", "ssh-rsa c2FmZQ==", "ssh-ed25519 !!!", "a\nb"])
def test_public_key_validation_rejects_non_public_input(value):
    with pytest.raises(ConnectError, match="public key"):
        parse_public_key(value)


def test_public_key_run_command_uses_base64_and_is_idempotent(box, tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    public = "ssh-ed25519 cHVibGljLWtleQ== workstation"
    (ssh / "cao-box-1_ed25519.pub").write_text(public, encoding="utf-8")
    runner = FakeRunner()
    install_public_key(box, runner, tmp_path)
    install_public_key(box, runner, tmp_path)
    commands = [" ".join(argv) for argv in runner.argv if "send-command" in argv]
    assert len(commands) == 2
    assert public not in "\n".join(commands)
    assert "c3NoLWVkMjU1MTkgY0hWaWJHbGpMV3RsZVE9PSB3b3Jrc3RhdGlvbg==" in commands[0]
    assert "runuser -u" in commands[0]
    assert "authorized_keys" in commands[0]


def test_public_key_denial_prints_key_and_no_consent_does_not_send_command(
    box, tmp_path, monkeypatch
):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    public = "ssh-ed25519 cHVibGljLWtleQ== workstation"
    (ssh / "cao-box-1_ed25519").write_text("private", encoding="utf-8")
    (ssh / "cao-box-1_ed25519.pub").write_text(public, encoding="utf-8")
    runner = FakeRunner()

    def denied(argv, timeout=15.0):
        result = FakeRunner.run(runner, argv, timeout)
        if argv[0] == "ssh":
            return RunResult(255, stderr="Permission denied (publickey).")
        return result

    monkeypatch.setattr(runner, "run", denied)
    monkeypatch.setattr("cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: [])
    messages: list[str] = []
    connect(
        box,
        runner,
        tmp_path,
        lambda action: not action.startswith("Install SSH public key"),
        messages.append,
        no_attach=True,
    )
    assert f"BOX_SSH_PUBLIC_KEY='{public}'" in messages
    assert not any("authorized_keys" in " ".join(argv) for argv in runner.argv)


@pytest.mark.parametrize("status, expected", [("Failed", "failed"), ("TimedOut", "failed")])
def test_public_key_command_failure_is_clear_fix(box, tmp_path, monkeypatch, status, expected):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    public = "ssh-ed25519 cHVibGljLWtleQ== workstation"
    (ssh / "cao-box-1_ed25519").write_text("private", encoding="utf-8")
    (ssh / "cao-box-1_ed25519.pub").write_text(public, encoding="utf-8")
    runner = FakeRunner()

    invocation_count = 0

    def result(argv, timeout=15.0):
        nonlocal invocation_count
        command = tuple(argv)
        runner.argv.append(command)
        if command[0] == "ssh":
            return RunResult(255, stderr="Permission denied (publickey).")
        if "send-command" in command:
            return RunResult(0, json.dumps({"Command": {"CommandId": "cmd-1"}}))
        if "get-command-invocation" in command:
            invocation_count += 1
            if invocation_count == 1:
                return RunResult(
                    0,
                    json.dumps(
                        {
                            "Status": "Success",
                            "StandardOutputContent": "ssh-ed25519 c2FmZS1rZXk=\n",
                        }
                    ),
                )
            return RunResult(0, json.dumps({"Status": status}))
        return RunResult(0, "herdr 1.0")

    monkeypatch.setattr(runner, "run", result)
    monkeypatch.setattr("cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: [])
    messages: list[str] = []
    connect(box, runner, tmp_path, lambda _: True, messages.append, no_attach=True)
    assert any(line == f"FIX  ssh key: SSM Run Command {expected}" for line in messages)


def test_remove_ssh_artifacts_preserves_other_blocks_and_is_idempotent(box, tmp_path):
    update_ssh_config(box, tmp_path)
    ssh = tmp_path / ".ssh"
    config = ssh / "config"
    other = "\nHost personal\n  HostName example.test\n"
    with config.open("a", encoding="utf-8", newline="") as handle:
        handle.write(other)
    known = ssh / "cao_known_hosts"
    known.write_text(
        "cao-box-1 ssh-ed25519 c2FmZQ==\nother ssh-ed25519 b3RoZXI=\n", encoding="utf-8"
    )
    assert remove_ssh_artifacts(box, tmp_path)
    assert config.read_text(encoding="utf-8") == other
    assert known.read_text(encoding="utf-8") == "other ssh-ed25519 b3RoZXI=\n"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(known.stat().st_mode) == 0o600
    assert not remove_ssh_artifacts(box, tmp_path)


def test_remove_ssh_artifacts_handles_missing_files(box, tmp_path):
    assert not remove_ssh_artifacts(box, tmp_path)


@pytest.mark.parametrize(
    ("arguments", "input_text", "expected_cleanup"),
    [
        (["--yes"], None, True),
        ([], "n\n", False),
    ],
)
def test_remote_remove_cleanup_requires_consent(
    box, tmp_path, monkeypatch, arguments, input_text, expected_cleanup
):
    calls: list[object] = []
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.remote.load", lambda _: box)
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.remote.remove", lambda _: calls.append("remove")
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.remote.has_ssh_artifacts", lambda *_: True
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.remote.remove_ssh_artifacts",
        lambda actual_box, home: calls.append((actual_box, home)),
    )
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.remote.Path.home", lambda: tmp_path)
    result = CliRunner().invoke(cli, ["remote", "remove", box.name, *arguments], input=input_text)
    assert result.exit_code == 0
    assert calls[0] == "remove"
    assert (len(calls) == 2) is expected_cleanup
