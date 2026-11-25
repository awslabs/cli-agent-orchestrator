"""Consent, SSH-file and argv tests for ``cao remote connect``."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
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
    assert first.endswith(original)
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
    assert config.read_text(encoding="utf-8").endswith("Host personal\n  User me")


@pytest.mark.parametrize("edit", ["blank", "missing_dash", "moved_main"])
def test_hand_edits_survive_three_updates(box, tmp_path, edit):
    config = tmp_path / ".ssh" / "config"
    config.parent.mkdir()
    original = "Host *\n  ForwardAgent yes\nHost personal\n  User me\n"
    config.write_text(original, encoding="utf-8")
    update_ssh_config(box, tmp_path)
    content = config.read_text(encoding="utf-8")
    main_start = content.index("# >>> cao remote: cao-box-1 ")
    dash_start = content.index("# >>> cao remote: cao-box-1-dash ")
    dash_end = content.index("\n", content.index("# <<< cao remote: cao-box-1-dash ")) + 1
    main_end = content.index("\n", content.index("# <<< cao remote: cao-box-1 ")) + 1
    if edit == "blank":
        content = content[:main_end] + "\n" + content[main_end:]
    elif edit == "missing_dash":
        content = content[:dash_start] + content[dash_end:]
    else:
        main = content[main_start:main_end]
        content = main + content[:main_start] + content[main_end:]
    content += "Host work\n  User colleague\n"
    config.write_text(content, encoding="utf-8")
    for _ in range(2):
        before = config.read_text(encoding="utf-8")
        update_ssh_config(box, tmp_path)
        after = config.read_text(encoding="utf-8")
        assert "Host work\n  User colleague\n" in after
        assert "Host *\n  ForwardAgent yes\nHost personal\n  User me\n" in after
        if after != before:
            assert (config.parent / "config.cao-backup").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("damage", ["missing_end", "duplicate", "out_of_order", "nested"])
def test_corrupt_markers_fail_closed(box, tmp_path, damage):
    config = tmp_path / ".ssh" / "config"
    config.parent.mkdir()
    update_ssh_config(box, tmp_path)
    text = config.read_text(encoding="utf-8")
    start = next(line for line in text.splitlines() if line.startswith("# >>> cao remote:"))
    end = next(line for line in text.splitlines() if line.startswith("# <<< cao remote:"))
    if damage == "missing_end":
        text = text.replace(end + "\n", "")
    elif damage == "duplicate":
        text += start + "\nHost duplicate\n" + end + "\n"
    elif damage == "out_of_order":
        text = end + "\n" + text.replace(end + "\n", "", 1)
    else:
        text = text.replace(start + "\n", start + "\n" + start + "\n", 1)
    config.write_text(text, encoding="utf-8")
    backup_path = config.parent / "config.cao-backup"
    backup = backup_path.read_bytes() if backup_path.exists() else None
    with pytest.raises(ConnectError):
        update_ssh_config(box, tmp_path)
    assert config.read_text(encoding="utf-8") == text
    assert (backup_path.read_bytes() if backup_path.exists() else None) == backup
    with pytest.raises(ConnectError):
        remove_ssh_artifacts(box, tmp_path)
    assert config.read_text(encoding="utf-8") == text


def test_written_blocks_precede_user_hosts_and_share_dashboard_settings(box, tmp_path):
    config = tmp_path / ".ssh" / "config"
    config.parent.mkdir()
    config.write_text("Host *\n  ForwardAgent yes\n", encoding="utf-8")
    update_ssh_config(box, tmp_path)
    text = config.read_text(encoding="utf-8")
    assert text.index("Host cao-box-1") < text.index("Host *")
    assert "Host cao-box-1 cao-box-1-dash" in text
    assert "StrictHostKeyChecking yes" in text
    assert "ForwardAgent no" in text
    if shutil.which("ssh"):
        for host in ("cao-box-1", "cao-box-1-dash"):
            output = subprocess.run(
                ["ssh", "-G", "-F", str(config), host], capture_output=True, text=True, check=True
            ).stdout.lower()
            assert "stricthostkeychecking true" in output or "stricthostkeychecking yes" in output
            assert "forwardagent no" in output
            assert "hostkeyalias cao-box-1" in output
            assert "proxycommand sh -c" in output
            assert "userknownhostsfile " in output and "/cao_known_hosts" in output
            if host.endswith("-dash"):
                assert "localforward 19889 " in output and "9889" in output


def test_invalid_profile_and_dash_suffix_rejected(box):
    from cli_agent_orchestrator.services.remote.registry import RemoteConfigError

    for name, profile in (("box-1", "x$(touch${IFS}file)"), ("dev-dash", "work")):
        with pytest.raises(RemoteConfigError):
            RemoteBox(name, box.instance_id, box.region, profile)


def test_existing_invalid_registration_is_rejected(box, tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.remote import registry

    monkeypatch.setattr(registry, "CAO_HOME_DIR", tmp_path)
    for name, profile in (("box-1", "x$(touch${IFS}file)"), ("dev-dash", "work")):
        entry = tmp_path / "remote" / f"{name}.json"
        entry.parent.mkdir(exist_ok=True)
        entry.write_text(
            json.dumps(
                dict(
                    name=name,
                    instance_id=box.instance_id,
                    region=box.region,
                    profile=profile,
                    mode="herdr",
                )
            ),
            encoding="utf-8",
        )
        with pytest.raises(registry.RemoteConfigError):
            registry.load(name)


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


def test_host_key_accepts_comment_but_pins_only_key(box, tmp_path):
    assert parse_host_key("ssh-ed25519 c2FmZS1rZXk= root@host") == "ssh-ed25519 c2FmZS1rZXk="
    assert parse_host_key("ssh-ed25519 c2FmZS1rZXk=") == "ssh-ed25519 c2FmZS1rZXk="


def test_run_command_parameters_are_json(box, tmp_path):
    runner = FakeRunner()
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "cao-box-1_ed25519.pub").write_text(
        "ssh-ed25519 cHVibGljLWtleQ== workstation", encoding="utf-8"
    )
    install_public_key(box, runner, tmp_path)
    command = next(argv for argv in runner.argv if "send-command" in argv)
    parameters = json.loads(command[command.index("--parameters") + 1])
    assert len(parameters["commands"]) == 1
    assert "runuser -u" in parameters["commands"][0]


def test_unreachable_probe_exits_one_without_ready(box, tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: [])
    runner = FakeRunner()
    original = runner.run

    def unreachable(argv, timeout=15.0):
        if argv[0] == "ssh" and "true" in argv:
            return RunResult(255, stderr="Connection refused")
        return original(argv, timeout)

    monkeypatch.setattr(runner, "run", unreachable)
    messages = []
    assert connect(box, runner, tmp_path, lambda _: True, messages.append, no_attach=True) == 1
    assert any(line.startswith("FIX  ssh:") for line in messages)
    assert "Ready:" not in messages


@pytest.mark.parametrize("failed_command", ["aws", "ssh-keygen"])
def test_failed_interactive_setup_exits_one(box, tmp_path, monkeypatch, failed_command):
    from cli_agent_orchestrator.services.remote.checks import Check

    checks = [Check("FIX", "aws-login", "login")] if failed_command == "aws" else []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: checks
    )
    runner = FakeRunner()

    def failed(argv):
        runner.interactive.append(tuple(argv))
        return RunResult(7)

    monkeypatch.setattr(runner, "run_interactive", failed)
    assert connect(box, runner, tmp_path, lambda _: True, lambda _: None) == 1
    assert len(runner.interactive) == 1


def test_declined_prompt_exits_one(box, tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: [])
    assert connect(box, FakeRunner(), tmp_path, lambda _: False, lambda _: None) == 1


def test_attach_exit_code_is_forwarded(box, tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: [])
    runner = FakeRunner()
    original = runner.run_interactive

    def attach_failure(argv):
        if argv[0] == "herdr":
            return RunResult(17)
        return original(argv)

    monkeypatch.setattr(runner, "run_interactive", attach_failure)
    assert connect(box, runner, tmp_path, lambda _: True, lambda _: None) == 17


def test_rekey_shows_fingerprints_and_asks_again(box, tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: [])
    known = tmp_path / ".ssh" / "cao_known_hosts"
    known.parent.mkdir()
    known.write_text("cao-box-1 ssh-ed25519 b2xkLWtleQ==\n", encoding="utf-8")
    actions = []
    messages = []

    def ask(action):
        actions.append(action)
        return "Replace changed" not in action

    assert connect(box, FakeRunner(), tmp_path, ask, messages.append, rekey=True) == 1
    assert len([action for action in actions if "host key" in action.lower()]) >= 2
    assert any("SHA256:" in message for message in messages)
    assert known.read_text(encoding="utf-8") == "cao-box-1 ssh-ed25519 b2xkLWtleQ==\n"


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


@pytest.mark.parametrize("declined", ["login", "key", "config", "pin", "install"])
def test_each_declined_action_preserves_its_artifacts(box, tmp_path, monkeypatch, declined):
    from cli_agent_orchestrator.services.remote.checks import Check

    checks = [Check("FIX", "aws-login", "login")] if declined == "login" else []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: checks
    )
    ssh = tmp_path / ".ssh"
    if declined in {"config", "pin", "install"}:
        ssh.mkdir()
        (ssh / "cao-box-1_ed25519").write_text("private", encoding="utf-8")
        (ssh / "cao-box-1_ed25519.pub").write_text(
            "ssh-ed25519 cHVibGljLWtleQ== workstation", encoding="utf-8"
        )
    runner = FakeRunner()
    if declined == "install":
        original_run = runner.run

        def denied(argv, timeout=15.0):
            if argv[0] == "ssh" and "true" in argv:
                return RunResult(255, stderr="Permission denied (publickey).")
            return original_run(argv, timeout)

        monkeypatch.setattr(runner, "run", denied)
    prefixes = {
        "login": "Run aws sso login",
        "key": "Create SSH key",
        "config": "Update managed SSH entries",
        "pin": "Pin host key",
        "install": "Install SSH public key",
    }
    assert (
        connect(
            box,
            runner,
            tmp_path,
            lambda action: not action.startswith(prefixes[declined]),
            lambda _: None,
            no_attach=True,
        )
        == 1
    )
    if declined in {"login", "key", "config"}:
        assert not (ssh / "config").exists()
    if declined == "pin":
        assert not (ssh / "cao_known_hosts").exists()
    assert not any("authorized_keys" in " ".join(argv) for argv in runner.argv)


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
    assert attach_argv(tmux) == [
        "ssh",
        "-o",
        "ForwardAgent=no",
        "-t",
        "cao-box-1",
        "tmux",
        "attach",
    ]


def test_probe_disables_agent_forwarding(box, tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.services.remote.connect.check_box", lambda *_: [])
    runner = FakeRunner()
    assert connect(box, runner, tmp_path, lambda _: True, lambda _: None, no_attach=True) == 0
    probe = next(argv for argv in runner.argv if argv[0] == "ssh" and "true" in argv)
    assert probe[probe.index("-o") + 1] == "ForwardAgent=no"


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
        "cao-box-1 ssh-ed25519 c2FmZQ==\ncao-box-10 ssh-ed25519 c2FmZQ==\nother ssh-ed25519 b3RoZXI=\n",
        encoding="utf-8",
    )
    assert remove_ssh_artifacts(box, tmp_path)
    assert config.read_text(encoding="utf-8") == other
    assert known.read_text(encoding="utf-8") == (
        "cao-box-10 ssh-ed25519 c2FmZQ==\nother ssh-ed25519 b3RoZXI=\n"
    )
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
    assert result.exit_code == (0 if expected_cleanup else 1)
    if expected_cleanup:
        assert calls == [(box, tmp_path), "remove"]
    else:
        assert not calls


def test_remote_remove_no_input_keeps_registration_and_entries(box, tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.remote import registry

    monkeypatch.setattr(registry, "CAO_HOME_DIR", tmp_path / "cao")
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.remote.Path.home", lambda: tmp_path)
    registry.save(box)
    update_ssh_config(box, tmp_path)
    config = tmp_path / ".ssh" / "config"
    before = config.read_bytes()
    result = CliRunner().invoke(cli, ["remote", "remove", box.name], input="")
    assert result.exit_code == 1
    assert registry.load(box.name) == box
    assert config.read_bytes() == before


def test_runtime_connect_error_is_exit_one(box, tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.remote.load", lambda _: box)

    def failure(*args, **kwargs):
        raise ConnectError("SSM Run Command failed")

    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.remote.connect", failure)
    result = CliRunner().invoke(cli, ["remote", "connect", box.name, "--yes"])
    assert result.exit_code == 1
    assert "SSM Run Command failed" in result.output
    assert "Usage:" not in result.output
