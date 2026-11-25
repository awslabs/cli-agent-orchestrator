"""Mutating, consent-gated setup for an experimental Remote CAO box."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
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
PUBLIC_KEY_RE = re.compile(r"^ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?: [^\r\n]+)?$")


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
    profile = shlex.quote(box.aws_profile).replace("%", "%%")
    proxy = (
        f'ProxyCommand sh -c "aws ssm start-session --profile {profile} '
        f"--region {box.region} --target %h --document-name AWS-StartSSHSession "
        "--parameters 'portNumber=%p'\""
    )
    common = [
        f"Host {host} {host}-dash",
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
        "  LocalForward 19889 127.0.0.1:9889",
        "  ExitOnForwardFailure yes",
    ]
    chunks = [
        MARKER.format(host=f"{host}-dash"),
        *dash,
        END_MARKER.format(host=f"{host}-dash"),
        MARKER.format(host=host),
        *common,
        END_MARKER.format(host=host),
        "",
    ]
    return newline.join(chunks)


def _managed_spans(content: str, hosts: tuple[str, ...]) -> list[tuple[int, int]]:
    lines = content.splitlines(keepends=True)
    starts = {MARKER.format(host=host): host for host in hosts}
    ends = {END_MARKER.format(host=host): host for host in hosts}
    spans: list[tuple[int, int]] = []
    active: tuple[str, int] | None = None
    seen: set[str] = set()
    for index, line in enumerate(lines):
        marker = line.rstrip("\r\n")
        if marker.startswith(("# >>> cao remote:", "# <<< cao remote:")):
            if marker in starts:
                host = starts[marker]
                if active or host in seen:
                    raise ConnectError("SSH config has duplicate or nested CAO markers")
                active = (host, index)
            elif marker in ends:
                host = ends[marker]
                if active is None or active[0] != host:
                    raise ConnectError("SSH config has out-of-order CAO markers")
                spans.append((active[1], index + 1))
                seen.add(host)
                active = None
            elif active:
                raise ConnectError("SSH config has nested CAO markers")
    if active:
        raise ConnectError("SSH config has an unterminated CAO marker")
    return spans


def _without_spans(content: str, spans: list[tuple[int, int]]) -> str:
    lines = content.splitlines(keepends=True)
    for start, end in sorted(spans, reverse=True):
        del lines[start:end]
    return "".join(lines)


def _insert_before_hosts(content: str, block: str, newline: str) -> str:
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if re.match(r"\s*(?:Host|Match)\s", line, flags=re.IGNORECASE):
            prefix = "" if index == 0 or lines[index - 1].endswith(("\n", "\r")) else newline
            return "".join(lines[:index]) + prefix + block + "".join(lines[index:])
    return content + (newline if content and not content.endswith(("\n", "\r")) else "") + block


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
    spans = _managed_spans(original, (host, f"{host}-dash"))
    replacement = _managed_blocks(box, newline)
    updated = _insert_before_hosts(_without_spans(original, spans), replacement, newline)
    if updated == original:
        return False
    backup = path.with_name("config.cao-backup")
    if original:
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


def parse_public_key(value: str) -> str:
    """Accept exactly one syntactically valid ed25519 public-key line."""
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ConnectError("SSH public key must be exactly one ed25519 public-key line")
    match = PUBLIC_KEY_RE.fullmatch(lines[0])
    if not match:
        raise ConnectError("SSH public key must be an ed25519 public-key line")
    try:
        base64.b64decode(match.group(1), validate=True)
    except ValueError as exc:
        raise ConnectError("SSH public key must be an ed25519 public-key line") from exc
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


def install_public_key(box: RemoteBox, runner: Runner, home: Path) -> None:
    """Install this laptop's public key using a bounded, idempotent Run Command."""
    path = home / ".ssh" / f"cao-{box.name}_ed25519.pub"
    if not path.exists():
        raise ConnectError(f"SSH public key {path.name} is missing")
    public_key = parse_public_key(path.read_text(encoding="utf-8"))
    encoded = base64.b64encode(public_key.encode("utf-8")).decode("ascii")
    script = (
        "umask 077; mkdir -p ~/.ssh; chmod 700 ~/.ssh; "
        "touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; "
        f"key=$(printf %s {encoded} | base64 -d); "
        'grep -qxF "$key" ~/.ssh/authorized_keys || '
        "printf '%s\\n' \"$key\" >> ~/.ssh/authorized_keys"
    )
    command = f"runuser -u {shlex.quote(box.user)} -- sh -c {shlex.quote(script)}"
    _run_command(runner, box, command)


def remove_ssh_artifacts(box: RemoteBox, home: Path) -> bool:
    """Remove only a box's marked config blocks and pinned host-key line."""
    host = f"cao-{box.name}"
    changed = False
    config = home / ".ssh" / "config"
    if config.exists():
        with config.open(encoding="utf-8", newline="") as handle:
            original = handle.read()
        updated = _without_spans(original, _managed_spans(original, (host, f"{host}-dash")))
        if updated != original:
            _atomic_write(config.with_name("config.cao-backup"), original)
            _atomic_write(config, updated)
            changed = True
    known_hosts = home / ".ssh" / "cao_known_hosts"
    if known_hosts.exists():
        with known_hosts.open(encoding="utf-8", newline="") as handle:
            original = handle.read()
        updated = re.sub(rf"(?m)^{re.escape(host)} [^\r\n]*(?:\r?\n|$)", "", original)
        if updated != original:
            _atomic_write(known_hosts, updated)
            changed = True
    return changed


def has_ssh_artifacts(box: RemoteBox, home: Path) -> bool:
    """Return whether this box has managed local SSH artifacts to clean up."""
    host = f"cao-{box.name}"
    config = home / ".ssh" / "config"
    if config.exists():
        config_text = config.read_text(encoding="utf-8")
        if any(
            MARKER.format(host=managed_host) in config_text
            for managed_host in (host, f"{host}-dash")
        ):
            return True
    known_hosts = home / ".ssh" / "cao_known_hosts"
    return known_hosts.exists() and any(
        line.startswith(f"{host} ") for line in known_hosts.read_text(encoding="utf-8").splitlines()
    )


def attach_argv(box: RemoteBox) -> list[str]:
    host = f"cao-{box.name}"
    if box.mode == "herdr":
        return ["herdr", "--remote", host, "--session", f"cao-{box.instance_id}"]
    return ["ssh", "-o", "ForwardAgent=no", "-t", host, "tmux", "attach"]


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
    ssh = runner.run(
        ["ssh", "-o", "ForwardAgent=no", "-o", "BatchMode=yes", f"cao-{box.name}", "true"]
    )
    if ssh.returncode and "publickey" in f"{ssh.stdout}\n{ssh.stderr}".lower():
        public_path = home / ".ssh" / f"cao-{box.name}_ed25519.pub"
        if not public_path.exists():
            tell(f"FIX  ssh key: public key {public_path.name} is missing")
            return
        public_key = parse_public_key(public_path.read_text(encoding="utf-8"))
        tell(f"BOX_SSH_PUBLIC_KEY='{public_key}'")
        action = f"Install SSH public key for {box.user} through SSM Run Command"
        tell(action)
        if not ask(action):
            return
        try:
            install_public_key(box, runner, home)
        except ConnectError as exc:
            tell(f"FIX  ssh key: {exc}")
            return
        checks = check_box(box, runner, home)
        for check in checks:
            if check.name == "ssh":
                tell(check.rendered())
                if check.status == "FIX":
                    return
    if no_attach:
        for line in ready_lines(box):
            tell(line)
        return
    runner.run_interactive(attach_argv(box))
