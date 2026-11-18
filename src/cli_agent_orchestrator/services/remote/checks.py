"""Read-only Remote CAO laptop and box preflight checks."""

from __future__ import annotations

import configparser
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .registry import RemoteBox
from .runner import Runner, RunResult


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str

    def rendered(self) -> str:
        return f"{self.status:<4} {self.name}: {self.detail}"


def _ok(result: RunResult) -> bool:
    return result.returncode == 0


def _version(result: RunResult) -> str:
    return (
        (result.stdout or result.stderr).strip().splitlines()[0]
        if (result.stdout or result.stderr)
        else ""
    )


def _has_access_key(profile: str, aws_dir: Path) -> bool:
    parser = configparser.RawConfigParser()
    for filename in (aws_dir / "credentials", aws_dir / "config"):
        try:
            parser.read(filename, encoding="utf-8")
        except OSError:
            continue
    sections = (profile, f"profile {profile}")
    return any(parser.has_option(section, "aws_access_key_id") for section in sections)


def check_box(
    box: RemoteBox, runner: Runner | None = None, home: Path | None = None
) -> list[Check]:
    """Check prerequisites without changing local or remote state."""
    runner = runner or Runner()
    home = home or Path.home()
    checks: list[Check] = []
    aws = runner.run(["aws", "--version"])
    checks.append(
        Check(
            "OK" if _ok(aws) and "aws-cli/2." in _version(aws) else "FIX",
            "aws-cli",
            "2.x" if _ok(aws) and "aws-cli/2." in _version(aws) else "install AWS CLI v2",
        )
    )
    plugin = runner.run(["session-manager-plugin", "--version"])
    checks.append(
        Check(
            "OK" if _ok(plugin) else "FIX",
            "session-manager-plugin",
            "installed" if _ok(plugin) else "install the Session Manager plugin",
        )
    )
    ssh = runner.run(["ssh", "-V"])
    checks.append(
        Check("OK" if _ok(ssh) else "FIX", "ssh", "installed" if _ok(ssh) else "install OpenSSH")
    )
    laptop_herdr = RunResult(1)
    if box.mode == "herdr":
        laptop_herdr = runner.run(["herdr", "--version"])
        checks.append(
            Check(
                "OK" if _ok(laptop_herdr) else "FIX",
                "herdr",
                _version(laptop_herdr) if _ok(laptop_herdr) else "install herdr on the laptop",
            )
        )
    profile_listing = runner.run(["aws", "configure", "list-profiles"])
    exists = box.aws_profile in profile_listing.stdout.splitlines()
    checks.append(
        Check(
            "OK" if exists else "FIX",
            "aws-profile",
            box.aws_profile if exists else f"run aws configure sso --profile {box.aws_profile}",
        )
    )
    has_key = _has_access_key(box.aws_profile, home / ".aws")
    checks.append(
        Check(
            "FIX" if has_key else "OK",
            "aws-credentials",
            "has aws_access_key_id" if has_key else "no long-lived keys",
        )
    )
    login = runner.run(["aws", "sts", "get-caller-identity", "--profile", box.aws_profile])
    checks.append(
        Check(
            "OK" if _ok(login) else "FIX",
            "aws-login",
            "valid" if _ok(login) else f"run aws sso login --profile {box.aws_profile}",
        )
    )
    instance = runner.run(
        [
            "aws",
            "ec2",
            "describe-instances",
            "--profile",
            box.aws_profile,
            "--region",
            box.region,
            "--instance-ids",
            box.instance_id,
            "--query",
            "Reservations[].Instances[].State.Name",
            "--output",
            "text",
        ]
    )
    running = _ok(instance) and "running" in instance.stdout
    checks.append(
        Check(
            "OK" if running else "FIX",
            "instance",
            "running" if running else "instance must be running",
        )
    )
    ssm = runner.run(
        [
            "aws",
            "ssm",
            "describe-instance-information",
            "--profile",
            box.aws_profile,
            "--region",
            box.region,
            "--filters",
            f"Key=InstanceIds,Values={box.instance_id}",
            "--query",
            "InstanceInformationList[].PingStatus",
            "--output",
            "text",
        ]
    )
    online = _ok(ssm) and "Online" in ssm.stdout
    checks.append(
        Check("OK" if online else "FIX", "ssm", "Online" if online else "SSM must be Online")
    )
    key = home / ".ssh" / f"cao-{box.name}_ed25519"
    config = home / ".ssh" / "config"
    key_exists = key.exists()
    checks.append(
        Check(
            "OK" if key_exists else "FIX",
            "ssh-key",
            "present" if key_exists else f"create {key.name}",
        )
    )
    try:
        configured = (
            re.search(
                rf"(?m)^Host\s+cao-{re.escape(box.name)}(?:\s|$)",
                config.read_text(encoding="utf-8"),
            )
            is not None
        )
    except OSError:
        configured = False
    checks.append(
        Check(
            "OK" if configured else "FIX",
            "ssh-config",
            "managed host exists" if configured else f"add Host cao-{box.name}",
        )
    )
    prerequisites = all(check.status == "OK" for check in checks)
    if not prerequisites:
        checks.extend(
            [
                Check("SKIP", "ssh", "prerequisites need fixes"),
                Check("SKIP", "cao-server /health", "needs SSH first"),
            ]
        )
        return checks
    remote = runner.run(["ssh", "-o", "BatchMode=yes", f"cao-{box.name}", "true"])
    checks.append(
        Check(
            "OK" if _ok(remote) else "FIX",
            "ssh",
            "connected" if _ok(remote) else "SSH connection failed",
        )
    )
    if not _ok(remote):
        checks.append(Check("SKIP", "cao-server /health", "needs SSH first"))
        return checks
    if box.mode == "herdr":
        remote_herdr = runner.run(
            ["ssh", "-o", "BatchMode=yes", f"cao-{box.name}", "herdr", "--version"]
        )
        matching = _ok(remote_herdr) and _version(remote_herdr) == _version(laptop_herdr)
        checks.append(
            Check(
                "OK" if matching else "FIX",
                "herdr-version",
                "matches laptop" if matching else "upgrade both sides to one version",
            )
        )
    else:
        tmux = runner.run(["ssh", "-o", "BatchMode=yes", f"cao-{box.name}", "tmux", "-V"])
        supported = _ok(tmux) and re.search(r"tmux 3\.(?:[3-9]|\d{2,})", _version(tmux)) is not None
        checks.append(
            Check(
                "OK" if supported else "FIX",
                "tmux",
                "3.3 or later" if supported else "re-run box setup for tmux 3.3+",
            )
        )
    health = runner.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            f"cao-{box.name}",
            "curl",
            "-fsS",
            "http://127.0.0.1:9889/health",
        ]
    )
    checks.append(
        Check(
            "OK" if _ok(health) else "FIX",
            "cao-server /health",
            "healthy" if _ok(health) else "check cao-server.service on the box",
        )
    )
    return checks


def check_json(checks: list[Check]) -> list[dict[str, str]]:
    return [asdict(check) for check in checks]
