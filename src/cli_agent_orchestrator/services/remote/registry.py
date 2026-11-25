"""The small, local registry used by ``cao remote``."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cli_agent_orchestrator.constants import CAO_HOME_DIR

NAME_RE = re.compile(r"^[a-z0-9-]{1,32}$")
INSTANCE_RE = re.compile(r"^i-[0-9a-f]{8,17}$")
REGION_RE = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d+$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class RemoteConfigError(ValueError):
    """A registry entry is absent or invalid."""


@dataclass(frozen=True)
class RemoteBox:
    name: str
    instance_id: str
    region: str
    aws_profile: str
    mode: str = "herdr"
    user: str = ""

    def __post_init__(self) -> None:
        if not NAME_RE.fullmatch(self.name):
            raise RemoteConfigError("box must match [a-z0-9-]{1,32}")
        if not INSTANCE_RE.fullmatch(self.instance_id):
            raise RemoteConfigError("instance id must look like i-0123456789abcdef")
        if not REGION_RE.fullmatch(self.region):
            raise RemoteConfigError("region must look like us-east-1")
        if not self.aws_profile:
            raise RemoteConfigError("profile must not be empty")
        if any(character.isspace() or ord(character) < 32 for character in self.aws_profile):
            raise RemoteConfigError("profile must not contain control characters or whitespace")
        if self.mode not in {"herdr", "tmux"}:
            raise RemoteConfigError("mode must be herdr or tmux")
        if not self.user:
            object.__setattr__(self, "user", f"cao-{self.mode}")
        elif not USER_RE.fullmatch(self.user):
            raise RemoteConfigError("user must match [a-z_][a-z0-9_-]{0,31}")


def registry_dir() -> Path:
    """Return a CAO-owned path, alongside settings rather than a hard-coded home."""
    return CAO_HOME_DIR / "remote"


def path_for(name: str) -> Path:
    if not NAME_RE.fullmatch(name):
        raise RemoteConfigError("box must match [a-z0-9-]{1,32}")
    return registry_dir() / f"{name}.json"


def save(box: RemoteBox) -> Path:
    target = path_for(box.name)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (
        json.dumps(
            {
                "instance_id": box.instance_id,
                "mode": box.mode,
                "name": box.name,
                "profile": box.aws_profile,
                "region": box.region,
                "user": box.user,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{box.name}.", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def load(name: str) -> RemoteBox:
    target = path_for(name)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("a registration must be a JSON object")
        if any(not isinstance(value, str) for value in data.values()) or data.get("name") != name:
            raise ValueError("registration fields must be strings and name must match filename")
        data["aws_profile"] = data.pop("profile")
        return RemoteBox(**data)
    except FileNotFoundError as exc:
        raise RemoteConfigError(f"remote box '{name}' is not registered") from exc
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise RemoteConfigError(f"remote box '{name}' has invalid configuration") from exc


def list_boxes() -> list[RemoteBox]:
    directory = registry_dir()
    if not directory.exists():
        return []
    boxes: list[RemoteBox] = []
    for entry in sorted(directory.glob("*.json")):
        try:
            boxes.append(load(entry.stem))
        except RemoteConfigError:
            continue
    return boxes


def remove(name: str) -> None:
    try:
        path_for(name).unlink()
    except FileNotFoundError as exc:
        raise RemoteConfigError(f"remote box '{name}' is not registered") from exc
    except (IsADirectoryError, NotADirectoryError) as exc:
        raise RemoteConfigError(f"remote box '{name}' has invalid configuration") from exc
    except OSError as exc:
        raise RemoteConfigError(
            f"could not remove remote box '{name}': {exc.strerror or str(exc)}"
        ) from exc
