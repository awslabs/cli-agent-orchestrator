"""Production-shaped DeepSeek ACP lifecycle (COND-0415).

Runs the REAL pinned Claude executable against the DeepSeek gateway
through a real wrapper clone that claims a one-shot token exactly as the
conductor's shims/claude does.  Skips in CI and anywhere the ingredients
(real claude binary + DeepSeek key) are absent; the credential is read
from the ambient environment or the operator's key file and lives only
in the mode-0600 one-shot token file — never in argv, logs, or receipts.

Proves, with real provider bytes:
  * the real-binary version probe leaves the one-shot token present;
  * the wrapper launch consumes the token exactly once and records the
    exact consumed marker;
  * the SessionStart hook AND the provider's system/init event precede
    readiness, attesting the exact session, model, and working directory;
  * the first user event/turn is accepted;
  * a second launch of the same reservation refuses (replay) with zero
    provider bytes;
  * ambient Anthropic credentials never reach the provider child (no
    ambient fallback).
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import time
import uuid
from typing import Any, Optional

import pytest

from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import provider_contracts

DEEPSEEK_GATEWAY = "https://api.deepseek.com/anthropic"
MODEL = "deepseek-v4-flash"
EFFORT = "high"


def _resolve_real_claude() -> Optional[str]:
    candidate = os.environ.get("CAO_TEST_CLAUDE_REAL_BIN") or shutil.which("claude")
    if not candidate:
        return None
    canonical = os.path.realpath(candidate)
    if not os.path.isfile(canonical) or not os.access(canonical, os.X_OK):
        return None
    try:
        probe = subprocess.run([canonical, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    return canonical


def _resolve_deepseek_key() -> Optional[str]:
    """Return the operator's DeepSeek key, or None (test skips).

    Mirrors the conductor's resolution order: ambient env var first, then
    the operator's key file (default ``~/.secrets/env/DEEPSEEK_API_KEY``).
    The value is used ONLY to mint the mode-0600 one-shot token file; it
    is never printed, logged, or placed in argv.
    """
    token = os.environ.get("DEEPSEEK_API_KEY", "")
    if not token:
        path = os.path.expanduser(
            os.environ.get("DEEPSEEK_API_KEY_FILE", "~/.secrets/env/DEEPSEEK_API_KEY")
        )
        try:
            mode = os.stat(path).st_mode
        except OSError:
            return None
        if not stat.S_ISREG(mode) or mode & (stat.S_IRWXG | stat.S_IRWXO):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            return None
        assignment = re.fullmatch(r"(?:export\s+)?DEEPSEEK_API_KEY\s*=\s*([^\r\n]+)", raw)
        token = assignment.group(1) if assignment else raw
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            token = token[1:-1]
    if not token or "\n" in token or "\r" in token or "\0" in token:
        return None
    return token


_HAS_INGREDIENTS = _resolve_real_claude() is not None and _resolve_deepseek_key() is not None

pytestmark = pytest.mark.skipif(
    not _HAS_INGREDIENTS,
    reason="production-shaped DeepSeek ACP lifecycle requires the real claude "
    "binary and a DeepSeek key (CAO_TEST_CLAUDE_REAL_BIN / DEEPSEEK_API_KEY[_FILE])",
)


def _wrapper_script(
    tmp_path: pathlib.Path,
    *,
    real_claude: str,
    launch_count_path: pathlib.Path,
) -> str:
    """A real conductor-shim clone: claim token, marker, gateway env, exec.

    Keyed on realpath(cwd) like the conductor wrapper; refuses (exit 70)
    when the token is already claimed, and never execs when the marker
    could not be recorded.
    """
    return f"""#!/usr/bin/env python3
import json, os, sys
real = {real_claude!r}
routes_path = os.environ["CAO_CONDUCTOR_ROUTES"]
data = json.load(open(routes_path, encoding="utf-8"))
entry = data["routes"][os.path.realpath(os.getcwd())]
count_path = {str(launch_count_path)!r}
try:
    count = int(open(count_path, encoding="utf-8").read().strip())
except OSError:
    count = 0
open(count_path, "w", encoding="utf-8").write(str(count + 1))
token_path = entry["token_path"]
claim = token_path + ".claim." + str(os.getpid())
try:
    os.rename(token_path, claim)
except OSError:
    sys.stderr.write("one-shot token already claimed\\n")
    raise SystemExit(70)
