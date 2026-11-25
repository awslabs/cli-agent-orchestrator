"""Read-only Remote CAO laptop and box preflight checks."""

from __future__ import annotations

import configparser
import os
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
    output = result.stdout.strip() or result.stderr.strip()
    return output.splitlines()[0] if output else ""


def _home_path(value: str, home: Path) -> Path:
    return home / value[2:] if value.startswith("~/") else Path(value)


def _has_access_key(profile: str, home: Path) -> tuple[bool, Path | None]:
    aws_dir = home / ".aws"
    credentials_env = os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "").strip()
    config_env = os.environ.get("AWS_CONFIG_FILE", "").strip()
    filenames = (
        _home_path(credentials_env, home) if credentials_env else aws_dir / "credentials",
        _home_path(config_env, home) if config_env else aws_dir / "config",
    )
    for filename in filenames:
        parser = configparser.RawConfigParser()
        try:
            parser.read(filename, encoding="utf-8")
        except OSError:
            continue
        except (configparser.Error, UnicodeDecodeError):
            return False, filename
        sections = (profile, f"profile {profile}")
        if any(parser.has_option(section, "aws_access_key_id") for section in sections):
            return True, None
    return False, None


def _ssh_options(result: RunResult) -> dict[str, list[str]]:
    options: dict[str, list[str]] = {}
    if not _ok(result):
        return options
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(" ")
        if separator:
            options.setdefault(key.lower(), []).append(value)
    return options


def _identity_path(value: str, home: Path) -> Path:
    return _home_path(value, home)


def _tmux_supported(result: RunResult) -> bool:
    match = re.fullmatch(r"tmux (?:next-)?(\d+)\.(\d+)[a-z]*", _version(result))
    return _ok(result) and match is not None and tuple(map(int, match.groups())) >= (3, 3)


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
    ssh = runner.run(["ssh", "-o", "ForwardAgent=no", "-V"])
    checks.append(
        Check("OK" if _ok(ssh) else "FIX", "ssh", "installed" if _ok(ssh) else "install OpenSSH")
    )
    laptop_herdr_version = ""
    if box.mode == "herdr":
        laptop_herdr = runner.run(["herdr", "--version"])
        laptop_herdr_version = _version(laptop_herdr) if _ok(laptop_herdr) else ""
        checks.append(
            Check(
                "OK" if laptop_herdr_version else "FIX",
                "herdr",
                laptop_herdr_version or "install herdr on the laptop",
            )
        )
    profile_listing = runner.run(["aws", "configure", "list-profiles"])
    exists = _ok(profile_listing) and box.aws_profile in profile_listing.stdout.splitlines()
    checks.append(
        Check(
            "OK" if exists else "FIX",
            "aws-profile",
            box.aws_profile if exists else f"run aws configure sso --profile {box.aws_profile}",
        )
    )
    has_key, invalid_aws_file = _has_access_key(box.aws_profile, home)
    checks.append(
        Check(
            "FIX" if has_key or invalid_aws_file else "OK",
            "aws-credentials",
            (
                f"repair {invalid_aws_file}"
                if invalid_aws_file
                else "has aws_access_key_id" if has_key else "no long-lived keys"
            ),
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
    ssh_options = _ssh_options(
        runner.run(["ssh", "-o", "ForwardAgent=no", "-G", f"cao-{box.name}"])
    )
    identity_files = [_identity_path(value, home) for value in ssh_options.get("identityfile", [])]
    key_exists = any(path.exists() for path in identity_files)
    checks.append(
        Check(
            "OK" if key_exists else "FIX",
            "ssh-key",
            "present" if key_exists else "add an IdentityFile for the box",
        )
    )
    configured = ssh_options.get("hostname") == [box.instance_id]
    checks.append(
        Check(
            "OK" if configured else "FIX",
            "ssh-config",
            "resolves to the box instance" if configured else f"configure Host cao-{box.name}",
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
    remote = runner.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ForwardAgent=no", f"cao-{box.name}", "true"]
    )
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
        # herdr is in the box user's ~/.local/bin, which only a login shell puts on PATH.
        remote_herdr = runner.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ForwardAgent=no",
                f"cao-{box.name}",
                "bash -lc 'herdr --version'",
            ]
        )
        matching = (
            _ok(remote_herdr)
            and bool(laptop_herdr_version)
            and _version(remote_herdr) == laptop_herdr_version
        )
        checks.append(
            Check(
                "OK" if matching else "FIX",
                "herdr-version",
                "matches laptop" if matching else "upgrade both sides to one version",
            )
        )
    else:
        tmux = runner.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ForwardAgent=no", f"cao-{box.name}", "tmux", "-V"]
        )
        supported = _tmux_supported(tmux)
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
            "-o",
            "ForwardAgent=no",
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
