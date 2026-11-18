"""Mutating, consent-gated setup for an experimental Remote CAO box."""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Callable

from .checks import check_box
from .registry import RemoteBox
from .runner import Runner, RunResult

Ask = Callable[[str], bool]
Tell = Callable[[str], None]
MARKER = "# >>> cao remote: {host} (managed by CAO; edits are replaced) >>>"
END_MARKER = "# <<< cao remote: {host} (managed by CAO; edits are replaced) <<<"
HOST_KEY_RE = re.compile(r"^ssh-ed25519 ([A-Za-z0-9+/]+={0,2})$")


class ConnectError(ValueError):
    """A safe connect operation could not be completed."""


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _managed_blocks(box: RemoteBox, newline: str) -> str:
    host = f"cao-{box.name}"
    key = f"~/.ssh/{host}_ed25519"
    proxy = (
        f'ProxyCommand sh -c "aws ssm start-session --profile {box.aws_profile} '
        f"--region {box.region} --target %h --document-name AWS-StartSSHSession "
        "--parameters 'portNumber=%p'\""
    )
    common = [
        f"Host {host}",
        f"  HostName {box.instance_id}",
        f"  HostKeyAlias {host}",
        f"  User {box.user}",
        f"  IdentityFile {key}",
        "  IdentitiesOnly yes",
        "  UserKnownHostsFile ~/.ssh/cao_known_hosts",
        "  StrictHostKeyChecking yes",
        f"  {proxy}",
        "  ForwardAgent no",
        "  ServerAliveInterval 30",
        "  ServerAliveCountMax 3",
    ]
    dash = [
        f"Host {host}-dash",
        f"  HostName {host}",
        "  LocalForward 19889 127.0.0.1:9889",
        "  ExitOnForwardFailure yes",
    ]
    chunks = [
        MARKER.format(host=host),
        *common,
        END_MARKER.format(host=host),
        MARKER.format(host=f"{host}-dash"),
        *dash,
        END_MARKER.format(host=f"{host}-dash"),
        "",
    ]
    return newline.join(chunks)


def update_ssh_config(box: RemoteBox, home: Path) -> bool:
    """Idempotently replace only this box's marked SSH blocks."""
    path = home / ".ssh" / "config"
    if path.exists():
        with path.open(encoding="utf-8", newline="") as handle:
            original = handle.read()
    else:
        original = ""
    newline = "\r\n" if "\r\n" in original else "\n"
    host = f"cao-{box.name}"
    pattern = re.compile(
        rf"(?P<prefix>^|\r?\n){re.escape(MARKER.format(host=host))}\r?\n.*?"
        rf"{re.escape(END_MARKER.format(host=host))}\r?\n?"
        rf"{re.escape(MARKER.format(host=f'{host}-dash'))}\r?\n.*?"
        rf"{re.escape(END_MARKER.format(host=f'{host}-dash'))}\r?\n?",
        re.DOTALL,
    )
    replacement = _managed_blocks(box, newline)
    found = pattern.search(original) is not None
    updated = pattern.sub(lambda match: match.group("prefix") + replacement, original)
    if found and updated == original:
        return False
    if not found:
        updated = original + ("" if not original or original.endswith(("\n", "\r")) else newline)
        updated += replacement
    backup = path.with_name("config.cao-backup")
    if original and not backup.exists():
        _atomic_write(backup, original)
    _atomic_write(path, updated)
    return True