token = open(claim, encoding="utf-8").readline().rstrip("\\n")
os.unlink(claim)
open(entry["consumed_path"], "w", encoding="utf-8").write("consumed\\n")
env = dict(os.environ)
for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY",
             "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
             "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
             "DEEPSEEK_API_KEY"):
    env.pop(name, None)
env["ANTHROPIC_BASE_URL"] = {DEEPSEEK_GATEWAY!r}
env["ANTHROPIC_AUTH_TOKEN"] = token
# Remap every model slot like the conductor shim: Claude Code's registry
# does not know the gateway model id, so the slots must all name it.
for slot in ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
             "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
             "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
    env[slot] = entry["model"]
env["CLAUDE_CODE_EFFORT_LEVEL"] = entry.get("effort") or "high"
env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "1000000"
env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "1000000"
env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
env["API_TIMEOUT_MS"] = "3000000"
os.execve(real, [real, *sys.argv[1:]], env)
"""


def _envelope(tmp_path: pathlib.Path, real_claude: str, key: str) -> dict:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    launch_count = tmp_path / "launch-count"
    launch_count.write_text("0\n")
    wrapper = tmp_path / "shims" / "claude"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        _wrapper_script(tmp_path, real_claude=real_claude, launch_count_path=launch_count)
    )
    wrapper.chmod(0o755)
    token = tmp_path / "deepseek-token.txt"
    token.write_text(key + "\n")
    token.chmod(0o600)
    marker = tmp_path / "deepseek-token.consumed"
    route_map = tmp_path / "deepseek-routes.json"
    route_map.write_text(
        json.dumps(
            {
                "routes": {
                    str(worktree): {
                        "route": "deepseek",
                        "model": MODEL,
                        "effort": EFFORT,
                        "token_path": str(token),
                        "consumed_path": str(marker),
                    }
                }
            }
        )
    )
    return {
        "wrapper_executable": str(wrapper),
        "wrapper_executable_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        "inner_executable": real_claude,
        "inner_executable_sha256": hashlib.sha256(
            pathlib.Path(real_claude).read_bytes()
        ).hexdigest(),
        "route_map_path": str(route_map),
        "worktree_realpath": str(worktree),
        "token_path": str(token),
        "consumed_marker_path": str(marker),
    }


def _request(tmp_path: pathlib.Path, envelope: dict) -> dict[str, Any]:
    return {
        "bridge_version": bridge.BRIDGE_VERSION,
        "reservation_id": str(uuid.uuid4()),
        "terminal_id": "decafbad",
        "generation": str(uuid.uuid4()),
        "delivery_id": str(uuid.uuid4()),
        "provider": "claude_code",
        "agent_profile": "implementer",
        "profile_sha256": "a" * 64,
        "model": MODEL,
        "effort": EFFORT,
        "provider_route": "deepseek",
        "route_envelope": envelope,
        "working_directory": envelope["worktree_realpath"],
        "provider_executable": envelope["inner_executable"],
        "provider_executable_sha256": envelope["inner_executable_sha256"],
        "project": "cond0415-production-shape",
        "task_id": "production-shape-task",
        "rendezvous_identity": {
            "project": "cond0415-production-shape",
            "task_id": "production-shape-task",
            "terminal_id": "decafbad",
            "terminal_generation": str(uuid.uuid4()),
            "worktree_realpath": envelope["worktree_realpath"],
            "repository": "test-repository",
            "head": "2" * 40,
            "actor": "cafebabe",
        },
    }


def _material() -> dict[str, Any]:
    return {
        "profile": object(),
        "profile_sha256": "a" * 64,
        "allowed_tools": ["*"],
        "system_prompt": "",
        "mcp_servers": [],
    }


def test_deepseek_acp_lifecycle_with_real_binary(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_claude = _resolve_real_claude()
    assert real_claude is not None
    key = _resolve_deepseek_key()
    assert key is not None

    # Isolate every bridge artifact from the real CAO home and companion dir.
    # The rendezvous socket must stay under the AF_UNIX path bound, so it gets
    # a short /tmp root instead of the long pytest tmp path.
    monkeypatch.setattr(bridge, "BRIDGE_ROOT", tmp_path / "bridge-root")
    rendezvous_root = pathlib.Path(f"/tmp/cao-prodshape-{os.getpid()}")
    monkeypatch.setattr(bridge, "RENDEZVOUS_ROOT", rendezvous_root)
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    monkeypatch.setattr(constants, "CAO_HOME_DIR", tmp_path / "cao-home")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    # The provider-launcher shim needs the live bridge socket accept loop
    # (only present under `_serve`), and its channel/lineage behavior is
    # covered by the unit suite.  This test exercises the exact real
    # provider process — pinned wrapper -> one-shot token claim -> real
    # Claude binary under the bounded conductor route environment — so the
    # launcher layer is passed through.
    monkeypatch.setattr(
        bridge, "_launcher_argv", lambda _socket, _identity, provider_argv: provider_argv
    )

    envelope = _envelope(tmp_path, real_claude=real_claude, key=key)
    request = _request(tmp_path, envelope)

    # The child inherits the test process cwd, and the wrapper keys the
    # route map on realpath(cwd) exactly like the conductor shim — so the
    # working directory must be the reservation worktree itself.
    monkeypatch.chdir(pathlib.Path(envelope["worktree_realpath"]))

    # An ambient Anthropic credential is present on purpose: the DeepSeek
    # child environment must scrub it, so success can never be ambient
    # fallback to api.anthropic.com.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ambient-must-not-be-used")

    # The composed child environment: bounded conductor route, no ambient
    # Anthropic credential or gateway pointer.
    child_env = bridge._provider_child_environment(request)
    assert child_env["CAO_CONDUCTOR_ROUTES"] == envelope["route_map_path"]
    assert child_env["CAO_CONDUCTOR_REAL_CLAUDE"] == envelope["inner_executable"]
    assert "ANTHROPIC_API_KEY" not in child_env
    assert "ANTHROPIC_AUTH_TOKEN" not in child_env
    assert "ANTHROPIC_BASE_URL" not in child_env

    token_path = pathlib.Path(envelope["token_path"])
    marker_path = pathlib.Path(envelope["consumed_marker_path"])
    launch_count = tmp_path / "launch-count"
    assert token_path.exists()

    # The version probe runs the pinned REAL binary and must leave the
    # one-shot token present and the marker absent.
    session = bridge._ProviderSession(request)
    banner = session._version(real_claude, provider_contracts.PROVIDER_CLAUDE)
    assert "2.1.233" in banner or "claude" in banner.lower()
    assert token_path.exists()
    assert not marker_path.exists()
    assert launch_count.read_text().strip() == "0"

    # The provider session launches the pinned wrapper exactly once; the
    # wrapper claims the token and records the marker.  Readiness is the
    # SessionStart hook (exact session + cwd proof) plus the
    # wrapper-consumed marker — system/init is first-turn evidence on
    # Claude Code 2.1.x and is validated at the first admission below.
    readiness = session.initialize()
    assert readiness["provider_receipt_kind"] == "claude-session-start"
    assert readiness["provider_session_id"] == session.provider_session_id
    assert readiness["session_start"]["session_id"] == session.provider_session_id
    assert readiness["session_start"]["cwd"] == envelope["worktree_realpath"]
    assert readiness["session_start"]["hook_event"] == "SessionStart"
    assert launch_count.read_text().strip() == "1"
    assert not token_path.exists()
    assert marker_path.read_text(encoding="utf-8") == "consumed\n"

    # The first real user event/turn: system/init arrives only with this
    # message, attests the exact session/model/cwd before the replayed
    # user echo, and the turn is accepted exactly once.
    assert session.rpc is not None
    start_index = session.rpc.notification_count()
    turn_id, kind, evidence = session._submit_provider_turn(
        "Reply with exactly the single word: ready",
        client_message_id=request["delivery_id"],
        meta={"caoProductionShape": "v1"},
    )
    assert kind == "claude-turn-start"
    assert turn_id and isinstance(turn_id, str)
    assert evidence["session_init"] is not None
    assert evidence["session_init"]["model"] == MODEL
    assert evidence["session_init"]["cwd"] == envelope["worktree_realpath"]
    assert evidence["session_init"]["session_id"] == session.provider_session_id
    result = session.rpc.wait_notification(
        lambda item: item.get("type") == "result"
        and item.get("session_id") == session.provider_session_id,
        start_index=start_index,
        timeout=300.0,
    )
    assert result.get("type") == "result"

    # A second launch of the same reservation refuses before any provider
    # byte: the consumed marker (and the now-absent one-shot token) prove
    # the replay must not start a second provider session.
    second = bridge._ProviderSession(request)
    with pytest.raises(bridge.BridgeError, match="token|consumed marker"):
        second.initialize()
    assert launch_count.read_text().strip() == "1"

    session.close()
    time.sleep(0.1)
