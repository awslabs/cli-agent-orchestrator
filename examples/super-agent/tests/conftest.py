"""Shared helpers for the super-agent kit tests.

SUPER_AGENT_KIT points the tests at a kit root (default: this kit). The mutation
driver sets it to a mutant copy. Every script runs as a subprocess with a small
allowlisted environment, so nothing from the caller's shell leaks in.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(os.environ.get("SUPER_AGENT_KIT", Path(__file__).resolve().parent.parent))
BIN = KIT / "bin"
TESTS = Path(__file__).resolve().parent


def base_env(tmp_path, **extra):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "SUPER_AGENT_STATE_DIR": str(tmp_path / "state"),
    }
    env.update({k: str(v) for k, v in extra.items()})
    return env


def run(script, *args, env):
    return subprocess.run(
        [sys.executable, "-I", str(BIN / script), *map(str, args)],
        capture_output=True, text=True, env=env, timeout=60,
    )


CONDUCTOR_TERMINAL = "c0ffee01"


def ledger(cmd, *args, env, role="conductor", terminal=CONDUCTOR_TERMINAL):
    """go_ledger.py as the conductor runs it: --writer-role conductor, its CAO_TERMINAL_ID."""
    env = dict(env)
    if terminal is not None:
        env["CAO_TERMINAL_ID"] = terminal
    return run("go_ledger.py", cmd, *(["--writer-role", role] if role is not None else []), *args, env=env)


def write_json(path, data):
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def kit():
    return KIT