def parse_host_key(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ConnectError("SSM returned an invalid ed25519 host key (expected exactly one line)")
    match = HOST_KEY_RE.fullmatch(lines[0])
    if not match:
        raise ConnectError("SSM returned an invalid ed25519 host key")
    try:
        base64.b64decode(match.group(1), validate=True)
    except ValueError as exc:
        raise ConnectError("SSM returned an invalid ed25519 host key") from exc
    return lines[0]


def _command_id(result: RunResult) -> str:
    try:
        return str(json.loads(result.stdout)["Command"]["CommandId"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConnectError("could not obtain SSM command id") from exc


def _run_command(runner: Runner, box: RemoteBox, command: str, timeout: float = 30.0) -> str:
    sent = runner.run(
        [
            "aws",
            "ssm",
            "send-command",
            "--document-name",
            "AWS-RunShellScript",
            "--instance-ids",
            box.instance_id,
            "--parameters",
            f"commands={command}",
            "--profile",
            box.aws_profile,
            "--region",
            box.region,
            "--output",
            "json",
        ]
    )
    if sent.returncode:
        raise ConnectError("SSM Run Command could not be started")
    command_id = _command_id(sent)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = runner.run(
            [
                "aws",
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                box.instance_id,
                "--profile",
                box.aws_profile,
                "--region",
                box.region,
                "--output",
                "json",
            ]
        )
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
                if payload.get("Status") == "Success":
                    return str(payload.get("StandardOutputContent", ""))
                if payload.get("Status") in {"Failed", "Cancelled", "TimedOut"}:
                    raise ConnectError("SSM Run Command failed")
            except json.JSONDecodeError as exc:
                raise ConnectError("SSM returned invalid command output") from exc
        time.sleep(0.2)
    raise ConnectError("SSM Run Command timed out")


def ensure_host_key(box: RemoteBox, runner: Runner, home: Path, rekey: bool = False) -> bool:
    """Pin one SSM-obtained key, refusing an unexpected replacement."""
    path = home / ".ssh" / "cao_known_hosts"
    host = f"cao-{box.name}"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    matching = [line for line in old.splitlines() if line.startswith(f"{host} ")]
    key = parse_host_key(_run_command(runner, box, "cat /etc/ssh/ssh_host_ed25519_key.pub"))
    desired = f"{host} {key}"
    if matching and matching[0] == desired:
        return False
    if matching and not rekey:
        raise ConnectError(f"host key for {host} changed; rerun with --rekey after verification")
    lines = [line for line in old.splitlines() if not line.startswith(f"{host} ")]
    lines.append(desired)
    _atomic_write(path, "\n".join(lines) + "\n")
    return True


def attach_argv(box: RemoteBox) -> list[str]:
    host = f"cao-{box.name}"
    if box.mode == "herdr":
        return ["herdr", "--remote", host, "--session", f"cao-{box.instance_id}"]
    return ["ssh", "-t", host, "tmux", "attach"]


def ready_lines(box: RemoteBox) -> list[str]:
    host = f"cao-{box.name}"
    lines = ["Ready:", f"OK   SSH config: {host}", f"OK   host key: {host}"]
    lines.append(f"Attach: {' '.join(attach_argv(box))}")
    if box.mode == "herdr":
        lines.append(
            f"First time: herdr machine add {host} --remote-session cao-{box.instance_id} --label {box.name}"
        )
    lines.append(f"Dashboard: ssh -N {host}-dash  # http://127.0.0.1:19889")
    return lines


def connect(
    box: RemoteBox,
    runner: Runner,
    home: Path,
    ask: Ask,
    tell: Tell,
    no_attach: bool = False,
    rekey: bool = False,
) -> None:
    """Run checks, perform approved setup, then attach or print instructions."""
    checks = check_box(box, runner, home)
    for check in checks:
        tell(check.rendered())
    if any(check.name == "aws-login" and check.status == "FIX" for check in checks):
        action = f"Run aws sso login --profile {box.aws_profile}"
        tell(action)
        if not ask(action):
            return
        runner.run_interactive(["aws", "sso", "login", "--profile", box.aws_profile])
    key = home / ".ssh" / f"cao-{box.name}_ed25519"
    if not key.exists():
        action = f"Create SSH key {key.name}"
        tell(action)
        if not ask(action):
            return
        key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        runner.run_interactive(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-C", f"cao-{box.name}"]
        )
    action = f"Update managed SSH entries for cao-{box.name}"
    tell(action)
    if not ask(action):
        return
    update_ssh_config(box, home)
    action = f"Pin host key for cao-{box.name} from SSM"
    tell(action)
    if not ask(action):
        return
    ensure_host_key(box, runner, home, rekey)
    if no_attach:
        for line in ready_lines(box):
            tell(line)
        return
    runner.run_interactive(attach_argv(box))
