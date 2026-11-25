"""Narrow subprocess seam for Remote CAO checks."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


# Every AWS call names its profile, so ambient credentials must never answer for it.
_AMBIENT_AWS_CREDENTIALS = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_DEFAULT_PROFILE",
        "AWS_PROFILE",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    }
)


class Runner:
    """Run argv-only commands with no stdin and no ambient AWS credentials."""

    def run(self, argv: Sequence[str], timeout: float = 15.0) -> RunResult:
        # Keep PATH, SSH_AUTH_SOCK, proxy and CA settings: the user's tools live outside os.defpath.
        env = {
            key: value for key, value in os.environ.items() if key not in _AMBIENT_AWS_CREDENTIALS
        }
        env.update(LANG="C", LC_ALL="C")
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return RunResult(1, stderr=type(exc).__name__)
        return RunResult(completed.returncode, completed.stdout, completed.stderr)

    def run_interactive(self, argv: Sequence[str]) -> RunResult:
        """Run an explicitly approved command on the caller's terminal."""
        try:
            completed = subprocess.run(list(argv), check=False)
        except OSError as exc:
            return RunResult(1, stderr=type(exc).__name__)
        return RunResult(completed.returncode)
