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


class Runner:
    """Run argv-only commands with no stdin or ambient credential environment."""

    def run(self, argv: Sequence[str], timeout: float = 15.0) -> RunResult:
        env = {"PATH": os.defpath, "LANG": "C"}
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
