"""The EKS entrypoint keeps the runtime-channel token away from its setup steps (#745).

``cao init``, ``cao install`` and the provider warm-up run before the final
``exec``. With the token given as ``CAO_RUNTIME_TOKEN``, none of them may inherit
it; only the process the entrypoint finally execs (``cao-bridge`` or
``cao-server``) needs it. Every command is a stub that records its environment.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = (
    Path(__file__).resolve().parents[2] / "examples/cao-clusters/kubernetes/eks/entrypoint.sh"
)
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

# Records "<command> <args> TOKEN=<value>"; `claude` also answers the warm-up.
RECORDER = """#!/bin/sh
printf '%s %s TOKEN=%s\\n' "$(basename "$0")" "$*" "${CAO_RUNTIME_TOKEN-<unset>}" >> "$STUB_LOG"
[ "$(basename "$0")" = claude ] && echo ok
exit 0
"""
# Drops the duration and runs the command, as timeout(1) does.
TIMEOUT = """#!/bin/sh
shift
exec "$@"
"""


def _run(tmp_path, **env):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name in ("cao", "claude", "cao-bridge", "cao-server"):
        (stubs / name).write_text(RECORDER)
        (stubs / name).chmod(0o755)
    (stubs / "timeout").write_text(TIMEOUT)
    (stubs / "timeout").chmod(0o755)
    log = tmp_path / "calls.log"
    base = {
        "PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "CAO_HOME_DIR": str(tmp_path / "state"),
        "STUB_LOG": str(log),
        "CAO_INSTALL_PROFILES": "developer:claude_code",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_MODEL": "model-a",
        "CAO_WARM_PROVIDER": "1",
    }
    done = subprocess.run(
        [BASH, str(ENTRYPOINT)],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    calls = {}
    for line in log.read_text().splitlines():
        command, _, token = line.rpartition(" TOKEN=")
        # Keyed by the command and its first argument: "cao init", "claude -p", ...
        calls[" ".join(command.split()[:2])] = token
    return calls


@pytest.mark.parametrize("mode,final", [("bridge", "cao-bridge"), ("server", "cao-server --host")])
def test_only_the_final_process_receives_the_token(tmp_path, mode, final):
    calls = _run(tmp_path, CAO_NODE_MODE=mode, CAO_RUNTIME_TOKEN="s3cret")
    assert calls.pop(final) == "s3cret"
    assert set(calls) == {"cao init", "cao install", "claude -p"}
    assert set(calls.values()) == {"<unset>"}, calls


def test_without_a_token_none_is_invented(tmp_path):
    calls = _run(tmp_path, CAO_NODE_MODE="bridge")
    assert calls["cao-bridge"] == "<unset>"
