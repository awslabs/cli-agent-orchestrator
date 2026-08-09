"""Provider-native session bridge for one managed terminal generation.

The bridge runs inside the reserved terminal pane and owns the exact Codex
app-server thread or Kimi ACP session used for both readiness and task
admission. CAO talks to it over a generation-private Unix socket. Receipt IDs
come from the provider (Codex thread/turn IDs or Kimi session/update message
IDs); tmux paste success, pane text, and locally generated UUIDs are never
treated as provider acceptance.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import hashlib
import json
import logging
import os
import pathlib
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from cli_agent_orchestrator.constants import CAO_HOME_DIR, SECURITY_PROMPT
from cli_agent_orchestrator.providers.codex import (
    _toml_override,
    _toml_scalar,
    _validate_config_key,
    render_trusted_project_override,
)
from cli_agent_orchestrator.services import (
    actor_broker,
    companion_receipts,
    heartbeat_store,
    provider_contracts,
)
from cli_agent_orchestrator.services.codex_trust import _contains_session_flags
from cli_agent_orchestrator.services.kimi_route import _current_option
from cli_agent_orchestrator.services.managed_event_renderer import ManagedEventRenderer
from cli_agent_orchestrator.services.managed_session_control import ACCEPTED as CONTROL_ACCEPTED
from cli_agent_orchestrator.services.managed_session_control import AMBIGUOUS as CONTROL_AMBIGUOUS
from cli_agent_orchestrator.services.managed_session_control import COMPLETED as CONTROL_COMPLETED
from cli_agent_orchestrator.services.managed_session_control import QUEUED as CONTROL_QUEUED
from cli_agent_orchestrator.services.managed_session_control import REFUSED as CONTROL_REFUSED
from cli_agent_orchestrator.services.managed_session_control import SUBMITTED as CONTROL_SUBMITTED
from cli_agent_orchestrator.services.managed_session_control import (
    SessionControlJournal,
)
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.skills import build_skill_catalog
from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

logger = logging.getLogger(__name__)

BRIDGE_VERSION = "cao-native-provider-bridge-v1"
BRIDGE_ROOT = CAO_HOME_DIR / "managed-provider-sessions"
RENDEZVOUS_ROOT = pathlib.Path("/tmp") / f"cao-managed-bridge-{os.getuid()}"
RENDEZVOUS_SCHEMA = "cao-managed-bridge-rendezvous-v1"
RENDEZVOUS_DIGEST_DOMAIN = "cao-managed-bridge-rendezvous-v1"
RENDEZVOUS_IDENTITY_FIELDS = (
    "project",
    "task_id",
    "terminal_id",
    "terminal_generation",
    "worktree_realpath",
    "repository",
    "head",
    "actor",
)
_RENDEZVOUS_DIGEST_WIDTH = 16
_AF_UNIX_SAFE_PATH_BYTES = 100

# Provider and bridge child processes run under a minimal provider-bound
# environment built fresh, never the ambient server/tmux environment. System
# and conductor contributions are kept separate from the target provider
# namespace: foreign provider controls are scrubbed before the fail-closed
# guard, while target-provider controls are held only for that provider's
# child process.
_PROVIDER_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TERM_PROGRAM",
        "COLORTERM",
        "TMPDIR",
        "SSH_AUTH_SOCK",
        "DISPLAY",
        "XDG_RUNTIME_DIR",
        "DO_NOT_TRACK",
    }
)
_MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
_CONDUCTOR_ENV_EXACT = frozenset({"CAO_TERMINAL_ID", "ZDOTDIR"})
_CONDUCTOR_ENV_PREFIXES = ("CAO_CONDUCTOR_", "CAO_WORKFLOW_")
_PROVIDER_CONTROL_PREFIXES = {
    "codex": ("CODEX_",),
    "kimi_cli": ("KIMI_",),
    "claude_code": ("CLAUDE_", "ANTHROPIC_"),
}
_BRIDGE_ENV_INVENTORY_SCHEMA = "cao-managed-bridge-environment-v1"
LAUNCH_FAILURE_SCHEMA = "cao-managed-bridge-launch-failure-v1"
LAUNCH_FAILURE_DIGEST_DOMAIN = "cao-managed-bridge-launch-failure-evidence-v1"
_BOUND_PROVIDER_ENV: Optional[dict[str, str]] = None

# Variables that must never steer a managed provider from the ambient
# environment: quota bypass, conductor control, and route control. Route
# identity (model/effort/config home) comes ONLY from the reservation request.
_PROTECTED_ENV_EXACT = frozenset(
    {
        "CAO_SETUP_SKIP_QUOTA_PREFLIGHT",
        "CONDUCT_SKIP_QUOTA_PREFLIGHT",
        "KIMI_MODEL_THINKING_EFFORT",
    }
)
_PROTECTED_ENV_PREFIXES = ("CONDUCT_", "CHECK_AI_QUOTA", "CODEX_")


def _assert_bridge_environment() -> None:
    """Fail closed when the ambient environment carries protected control
    variables into the managed bridge."""
    leaked = sorted(
        name
        for name in os.environ
        if name in _PROTECTED_ENV_EXACT
        or any(name.startswith(prefix) for prefix in _PROTECTED_ENV_PREFIXES)
    )
    if leaked:
        raise BridgeError(
            "protected control variables leak into the managed bridge "
            "environment: " + ", ".join(leaked)
        )


def _environment_inventory(provider: str, names: list[str]) -> dict[str, Any]:
    """Return auditable names-only environment metadata.

    Values may include credentials or provider-specific configuration and
    must never enter the launch journal.  The digest is over the canonical
    provider-bound name inventory only.
    """
    payload = {
        "schema": _BRIDGE_ENV_INVENTORY_SCHEMA,
        "provider": provider,
        "names": sorted(names),
    }
    return {**payload, "names_sha256": _digest(payload)}


def _provider_bound_environments(
    provider: str,
    ambient: Optional[dict[str, str]] = None,
) -> tuple[dict[str, str], dict[str, str], dict[str, Any]]:
    """Compose bridge/provider environments from pinned provider-bound rules.

    The bridge receives the system base and non-secret conductor routing
    contributions.  The provider child additionally receives only its own
    control namespace.  Foreign provider namespaces never cross this seam.
    Ambient route controls still do not override the reservation-pinned route.
    """
    if provider not in _PROVIDER_CONTROL_PREFIXES:
        raise BridgeError(f"unsupported managed provider {provider!r}")
    source = dict(os.environ if ambient is None else ambient)
    bridge_env = {name: source[name] for name in _PROVIDER_ENV_ALLOWLIST if name in source}
    # PATH stays bounded.  Preserve only the conductor-declared shim prefix;
    # the remainder is the fixed system base, never arbitrary ambient PATH.
    shim_dir = source.get("CAO_CONDUCTOR_SHIM_DIR")
    if shim_dir and os.path.isabs(shim_dir):
        bridge_env["PATH"] = f"{shim_dir}:{_MINIMAL_PATH}"
    else:
        bridge_env["PATH"] = _MINIMAL_PATH
    for name, value in source.items():
        if name in _CONDUCTOR_ENV_EXACT or any(
            name.startswith(prefix) for prefix in _CONDUCTOR_ENV_PREFIXES
        ):
            bridge_env[name] = value

    provider_env = dict(bridge_env)
    for name, value in source.items():
        if any(name.startswith(prefix) for prefix in _PROVIDER_CONTROL_PREFIXES[provider]):
            # Route control comes only from the immutable reservation and is
            # added explicitly by the provider adapter.
            if name not in _PROTECTED_ENV_EXACT:
                provider_env[name] = value
    inventory = _environment_inventory(provider, list(provider_env))
    return bridge_env, provider_env, inventory


def _prune_bridge_environment(provider: str) -> dict[str, Any]:
    """Scrub the bridge before the unchanged fail-closed guard runs."""
    global _BOUND_PROVIDER_ENV
    bridge_env, provider_env, inventory = _provider_bound_environments(provider)
    _BOUND_PROVIDER_ENV = provider_env
    os.environ.clear()
    os.environ.update(bridge_env)
    return inventory


def _provider_env(overrides: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The provider-bound environment for provider child processes."""
    if _BOUND_PROVIDER_ENV is None:
        env = {name: os.environ[name] for name in _PROVIDER_ENV_ALLOWLIST if name in os.environ}
        env["PATH"] = _MINIMAL_PATH
    else:
        env = dict(_BOUND_PROVIDER_ENV)
    env.update(overrides or {})
    return env


def _provider_route_environment(request: dict[str, Any]) -> dict[str, str]:
    """Return route controls pinned by the immutable launch request.

    This is the one place a Kimi effort override becomes a real environment
    variable, for both the ACP bridge child and the native TUI child, so it
    is the one place the provider-default sentinel has to be honored. A gate
    placed inside a particular probe instead would leave every other path a
    trap: the first launch that took one would silently reinstate the
    override, and it would surface as a provider protocol error nowhere near
    its cause.

    It is likewise the one place the Kimi updater kill-switch
    (``provider_contracts.kimi_update_suppression_env``) is pinned for every
    managed Kimi child — the ACP bridge child, the preflight ``--version``
    probe, the session bootstrap, and the resumed native TUI all compose
    their environment through this seam, and it applies *after* the ambient
    passthrough, so an operator-supplied conflicting value cannot re-enable
    the provider's background self-updater inside a managed child process
    (cond-0315).  The fence is strictly per-process: it keeps the selected
    child self-identical until it exits and says nothing about the
    operator's PATH installation, which stays free to update on its own
    schedule.
    """
    if request["provider"] == "kimi_cli":
        effort = request.get("effort")
        if not isinstance(effort, str) or not effort:
            raise BridgeError("Kimi managed launch requires a pinned effort")
        # A route that selects no effort contributes no variable at all —
        # not the sentinel, and not a substituted default. The provider
        # then applies its own, which is the only value anyone here has
        # grounds to run under.
        return {
            **provider_contracts.kimi_update_suppression_env(),
            **provider_contracts.kimi_effort_env(effort),
        }
    return {}


def _provider_child_environment(
    request: dict[str, Any], *, session_env: Optional[dict[str, str]] = None
) -> dict[str, str]:
    """Compose the exact environment passed to the provider child."""
    if request.get("provider_route", "anthropic") == "glm":
        from cli_agent_orchestrator.services import glm_native_launch

        envelope = request.get("route_envelope") or {}
        if session_env is None:
            raise BridgeError("GLM native environment requires the stored session env")
        try:
            verified = glm_native_launch.validate_session_env(
                session_env=session_env,
                envelope=envelope,
                expected_model=request["model"],
            )
        except glm_native_launch.GlmRouteError as exc:
            raise BridgeError(str(exc)) from exc
        # The native GLM child must see the conductor shim and only its
        # non-secret routing names.  The wrapper claims the token inside the
        # provider process; no credential value is copied into this map.
        env = {name: verified[name] for name in _PROVIDER_ENV_ALLOWLIST if name in verified}
        shim_dir = verified["CAO_CONDUCTOR_SHIM_DIR"]
        env["PATH"] = f"{shim_dir}:{_MINIMAL_PATH}"
        for name, value in verified.items():
            if name.startswith("CAO_CONDUCTOR_") or name == "ZDOTDIR":
                env[name] = value
        return env
    return _provider_env(_provider_route_environment(request))


def native_child_environment(
    request: dict[str, Any], *, session_env: Optional[dict[str, str]] = None
) -> dict[str, str]:
    """The provider child environment for a native-TUI launch.

    Deliberately the *same* composition the ACP bridge gives its own
    provider child.  A native worker that inherited a different (or
    ambient) environment could resolve a different route, credential, or
    config file than the mode it is replacing, and the difference would
    only show up as inexplicably different behaviour between two modes
    that are supposed to be interchangeable.
    """
    return _provider_child_environment(request, session_env=session_env)


def provider_version_banner(
    request: dict[str, Any],
    *,
    timeout: Optional[float] = None,
    environment: Optional[dict[str, str]] = None,
) -> str:
    """Read the installed provider's ``--version`` in the child environment.

    Run in the child environment rather than the caller's, because the
    version that matters is the one the worker will actually run — a
    version probe under different config or PATH can report a binary
    other than the one about to start.

    ``timeout=None`` observes under the provider's contract deadline
    (``provider_contracts.version_probe_timeout``): finite, and wide
    enough for the provider's real cold start — the fixed 5 s bound that
    used to be the default failed a healthy pinned Kimi binary under
    startup load (cond-0313).
    """
    executable = request["provider_executable"]
    if not os.path.isabs(executable) or os.path.realpath(executable) != executable:
        raise BridgeError("provider executable must be a canonical absolute path")
    if timeout is None:
        timeout = provider_contracts.version_probe_timeout(request["provider"])
    proc = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment or _provider_child_environment(request),
    )
    if proc.returncode != 0:
        raise BridgeError(f"provider --version exited {proc.returncode}")
    return (proc.stdout or proc.stderr or "").strip()


def publish_native_ready_state(reservation_id: str, readiness: dict[str, Any]) -> None:
    """Publish the durable ready state for a native-TUI generation.

    The native TUI owns its own pane and runs no bridge, so nothing else
    would ever write this file — yet ``bind_native`` reads readiness from
    exactly here regardless of mode.  Writing the same durable record
    keeps bind mode-blind: it validates whatever receipt it finds against
    the mode-specific allowlist, instead of growing a second source of
    truth that could disagree with the first.

    Written through the same atomic replace the bridge uses, so a crash
    mid-write leaves the previous state rather than a truncated one.
    """
    _atomic_json(
        paths(reservation_id)["state"],
        {
            "bridge_version": BRIDGE_VERSION,
            "state": "ready",
            "readiness": readiness,
            "published_at": _now(),
        },
    )


def _bind_bridge_environment(request: dict[str, Any]) -> dict[str, Any]:
    """Scrub the bridge and bind the final provider child environment."""
    global _BOUND_PROVIDER_ENV

    _prune_bridge_environment(request["provider"])
    provider_env = _provider_child_environment(request)
    _BOUND_PROVIDER_ENV = provider_env
    return _environment_inventory(request["provider"], list(provider_env))


def _launcher_argv(
    socket_path: pathlib.Path,
    binding_identity: dict[str, str],
    provider_argv: list[str],
) -> list[str]:
    """Wrap the provider argv with the provider-originated launcher shim.

    The launcher becomes the recorded provider process (the actor broker's
    provider-tree root) and spawns the real provider as its child, so
    actor-assertion issuance gains a kernel-verifiable provider-originated
    channel over the generation-private socket. The launcher proxies
    stdio byte-transparently, so the provider session is unchanged.
    """
    return [
        sys.executable,
        "-I",
        "-m",
        "cli_agent_orchestrator.services.provider_launcher",
        "--socket",
        str(socket_path),
        "--identity-json",
        _canonical(binding_identity).decode("utf-8"),
        "--",
        *provider_argv,
    ]


class BridgeError(RuntimeError):
    pass


class SubmitUncertain(BridgeError):
    """The provider-boundary outcome is unknowable.

    Raised when a failure occurs after the submission request may have
    crossed the provider boundary (e.g. response loss, timeout, or
    connection failure after the request was sent). The provider may have
    accepted the turn; callers MUST durably record ``submit-ambiguous``
    evidence rather than asserting either submission or non-submission.
    """


class SessionOperationRefused(BridgeError):
    """A typed managed-session control refusal made before provider I/O."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _git_launch_identity(working_directory: str) -> tuple[str, str]:
    """Resolve the repository and exact head used by the launch tuple."""
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                working_directory,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError(f"worktree repository identity is unreadable: {exc}") from exc
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) != 2:
        raise BridgeError("worktree repository identity is unreadable")
    common_dir, head = lines
    if not re.fullmatch(r"[a-f0-9]{40}", head):
        raise BridgeError("worktree head is not a full lowercase hex OID")
    common_path = pathlib.Path(common_dir)
    repository_root = common_path.parent if common_path.name == ".git" else common_path
    repository = repository_root.name
    if not repository:
        raise BridgeError("worktree repository name is empty")
    return repository, head


def launch_binding_identity(
    *,
    project: str,
    task_id: str,
    terminal_id: str,
    terminal_generation: str,
    working_directory: str,
    actor: str,
) -> dict[str, str]:
    """Build the complete canonical tuple before any provider effect."""
    worktree = os.path.realpath(working_directory)
    if worktree != working_directory or not os.path.isdir(worktree):
        raise BridgeError("worktree_realpath must be an existing canonical directory")
    repository, head = _git_launch_identity(worktree)
    identity = {
        "project": project,
        "task_id": task_id,
        "terminal_id": terminal_id,
        "terminal_generation": terminal_generation,
        "worktree_realpath": worktree,
        "repository": repository,
        "head": head,
        "actor": actor,
    }
    return _validate_binding_identity(identity)


def verify_launch_binding_identity(identity: dict[str, str]) -> None:
    """Re-prove the repository/worktree pin at a provider-effect boundary."""
    identity = _validate_binding_identity(identity)
    worktree = identity["worktree_realpath"]
    if not os.path.isdir(worktree) or os.path.realpath(worktree) != worktree:
        raise BridgeError("bridge rendezvous worktree identity drifted")
    repository, head = _git_launch_identity(worktree)
    if repository != identity["repository"] or head != identity["head"]:
        raise BridgeError("bridge rendezvous repository/head identity drifted")


def _validate_binding_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(RENDEZVOUS_IDENTITY_FIELDS):
        raise BridgeError("bridge rendezvous identity is incomplete or malformed")
    identity: dict[str, str] = {}
    invalid: list[str] = []
    for field in RENDEZVOUS_IDENTITY_FIELDS:
        item = value.get(field)
        if not isinstance(item, str) or not item:
            invalid.append(field)
        else:
            identity[field] = item
    if invalid:
        raise BridgeError(f"bridge rendezvous identity has empty fields: {invalid}")
    if os.path.realpath(identity["worktree_realpath"]) != identity["worktree_realpath"]:
        raise BridgeError("bridge rendezvous worktree identity is not canonical")
    if not re.fullmatch(r"[a-f0-9]{40}", identity["head"]):
        raise BridgeError("bridge rendezvous head is not a full lowercase hex OID")
    return identity


def binding_identity(request: dict[str, Any]) -> dict[str, str]:
    return _validate_binding_identity(request.get("rendezvous_identity"))


def _rendezvous_canonical_bytes(identity: dict[str, str]) -> bytes:
    """Normative §4.3 bytes: domain first, fixed tuple order, one newline."""
    identity = _validate_binding_identity(identity)
    value = {"domain": RENDEZVOUS_DIGEST_DOMAIN}
    value.update((field, identity[field]) for field in RENDEZVOUS_IDENTITY_FIELDS)
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _rendezvous_digest(identity: dict[str, str]) -> str:
    return hashlib.sha256(_rendezvous_canonical_bytes(identity)).hexdigest()


def _rendezvous_key(identity: dict[str, str]) -> str:
    return f"sk-{_rendezvous_digest(identity)[:_RENDEZVOUS_DIGEST_WIDTH]}"


def _secure_rendezvous_root() -> pathlib.Path:
    try:
        RENDEZVOUS_ROOT.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = RENDEZVOUS_ROOT.lstat()
    except OSError as exc:
        raise BridgeError(f"bridge rendezvous runtime directory is unavailable: {exc}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise BridgeError("bridge rendezvous runtime directory is not owner-only")
    return RENDEZVOUS_ROOT


def rendezvous_paths(identity: dict[str, str]) -> dict[str, pathlib.Path]:
    identity = _validate_binding_identity(identity)
    root = _secure_rendezvous_root()
    key = _rendezvous_key(identity)
    socket_path = root / f"{key}.sock"
    if len(os.fsencode(socket_path)) > _AF_UNIX_SAFE_PATH_BYTES:
        raise BridgeError("bridge rendezvous path exceeds the safe AF_UNIX bound")
    return {
        "rendezvous_root": root,
        "socket": socket_path,
        "binding": root / f"{key}.json",
    }


def paths(reservation_id: str, request: Optional[dict[str, Any]] = None) -> dict[str, pathlib.Path]:
    root = BRIDGE_ROOT / reservation_id
    target = {
        "root": root,
        "request": root / "request.json",
        "state": root / "state.json",
    }
    if request is None:
        try:
            request = json.loads(target["request"].read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            request = None
    if request is not None:
        target.update(rendezvous_paths(binding_identity(request)))
    return target


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _iter_provider_error_items(node: Any) -> list[dict[str, Any]]:
    """Provider-native error items inside an RPC notification (§20.2f P1-10).
    Only the provider's own structured error items qualify — never text
    pattern-matched out of ordinary output."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "error" and isinstance(node.get("message"), str):
            found.append(node)
        for value in node.values():
            found.extend(_iter_provider_error_items(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_iter_provider_error_items(value))
    return found


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.part")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass(frozen=True)
class RendezvousVerification:
    """Pinned sidecar + socket filesystem identity from one exact verification."""

    record: dict[str, Any]
    sidecar_identity: tuple[int, int, int, int, int, int, int]
    socket_identity: tuple[int, int, int, int, int, int, int]


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _socket_identity_record(info: os.stat_result) -> dict[str, int]:
    return {
        "st_dev": info.st_dev,
        "st_ino": info.st_ino,
        "st_mode": info.st_mode,
        "st_uid": info.st_uid,
        "st_size": info.st_size,
        "st_mtime_ns": info.st_mtime_ns,
        "st_ctime_ns": info.st_ctime_ns,
    }


def _validate_socket_identity(value: Any) -> Optional[dict[str, int]]:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    }:
        raise BridgeError("socket-binding-record-malformed")
    if any(not isinstance(item, int) or isinstance(item, bool) for item in value.values()):
        raise BridgeError("socket-binding-record-malformed")
    if (
        value["st_dev"] < 0
        or value["st_ino"] <= 0
        or value["st_size"] < 0
        or value["st_mtime_ns"] < 0
        or value["st_ctime_ns"] < 0
        or not stat.S_ISSOCK(value["st_mode"])
        or value["st_uid"] != os.getuid()
        or stat.S_IMODE(value["st_mode"]) != 0o600
    ):
        raise BridgeError("socket-binding-record-malformed")
    return dict(value)


def _binding_record(
    identity: dict[str, str],
    *,
    socket_identity: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    identity = _validate_binding_identity(identity)
    socket_identity = _validate_socket_identity(socket_identity)
    return {
        "schema": RENDEZVOUS_SCHEMA,
        "rendezvous_key": _rendezvous_key(identity),
        "binding_identity": identity,
        "binding_identity_sha256": _rendezvous_digest(identity),
        "socket_identity": socket_identity,
    }


def _read_binding_record_descriptor(descriptor: int) -> tuple[dict[str, Any], os.stat_result]:
    try:
        info = os.fstat(descriptor)
    except OSError as exc:
        raise BridgeError(f"socket-binding-record-malformed: {exc}") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise BridgeError("socket-binding-record-malformed")
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = bytearray()
        while True:
            block = os.read(descriptor, 65536)
            if not block:
                break
            raw.extend(block)
            if len(raw) > 1024 * 1024:
                raise BridgeError("socket-binding-record-malformed")
        record = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("socket-binding-record-malformed") from exc
    try:
        after = os.fstat(descriptor)
    except OSError as exc:
        raise BridgeError(f"socket-binding-record-malformed: {exc}") from exc
    if _file_identity(after) != _file_identity(info):
        raise BridgeError("socket-binding-record-replaced")
    if not isinstance(record, dict) or set(record) != {
        "schema",
        "rendezvous_key",
        "binding_identity",
        "binding_identity_sha256",
        "socket_identity",
    }:
        raise BridgeError("socket-binding-record-malformed")
    try:
        identity = _validate_binding_identity(record["binding_identity"])
        socket_identity = _validate_socket_identity(record["socket_identity"])
    except BridgeError as exc:
        raise BridgeError("socket-binding-record-malformed") from exc
    expected = _binding_record(identity, socket_identity=socket_identity)
    if record != expected:
        raise BridgeError("socket-binding-record-malformed")
    return record, after


def _open_binding_record(
    path: pathlib.Path, *, writable: bool = False
) -> tuple[int, dict[str, Any], os.stat_result]:
    flags = os.O_RDWR if writable else os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise BridgeError("socket-binding-record-absent") from None
    except OSError as exc:
        raise BridgeError(f"socket-binding-record-malformed: {exc}") from exc
    try:
        record, info = _read_binding_record_descriptor(descriptor)
        for _ in range(2):
            path_info = path.lstat()
            if _file_identity(path_info) != _file_identity(info):
                raise BridgeError("socket-binding-record-replaced")
        return descriptor, record, info
    except FileNotFoundError:
        os.close(descriptor)
        raise BridgeError("socket-binding-record-replaced") from None
    except Exception:
        os.close(descriptor)
        raise


def _read_binding_record(path: pathlib.Path) -> dict[str, Any]:
    descriptor, record, _ = _open_binding_record(path)
    os.close(descriptor)
    return record


def verify_rendezvous_binding(
    socket_path: pathlib.Path | str,
    expected_identity: dict[str, str],
    *,
    expected: Optional[RendezvousVerification] = None,
) -> RendezvousVerification:
    """Pin the full tuple and exact sidecar/socket inode before use."""
    expected_identity = _validate_binding_identity(expected_identity)
    socket_path = pathlib.Path(socket_path)
    try:
        root_info = socket_path.parent.lstat()
    except OSError as exc:
        raise BridgeError(f"bridge rendezvous runtime directory is unavailable: {exc}") from exc
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or stat.S_IMODE(root_info.st_mode) != 0o700
        or socket_path.name != f"{_rendezvous_key(expected_identity)}.sock"
        or len(os.fsencode(socket_path)) > _AF_UNIX_SAFE_PATH_BYTES
    ):
        raise BridgeError("socket-identity-collision")
    descriptor, record, sidecar_info = _open_binding_record(socket_path.with_suffix(".json"))
    try:
        if record["binding_identity"] != expected_identity:
            raise BridgeError("socket-identity-collision")
        recorded_socket = record["socket_identity"]
        if recorded_socket is None:
            raise BridgeError("socket-binding-not-ready")
        try:
            socket_info = socket_path.lstat()
        except FileNotFoundError:
            raise BridgeError("socket-binding-not-ready") from None
        if (
            _socket_identity_record(socket_info) != recorded_socket
            or not stat.S_ISSOCK(socket_info.st_mode)
            or socket_info.st_uid != os.getuid()
            or stat.S_IMODE(socket_info.st_mode) != 0o600
        ):
            raise BridgeError("socket-identity-collision")
        result = RendezvousVerification(
            record=record,
            sidecar_identity=_file_identity(sidecar_info),
            socket_identity=_stat_identity(socket_info),
        )
        if expected is not None and result != expected:
            raise BridgeError("socket-rendezvous-replaced")
        return result
    finally:
        os.close(descriptor)


def _fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_binding_record_descriptor(
    descriptor: int,
    identity: dict[str, str],
    *,
    socket_identity: Optional[dict[str, int]] = None,
) -> None:
    payload = _canonical(_binding_record(identity, socket_identity=socket_identity)) + b"\n"
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise BridgeError("socket binding record write made no progress")
        view = view[written:]
    os.fsync(descriptor)


def _create_binding_record(path: pathlib.Path, identity: dict[str, str]) -> None:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        record = _read_binding_record(path)
        if record["binding_identity"] == identity:
            raise BridgeError("duplicate-live-bridge-identity") from None
        raise BridgeError("socket-identity-collision") from None
    except OSError as exc:
        raise BridgeError(f"socket binding record creation failed: {exc}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        _write_binding_record_descriptor(descriptor, identity)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _acquire_binding_claim(path: pathlib.Path, identity: dict[str, str]) -> int:
    """Acquire/recover one process-pinned claim; a live claimant refuses."""
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise BridgeError(f"socket-binding-record-malformed: {exc}") from exc
    except OSError as exc:
        raise BridgeError(f"socket binding record creation failed: {exc}") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("duplicate-live-bridge-identity") from None
        if created:
            os.fchmod(descriptor, 0o600)
            _write_binding_record_descriptor(descriptor, identity)
            _fsync_directory(path.parent)
        else:
            record, descriptor_info = _read_binding_record_descriptor(descriptor)
            try:
                path_info = path.lstat()
            except FileNotFoundError:
                raise BridgeError("socket-binding-record-replaced") from None
            if _file_identity(path_info) != _file_identity(descriptor_info):
                raise BridgeError("socket-binding-record-replaced")
            if record["binding_identity"] != identity:
                raise BridgeError("socket-identity-collision")
            if record["socket_identity"] is not None:
                raise BridgeError("duplicate-live-bridge-identity")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _publish_socket_claim(
    descriptor: int,
    binding_path: pathlib.Path,
    socket_path: pathlib.Path,
    identity: dict[str, str],
) -> RendezvousVerification:
    """Bind the exact socket inode/type into the locked O_EXCL sidecar."""
    _, descriptor_info = _read_binding_record_descriptor(descriptor)
    try:
        binding_info = binding_path.lstat()
        socket_info = socket_path.lstat()
    except FileNotFoundError:
        raise BridgeError("socket rendezvous disappeared before publication") from None
    if _file_identity(binding_info) != _file_identity(descriptor_info):
        raise BridgeError("socket-binding-record-replaced")
    socket_identity = _socket_identity_record(socket_info)
    _validate_socket_identity(socket_identity)
    _write_binding_record_descriptor(
        descriptor,
        identity,
        socket_identity=socket_identity,
    )
    return verify_rendezvous_binding(socket_path, identity)


def _compare_unlink_socket(
    path: pathlib.Path,
    identity: dict[str, str],
    verification: RendezvousVerification,
) -> None:
    """Immediate inode/type compare-delete; replacement always survives."""
    verify_rendezvous_binding(path, identity, expected=verification)
    info = path.lstat()
    if _stat_identity(info) != verification.socket_identity or not stat.S_ISSOCK(info.st_mode):
        raise BridgeError("socket-rendezvous-replaced")
    path.unlink()
    _fsync_directory(path.parent)


def _compare_unlink_binding(
    path: pathlib.Path,
    identity: dict[str, str],
    *,
    expected: Optional[RendezvousVerification] = None,
) -> None:
    descriptor, record, info = _open_binding_record(path)
    try:
        if record["binding_identity"] != identity:
            raise BridgeError("socket-identity-collision")
        if expected is not None and _file_identity(info) != expected.sidecar_identity:
            raise BridgeError("socket-binding-record-replaced")
        path_info = path.lstat()
        if _file_identity(path_info) != _file_identity(info):
            raise BridgeError("socket-binding-record-replaced")
        path.unlink()
        _fsync_directory(path.parent)
    finally:
        os.close(descriptor)


def _file_digest_or_absent(path: pathlib.Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"


def profile_digest(agent_profile: str) -> str:
    """Digest the resolved profile without persisting its potentially secret values."""
    profile = load_agent_profile(agent_profile)
    return _digest(profile.model_dump(mode="json"))


def _kimi_wire_path(session_id: str, *, timeout: float = 5.0) -> pathlib.Path:
    """Resolve Kimi's version-bound structured session journal."""
    if not re.fullmatch(r"session_[A-Za-z0-9-]+", session_id):
        raise BridgeError("Kimi returned an unsafe provider session id")
    configured = os.environ.get("KIMI_CODE_HOME")
    home = (
        pathlib.Path(configured).expanduser() if configured else pathlib.Path.home() / ".kimi-code"
    )
    root = home.resolve() / "sessions"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = list(root.glob(f"*/{session_id}/agents/main/wire.jsonl"))
        if len(matches) == 1:
            resolved = matches[0].resolve()
            if resolved.is_file() and resolved.is_relative_to(root):
                return resolved
        if len(matches) > 1:
            raise BridgeError("Kimi provider session journal identity is ambiguous")
        time.sleep(0.05)
    raise BridgeError("Kimi provider session journal was not created")


def _wait_kimi_turn_start(
    wire_path: pathlib.Path, *, start_offset: int, timeout: float = 30.0
) -> dict[str, Any]:
    """Return Kimi's opaque step identity after its model loop begins."""
    deadline = time.monotonic() + timeout
    offset = start_offset
    pending = ""
    while time.monotonic() < deadline:
        try:
            with wire_path.open("r", encoding="utf-8") as wire:
                wire.seek(offset)
                chunk = wire.read()
                offset = wire.tell()
        except OSError as exc:
            raise BridgeError(f"Kimi provider session journal is unreadable: {exc}") from exc
        pending += chunk
        lines = pending.split("\n")
        pending = lines.pop()
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BridgeError("Kimi provider session journal contains invalid JSON") from exc
            event = item.get("event") if isinstance(item, dict) else None
            if (
                item.get("type") == "context.append_loop_event"
                and isinstance(event, dict)
                and event.get("type") == "step.begin"
                and isinstance(event.get("uuid"), str)
                and event["uuid"]
                and event.get("turnId") is not None
            ):
                return event
        time.sleep(0.05)
    raise BridgeError("Kimi emitted no structured provider turn-start identity")


def _profile_material(agent_profile: str, terminal_id: str) -> dict[str, Any]:
    profile = load_agent_profile(agent_profile)
    actual_digest = _digest(profile.model_dump(mode="json"))
    names = list(profile.mcpServers or {}) or None
    allowed_tools = resolve_allowed_tools(profile.allowedTools, profile.role, names)
    system_prompt = profile.system_prompt or ""
    skill_prompt = build_skill_catalog(profile.skills)
    if skill_prompt:
        system_prompt = f"{system_prompt}\n\n{skill_prompt}" if system_prompt else skill_prompt
    if allowed_tools and "*" not in allowed_tools:
        tools_list = ", ".join(allowed_tools)
        system_prompt = (
            SECURITY_PROMPT
            + f"\nYou only have access to these tools: {tools_list}\n"
            + system_prompt
        )

    mcp_servers: list[dict[str, Any]] = []
    for name, value in (profile.mcpServers or {}).items():
        raw = dict(value) if isinstance(value, dict) else value.model_dump(exclude_none=True)
        config = resolve_mcp_server_config(raw)
        env = {str(key): str(item) for key, item in (config.get("env") or {}).items()}
        env.setdefault("CAO_TERMINAL_ID", terminal_id)
        mcp_servers.append(
            {
                "name": name,
                "command": config["command"],
                "args": [str(item) for item in (config.get("args") or [])],
                "env": [{"name": key, "value": value} for key, value in sorted(env.items())],
                "env_vars": [str(item) for item in (config.get("env_vars") or [])],
                "tool_timeout_sec": config.get("tool_timeout_sec"),
            }
        )
    return {
        "profile": profile,
        "profile_sha256": actual_digest,
        "allowed_tools": allowed_tools,
        "system_prompt": system_prompt,
        "mcp_servers": mcp_servers,
    }


def write_request(reservation_id: str, request: dict[str, Any]) -> dict[str, pathlib.Path]:
    delivery_id = request.get("delivery_id")
    try:
        canonical_delivery_id = str(uuid.UUID(delivery_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise BridgeError("managed provider request requires a canonical delivery_id") from exc
    if delivery_id != canonical_delivery_id:
        raise BridgeError("managed provider request delivery_id is not canonical")
    identity = binding_identity(request)
    target = paths(reservation_id, request)
    target["root"].mkdir(mode=0o700, parents=True, exist_ok=True)
    if target["request"].exists():
        existing = json.loads(target["request"].read_text(encoding="utf-8"))
        if existing != request:
            raise BridgeError("managed provider request identity changed")
    else:
        _atomic_json(target["request"], request)
    if binding_identity(request) != identity:
        raise BridgeError("managed provider request rendezvous identity changed")
    return target


def read_state(reservation_id: str) -> Optional[dict[str, Any]]:
    path = paths(reservation_id)["state"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"managed provider state is unreadable: {exc}") from exc
    if not isinstance(value, dict) or value.get("bridge_version") != BRIDGE_VERSION:
        raise BridgeError("managed provider state has an unknown schema")
    return value


def validate_launch_failure(
    state: Any,
    *,
    reservation_id: str,
    terminal_id: str,
    generation: str,
    delivery_id: str,
    provider: str,
) -> dict[str, Any]:
    """Validate exact, names-only bridge failure evidence before a CAS."""
    if not isinstance(state, dict) or state.get("state") != "launch-failed-bridge":
        raise BridgeError("bridge state is not a launch-failed-bridge record")
    failure = state.get("launch_failure")
    if not isinstance(failure, dict):
        raise BridgeError("bridge launch failure evidence is missing")
    required_fields = {
        "schema",
        "evidence_digest_domain",
        "evidence_sha256",
        "outcome",
        "reservation_id",
        "terminal_id",
        "generation",
        "delivery_id",
        "error_class",
        "error_sha256",
        "log_evidence_sha256",
        "environment_inventory",
        "task_delivery",
        "provider_io_started",
        "task_bytes_submitted",
        "failed_at",
    }
    expected = {
        "outcome": "launch-failed-bridge",
        "reservation_id": reservation_id,
        "terminal_id": terminal_id,
        "generation": generation,
        "delivery_id": delivery_id,
        "task_bytes_submitted": False,
    }
    mismatches = {
        key: {"expected": value, "observed": failure.get(key)}
        for key, value in expected.items()
        if failure.get(key) != value
    }
    if set(failure) != required_fields:
        mismatches["fields"] = {
            "expected": sorted(required_fields),
            "observed": sorted(failure),
        }
    task_delivery = failure.get("task_delivery")
    if task_delivery != {
        "delivery_id": delivery_id,
        "status": "never-submitted",
    }:
        mismatches["task_delivery"] = {
            "expected": {"delivery_id": delivery_id, "status": "never-submitted"},
            "observed": task_delivery,
        }
    if state.get("readiness") is not None or state.get("submission") is not None:
        mismatches["provider_receipts"] = {
            "expected": None,
            "observed": {
                "readiness": state.get("readiness"),
                "submission": state.get("submission"),
            },
        }
    inventory = failure.get("environment_inventory")
    if not isinstance(inventory, dict) or set(inventory) != {
        "schema",
        "provider",
        "names",
        "names_sha256",
    }:
        mismatches["environment_inventory"] = {
            "expected": "names-only inventory",
            "observed": inventory,
        }
    else:
        names = inventory.get("names")
        names_valid = isinstance(names, list) and all(
            isinstance(name, str) and bool(name) for name in names
        )
        inventory_payload = {
            "schema": _BRIDGE_ENV_INVENTORY_SCHEMA,
            "provider": provider,
            "names": names,
        }
        if (
            inventory.get("schema") != _BRIDGE_ENV_INVENTORY_SCHEMA
            or inventory.get("provider") != provider
            or not names_valid
            or (names_valid and names != sorted(set(names)))
            or inventory.get("names_sha256") != _digest(inventory_payload)
            or state.get("environment_inventory") != inventory
        ):
            mismatches["environment_inventory"] = {
                "expected": "canonical provider-bound names-only inventory",
                "observed": inventory,
            }
    for digest_field in ("error_sha256", "log_evidence_sha256", "evidence_sha256"):
        value = failure.get(digest_field)
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            mismatches[digest_field] = {
                "expected": "64 lowercase hex characters",
                "observed": value,
            }
    if (
        failure.get("schema") != LAUNCH_FAILURE_SCHEMA
        or failure.get("evidence_digest_domain") != LAUNCH_FAILURE_DIGEST_DOMAIN
        or failure.get("evidence_sha256") != launch_failure_evidence_digest(failure)
        or not isinstance(failure.get("error_class"), str)
        or not failure["error_class"]
        or not isinstance(failure.get("provider_io_started"), bool)
        or not isinstance(failure.get("failed_at"), str)
        or not failure["failed_at"]
    ):
        mismatches["failure_schema"] = {
            "expected": (
                f"{LAUNCH_FAILURE_SCHEMA} with canonical "
                f"{LAUNCH_FAILURE_DIGEST_DOMAIN} proof and error_class"
            ),
            "observed": failure.get("schema"),
        }
    if mismatches:
        raise BridgeError(
            "bridge launch failure identity/evidence mismatch: "
            + json.dumps(mismatches, sort_keys=True, separators=(",", ":"))
        )
    return failure


def request_bridge(
    reservation_id: str, request: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    target = paths(reservation_id)
    socket_path = target["socket"]
    identity = binding_identity(json.loads(target["request"].read_text(encoding="utf-8")))
    deadline = time.monotonic() + timeout
    response: Any = None
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            try:
                verification = verify_rendezvous_binding(socket_path, identity)
            except BridgeError as exc:
                retryable = str(exc) in {
                    "socket-binding-record-absent",
                    "socket-binding-not-ready",
                }
                if not retryable or (
                    str(exc) == "socket-binding-record-absent"
                    and (socket_path.exists() or socket_path.is_symlink())
                ):
                    raise
                state = read_state(reservation_id)
                if state and state.get("state") in {
                    "preflight_blocked",
                    "launch-failed-bridge",
                }:
                    raise BridgeError(
                        str(state.get("error") or "managed provider failed before socket readiness")
                    ) from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                continue
            client.connect(str(socket_path))
            # The pathname may be replaced between verification and
            # connect. Re-pin the identical sidecar/socket inodes before
            # sending even the identity handshake to the connected peer.
            verify_rendezvous_binding(socket_path, identity, expected=verification)
            client.sendall(
                _canonical(
                    {
                        "rendezvous_identity": identity,
                        "request": request,
                    }
                )
                + b"\n"
            )
            received = bytearray()
            while b"\n" not in received:
                block = client.recv(65536)
                if not block:
                    raise BridgeError("managed provider bridge closed without a response")
                received.extend(block)
                if len(received) > 4 * 1024 * 1024:
                    raise BridgeError("managed provider bridge response exceeded 4 MiB")
            response = json.loads(bytes(received).split(b"\n", 1)[0])
            break
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last_error = exc
            state = read_state(reservation_id)
            if state and state.get("state") in {
                "preflight_blocked",
                "launch-failed-bridge",
            }:
                raise BridgeError(
                    str(state.get("error") or "managed provider failed before socket readiness")
                ) from exc
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError(f"managed provider bridge request failed: {exc}") from exc
        finally:
            client.close()
    if response is None:
        raise BridgeError(f"managed provider bridge was unavailable: {last_error}")
    if not isinstance(response, dict):
        raise BridgeError("managed provider bridge returned a non-object")
    if response.get("ok") is not True:
        raise BridgeError(str(response.get("error") or "managed provider bridge rejected request"))
    return response


def _operator_command(line: str) -> tuple[Optional[str], dict[str, Any]]:
    """Translate one terminal-console line into a semantic session operation."""
    text = line.strip()
    if not text:
        return None, {}
    if text == "/help":
        return "help", {}
    if text == "/cancel":
        return "cancel", {}
    if text == "/route":
        return "route-query", {}
    if text == "/resume-status":
        return "resume-status", {}
    if text.startswith("/operation ") or text.startswith("/op "):
        operation_id = text.split(maxsplit=1)[1].strip()
        if operation_id:
            return "operation-query", {"operation_id": operation_id}
    if text == "/compact" or text.startswith("/compact "):
        instruction = text[len("/compact") :].strip()
        return "compact", ({"instruction": instruction} if instruction else {})
    if text.startswith("/model "):
        return "route-set", {
            "config_id": "model",
            "value": text[len("/model ") :].strip(),
        }
    if text.startswith("/effort "):
        return "route-set", {
            "config_id": "thinking",
            "value": text[len("/effort ") :].strip(),
        }
    if text.startswith("/send "):
        return "follow-up", {"message": text[len("/send ") :]}
    if text.startswith("/"):
        return "invalid-command", {"command": text}
    return "follow-up", {"message": line.rstrip("\r\n")}


def _operator_console(request: dict[str, Any]) -> None:
    """Keep the managed worker directly steerable from its terminal pane."""
    print(
        "[operator] ready for input; type a message or /help "
        "(commands: /cancel, /compact, /route, /model, /effort, "
        "/resume-status, /operation)",
        flush=True,
    )
    for line in sys.stdin:
        action, payload = _operator_command(line)
        if action is None:
            continue
        if action == "help":
            print(
                "[operator] messages create provider-native follow-up turns; "
                "/cancel interrupts the active turn; /compact [instruction] compacts; "
                "/route queries the route; /model VALUE and /effort VALUE change it; "
                "/resume-status checks the persisted provider session; "
                "/operation ID reconciles an exact operation after response loss; "
                "/send TEXT sends prompt text that begins with a slash",
                flush=True,
            )
            continue
        if action == "invalid-command":
            print(
                f"[operator] unknown local command: {payload['command']}; "
                "use /help or /send TEXT",
                flush=True,
            )
            continue
        if action == "operation-query":
            operation_id = payload["operation_id"]
            command = {
                "bridge_version": BRIDGE_VERSION,
                "op": "session.op.query",
                "operation_id": operation_id,
                "reservation_id": request["reservation_id"],
                "terminal_id": request["terminal_id"],
                "generation": request["generation"],
            }
            try:
                response = request_bridge(request["reservation_id"], command, timeout=30.0)
                receipt = response.get("receipt") or {}
                state = receipt.get("state", "unknown")
                print(
                    f"\n[operator] operation {operation_id} is {state}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - operator-visible typed failure
                print(
                    f"\n[operator] operation {operation_id} could not be reconciled: {exc}",
                    flush=True,
                )
            continue
        operation_id = f"terminal-{uuid.uuid4()}"
        command = {
            "bridge_version": BRIDGE_VERSION,
            "op": "session.op.begin",
            "operation_id": operation_id,
            "action": action,
            "reservation_id": request["reservation_id"],
            "terminal_id": request["terminal_id"],
            "generation": request["generation"],
            **payload,
        }
        try:
            response = request_bridge(
                request["reservation_id"],
                command,
                timeout=16 * 60 if action == "compact" else 75.0,
            )
            receipt = response.get("receipt") or {}
            state = receipt.get("state", "unknown")
            detail = receipt.get("reason_detail")
            suffix = f": {detail}" if isinstance(detail, str) and detail else ""
            print(
                f"\n[operator] {action} {state} " f"(operation {operation_id}){suffix}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - operator-visible typed failure
            # The begin request may have crossed the provider boundary before
            # its response was lost. Reconcile the SAME durable operation ID;
            # never turn a console retry into a second provider turn.
            query = {
                "bridge_version": BRIDGE_VERSION,
                "op": "session.op.query",
                "operation_id": operation_id,
                "reservation_id": request["reservation_id"],
                "terminal_id": request["terminal_id"],
                "generation": request["generation"],
            }
            try:
                response = request_bridge(request["reservation_id"], query, timeout=30.0)
                receipt = response.get("receipt") or {}
                state = receipt.get("state", "unknown")
                print(
                    f"\n[operator] {action} response was lost, but operation "
                    f"{operation_id} is {state}; do not resend it",
                    flush=True,
                )
            except Exception as query_exc:  # noqa: BLE001 - exact ambiguity is visible
                print(
                    f"\n[operator] {action} outcome is unresolved "
                    f"(operation {operation_id}): {exc}; reconciliation failed: "
                    f"{query_exc}. Do not resend; use /operation {operation_id}",
                    flush=True,
                )


def _send_socket_response(connection: socket.socket, response: dict[str, Any]) -> bool:
    """Reply to one operator without letting response loss kill the bridge."""
    try:
        payload = _canonical(response) + b"\n"
    except Exception:  # noqa: BLE001 - serialization is connection-local
        logger.warning("managed operator response could not be serialized", exc_info=True)
        return False
    try:
        connection.sendall(payload)
        return True
    except OSError:
        logger.info(
            "managed operator disconnected before receiving its response",
            exc_info=True,
        )
        return False


def _authorize_operator_peer(
    connection: socket.socket, request: dict[str, Any]
) -> actor_broker.PeerCredentials:
    """Allow semantic controls only from this bridge or its pinned controller.

    The provider process tree runs under the operator's UID and can read the
    rendezvous envelope, so socket mode and request fields are not authority.
    Kernel peer credentials provide the non-forgeable process identity.  The
    controller PID is pinned before the provider is launched and never enters
    the provider environment; the bridge PID covers its local operator console.
    """
    try:
        peer = actor_broker.peer_credentials(connection)
    except Exception as exc:  # noqa: BLE001 - peer identity fails closed
        raise BridgeError(f"managed operator peer identity unavailable: {exc}") from exc
    controller_pid = request.get("controller_pid")
    allowed_pids = {os.getpid()}
    if isinstance(controller_pid, int) and controller_pid > 0:
        allowed_pids.add(controller_pid)
    if peer.uid != os.getuid() or peer.pid not in allowed_pids:
        raise BridgeError("managed operator peer is not the pinned conductor or bridge process")
    return peer


def _render_provider_diagnostic(line: str) -> Optional[str]:
    """Return one bounded human diagnostic, never a raw JSON payload."""
    diagnostic = line.strip()
    if not diagnostic:
        return None
    try:
        parsed = json.loads(diagnostic)
    except json.JSONDecodeError:
        return diagnostic[:500]
    if isinstance(parsed, dict):
        message = parsed.get("message")
        if not isinstance(message, str):
            error = parsed.get("error")
            message = error.get("message") if isinstance(error, dict) else None
        if isinstance(message, str) and message:
            return message[:500]
    return "structured detail suppressed"


class _RpcProcess:
    def __init__(
        self,
        argv: list[str],
        *,
        env: Optional[dict[str, str]] = None,
        companion_identity: Optional[tuple[str, str]] = None,
    ):
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        if self.proc.stdin is None or self.proc.stdout is None or self.proc.stderr is None:
            self.proc.kill()
            raise BridgeError("provider native process pipes were not created")
        # (terminal_id, generation) for the structured companion prompt
        # lifecycle (§20.2f P1-10): provider-native reverse requests are
        # recorded as generation-bound prompt observations while pending.
        self._companion_identity = companion_identity
        self._write_lock = threading.Lock()
        self._condition = threading.Condition()
        self._responses: dict[int, dict[str, Any]] = {}
        self._notifications: list[dict[str, Any]] = []
        self._next_id = 1
        self._closed_error: Optional[str] = None
        self._renderer = ManagedEventRenderer(provider="managed")
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _send(self, value: dict[str, Any]) -> None:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
        with self._write_lock:
            if self.proc.stdin is None:
                raise BridgeError("provider native stdin is closed")
            self.proc.stdin.write(raw + "\n")
            self.proc.stdin.flush()

    def _answer_reverse_request(self, item: dict[str, Any]) -> None:
        method = item.get("method")
        companion_prompt_id: Optional[str] = None
        if self._companion_identity is not None:
            # §20.2f P1-10: the provider-native structured prompt lifecycle.
            # Record the pending prompt observation before answering and close
            # it deterministically once answered — observation only, never an
            # answer beyond the bridge's existing managed-session policy.
            params = item.get("params") or {}
            if method == "session/request_permission":
                text = str(params.get("title") or method)
                choices = [
                    str(option.get("name") or option.get("optionId"))
                    for option in (params.get("options") or [])
                    if isinstance(option, dict)
                ]
            else:
                text = str(method)
                choices = []
            companion_prompt_id = f"{method}:{item.get('id')}"
            try:
                companion_receipts.record_prompt(
                    self._companion_identity[0],
                    self._companion_identity[1],
                    prompt_id=companion_prompt_id,
                    text=text,
                    choices=choices,
                )
            except Exception:  # noqa: BLE001 - observation never blocks the RPC
                companion_prompt_id = None
        try:
            if method == "session/request_permission":
                options = (item.get("params") or {}).get("options") or []
                selected = next(
                    (
                        option.get("optionId")
                        for option in options
                        if option.get("kind") in {"allow_always", "allow_once"}
                    ),
                    None,
                )
                if selected is None:
                    result = {"outcome": {"outcome": "cancelled"}}
                else:
                    result = {"outcome": {"outcome": "selected", "optionId": selected}}
                self._send({"jsonrpc": "2.0", "id": item["id"], "result": result})
                return
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": item["id"],
                    "error": {"code": -32601, "message": f"unsupported client method {method}"},
                }
            )
        finally:
            if companion_prompt_id is not None and self._companion_identity is not None:
                with contextlib.suppress(Exception):
                    companion_receipts.clear_prompt(
                        self._companion_identity[0],
                        self._companion_identity[1],
                        prompt_id=companion_prompt_id,
                    )

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    print("[provider protocol] unparseable output suppressed", flush=True)
                    continue
                if not isinstance(item, dict):
                    continue
                if "id" in item and "method" in item:
                    self._answer_reverse_request(item)
                    continue
                with self._condition:
                    if isinstance(item.get("id"), int) and ("result" in item or "error" in item):
                        self._responses[item["id"]] = item
                    else:
                        self._notifications.append(item)
                        rendered = self._renderer.render(item)
                        if rendered:
                            sys.stdout.write(rendered)
                            sys.stdout.flush()
                    self._condition.notify_all()
        finally:
            with self._condition:
                self._closed_error = f"provider native process exited {self.proc.poll()}"
                self._condition.notify_all()

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            diagnostic = _render_provider_diagnostic(line)
            if diagnostic is not None:
                print(f"[provider diagnostic] {diagnostic}", flush=True)

    def start_request(self, method: str, params: dict[str, Any]) -> int:
        with self._condition:
            request_id = self._next_id
            self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return request_id

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def wait_response(self, request_id: int, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while request_id not in self._responses:
                if self._closed_error:
                    raise BridgeError(self._closed_error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError(f"provider timed out awaiting response {request_id}")
                self._condition.wait(remaining)
            response = self._responses.pop(request_id)
        if "error" in response:
            raise BridgeError(f"provider request failed: {response['error']!r}")
        if "result" not in response:
            raise BridgeError("provider response omitted result")
        result = response["result"] or {}
        if not isinstance(result, dict):
            raise BridgeError("provider response result is not an object")
        return result

    def request(self, method: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        return self.wait_response(self.start_request(method, params), timeout)

    def wait_notification(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        start_index: int,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._condition:
            index = start_index
            while True:
                while index < len(self._notifications):
                    item = self._notifications[index]
                    index += 1
                    if predicate(item):
                        return item
                if self._closed_error:
                    raise BridgeError(self._closed_error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError("provider emitted no model-turn acceptance notification")
                self._condition.wait(remaining)

    def notification_count(self) -> int:
        with self._condition:
            return len(self._notifications)

    def notifications_since(self, index: int) -> tuple[list[dict[str, Any]], int]:
        """A snapshot of buffered notifications from `index`, and the new
        index — for companion observation scans (§20.2f P1-10)."""
        with self._condition:
            return list(self._notifications[index:]), len(self._notifications)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        if self.proc.poll() is None:
            self.proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=3)
        if self.proc.poll() is None:
            self.proc.kill()


class _ProviderSession:
    def __init__(self, request: dict[str, Any]):
        self.request = request
        self.provider = request["provider"]
        self.profile_material = _profile_material(request["agent_profile"], request["terminal_id"])
        if self.profile_material["profile_sha256"] != request["profile_sha256"]:
            raise BridgeError("managed provider profile changed after reservation")
        self.rpc: Optional[_RpcProcess] = None
        self.provider_session_id: Optional[str] = None
        self.readiness: Optional[dict[str, Any]] = None
        self.kimi_wire_path: Optional[pathlib.Path] = None
        self._companion_scan_index = 0
        self._current_turn_id: Optional[str] = None
        self._active_prompt_request_id: Optional[int] = None
        self._active_prompt_lock = threading.Lock()
        self.current_model = request["model"]
        self.current_effort = request["effort"]
        self._config_options: list[dict[str, Any]] = []
        self.provider_io_started = False
        # Per-session provider-turn ordinal (1-based), incremented only on a
        # natively accepted turn; the route receipt's event_sequence.
        self._turn_sequence = 0
        # One fenced heartbeat producer per bridge lifetime: epoch/sequence
        # and the coalescing watermark are producer state; constructing a
        # fresh producer per beat would restart the sequence (the fencing
        # compare step refuses that as a regression) and never coalesce.
        self._heartbeat_producer: Any = None

    def _companion_identity(self) -> tuple[str, str]:
        return (self.request["terminal_id"], self.request["generation"])

    def _base_receipt(self) -> dict[str, Any]:
        return {
            "bridge_version": BRIDGE_VERSION,
            "reservation_id": self.request["reservation_id"],
            "terminal_id": self.request["terminal_id"],
            "generation": self.request["generation"],
            "provider": self.request["provider"],
            "agent_profile": self.request["agent_profile"],
            # Receipts attest the route that actually crossed the provider
            # boundary.  These normally equal the reservation route; using
            # live values makes any unexpected pre-admission drift fail the
            # launch store's exact-route validation instead of certifying it.
            "model": self.current_model,
            "effort": self.current_effort,
            "working_directory": self.request["working_directory"],
        }

    def initialize(self) -> dict[str, Any]:
        if self.provider == "codex":
            readiness = self._initialize_codex()
        elif self.provider == "kimi_cli":
            readiness = self._initialize_kimi()
        else:
            raise BridgeError(f"unsupported managed provider {self.provider!r}")
        print(
            "[managed worker] "
            f"provider={self.provider} terminal={self.request['terminal_id']} "
            f"generation={self.request['generation']} "
            f"session={self.provider_session_id} "
            f"model={self.current_model} effort={self.current_effort}",
            flush=True,
        )
        print(
            "[managed worker] readable ACP view; use managed controls for input "
            "(raw terminal keystrokes are disabled)",
            flush=True,
        )
        return readiness

    def _version(self, executable: str, provider: str) -> str:
        """Run --version and enforce the provider's launch identity policy.

        ``provider`` is the short provider-contract name (``codex``, ``kimi``,
        ``claude``), not the wire key.  All providers use open enforcement by
        default: any non-empty semver-shaped version is accepted at the launch
        boundary.  An operator can opt one provider into strict exact-set
        enforcement with ``CAO_PROVIDER_VERSION_ENFORCEMENT_<PROVIDER>``.
        The receipt records the actual installed banner; advanced capability
        boundaries perform their own exact proven-build check.
        """
        if not os.path.isabs(executable) or os.path.realpath(executable) != executable:
            raise BridgeError("provider executable must be a canonical absolute path")
        if (
            _file_digest_or_absent(pathlib.Path(executable))
            != self.request["provider_executable_sha256"]
        ):
            raise BridgeError("provider executable digest changed after reservation")
        self.provider_io_started = True
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=provider_contracts.version_probe_timeout(self.provider),
            env=_provider_child_environment(self.request),
        )
        actual = proc.stdout.strip()
        if proc.returncode != 0:
            raise BridgeError(
                f"provider --version exited {proc.returncode}; stderr: {proc.stderr.strip()!r}"
            )
        try:
            provider_contracts.check_pinned_version(provider, actual)
        except provider_contracts.ProviderContractError as exc:
            raise BridgeError(f"unsupported provider version {actual!r}: {exc}") from exc
        return actual

    def _initialize_codex(self) -> dict[str, Any]:
        codex_bin = self.request["provider_executable"]
        version = self._version(codex_bin, provider_contracts.PROVIDER_CODEX)
        worktree = self.request["working_directory"]
        argv = [codex_bin, "-c", render_trusted_project_override(worktree)]
        argv.extend(["-c", _toml_override("model", self.request["model"])])
        argv.extend(["-c", _toml_override("model_reasoning_effort", self.request["effort"])])
        for server in self.profile_material["mcp_servers"]:
            name = _validate_config_key(server["name"], source="mcpServers name")
            prefix = f"mcp_servers.{name}"
            argv.extend(["-c", f"{prefix}.command={_toml_scalar(server['command'])}"])
            args_toml = "[" + ", ".join(_toml_scalar(item) for item in server["args"]) + "]"
            argv.extend(["-c", f"{prefix}.args={args_toml}"])
            for item in server["env"]:
                key = _validate_config_key(item["name"], source="mcpServers env")
                argv.extend(["-c", f"{prefix}.env.{key}={_toml_scalar(item['value'])}"])
            argv.extend(["-c", f"{prefix}.tool_timeout_sec=600.0"])
        argv.extend(["app-server", "--stdio"])
        argv = _launcher_argv(
            paths(self.request["reservation_id"], self.request)["socket"],
            binding_identity(self.request),
            argv,
        )
        config_path = pathlib.Path(os.path.expanduser("~/.codex/config.toml"))
        config_before = _file_digest_or_absent(config_path)
        self.provider_io_started = True
        self.rpc = _RpcProcess(
            argv,
            env=_provider_child_environment(self.request),
            companion_identity=self._companion_identity(),
        )
        initialize_request = {
            "clientInfo": {"name": "cao-managed-native", "version": BRIDGE_VERSION}
        }
        initialized = self.rpc.request("initialize", initialize_request)
        self.rpc.notify("initialized", {})
        config = self.rpc.request("config/read", {"cwd": worktree, "includeLayers": True})
        thread_params: dict[str, Any] = {
            "cwd": worktree,
            "ephemeral": False,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "model": self.request["model"],
        }
        if self.profile_material["system_prompt"]:
            thread_params["developerInstructions"] = self.profile_material["system_prompt"]
        thread = self.rpc.request("thread/start", thread_params)
        thread_info = thread.get("thread") or {}
        thread_id = thread_info.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise BridgeError("Codex thread/start omitted provider thread id")
        actual_model = thread.get("model")
        actual_effort = thread.get("reasoningEffort")
        actual_cwd = thread.get("cwd")
        if (
            actual_model != self.request["model"]
            or actual_effort != self.request["effort"]
            or actual_cwd != worktree
        ):
            raise BridgeError("Codex exact session resolved the wrong route")
        projects = (config.get("config") or {}).get("projects") or {}
        if (projects.get(worktree) or {}).get("trust_level") != "trusted":
            raise BridgeError("Codex exact session did not resolve project trust")
        if not (
            _contains_session_flags(config.get("origins"))
            or _contains_session_flags(config.get("layers"))
        ):
            raise BridgeError("Codex exact session did not prove sessionFlags trust origin")
        config_after = _file_digest_or_absent(config_path)
        if config_before != config_after:
            raise BridgeError("protected Codex user config changed during exact launch")
        self.provider_session_id = thread_id
        transcript = {
            "initialize": initialize_request,
            "initialized": initialized,
            "thread_start": thread_params,
            "thread_result": thread,
        }
        self.readiness = {
            **self._base_receipt(),
            "receipt_id": thread_id,
            "provider_session_id": thread_id,
            "provider_version": version,
            "provider_receipt_kind": "codex-thread-start",
            "provider_transcript_sha256": _digest(transcript),
            "protected_config_sha256": config_before,
            "model_input_ready": True,
        }
        return self.readiness

    def _initialize_kimi(self) -> dict[str, Any]:
        kimi_bin = self.request["provider_executable"]
        version = self._version(kimi_bin, provider_contracts.PROVIDER_KIMI)
        # Route control (thinking effort) comes ONLY from the reservation
        # request and is part of the final inventoried child environment.
        env = _provider_child_environment(self.request)
        self.provider_io_started = True
        self.rpc = _RpcProcess(
            _launcher_argv(
                paths(self.request["reservation_id"], self.request)["socket"],
                binding_identity(self.request),
                [kimi_bin, "acp"],
            ),
            env=env,
            companion_identity=self._companion_identity(),
        )
        initialize_request = {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "cao-managed-native", "version": BRIDGE_VERSION},
        }
        initialized = self.rpc.request("initialize", initialize_request)
        session_request = {
            "cwd": self.request["working_directory"],
            "mcpServers": self.profile_material["mcp_servers"],
        }
        session = self.rpc.request("session/new", session_request)
        session_id = session.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise BridgeError("Kimi session/new omitted provider session id")
        options = session.get("configOptions")
        if _current_option(options, category="model", option_id="model") != self.request["model"]:
            changed = self.rpc.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": "model", "value": self.request["model"]},
            )
            options = changed.get("configOptions")
        # A route that selects no effort sets none and checks none. The
        # model half is unchanged and still exact: what this route declines
        # to pin is the effort, not the model.
        selects_effort = provider_contracts.route_selects_effort(self.request["effort"])
        if (
            selects_effort
            and _current_option(options, category="thought_level", option_id="thinking")
            != self.request["effort"]
        ):
            changed = self.rpc.request(
                "session/set_config_option",
                {
                    "sessionId": session_id,
                    "configId": "thinking",
                    "value": self.request["effort"],
                },
            )
            options = changed.get("configOptions")
        if _current_option(options, category="model", option_id="model") != self.request[
            "model"
        ] or (
            selects_effort
            and _current_option(options, category="thought_level", option_id="thinking")
            != self.request["effort"]
        ):
            raise BridgeError("Kimi exact session resolved the wrong route")
        self._config_options = (
            [dict(option) for option in options if isinstance(option, dict)]
            if isinstance(options, list)
            else []
        )
        self.provider_session_id = session_id
        self.kimi_wire_path = _kimi_wire_path(session_id)
        transcript = {
            "initialize": initialize_request,
            "initialized": initialized,
            "session_new": session_request,
            "session_result": session,
            "config_options": options,
        }
        self.readiness = {
            **self._base_receipt(),
            "receipt_id": session_id,
            "provider_session_id": session_id,
            "provider_version": version,
            "provider_receipt_kind": "kimi-acp-session-new",
            "provider_transcript_sha256": _digest(transcript),
            "model_input_ready": True,
        }
        return self.readiness

    def _submit_provider_turn(
        self, message: str, *, client_message_id: str, meta: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        """Submit exact text to the provider as a new model turn and return
        (provider_turn_id, receipt_kind, provider_evidence). The provider's
        own turn identity is the only submission proof — never paste success
        or enqueue (§20.2d(1))."""
        assert self.rpc is not None and self.provider_session_id is not None
        if self.provider == "codex":
            params = {
                "threadId": self.provider_session_id,
                "input": [{"type": "text", "text": message}],
                "clientUserMessageId": client_message_id,
                "model": self.current_model,
                "effort": self.current_effort,
                "cwd": self.request["working_directory"],
                "approvalPolicy": "never",
            }
            try:
                result = self.rpc.request("turn/start", params, timeout=30.0)
                turn = result.get("turn") or {}
                turn_id = turn.get("id")
                if not isinstance(turn_id, str) or not turn_id:
                    raise BridgeError("Codex turn/start omitted provider turn id")
            except Exception as exc:
                # The turn/start request may have crossed the provider
                # boundary before the failure (timeout, connection loss,
                # malformed or error response after acceptance): the
                # outcome is unknowable — never assert non-submission.
                raise SubmitUncertain(
                    f"Codex turn/start outcome uncertain after provider boundary: {exc}"
                ) from exc
            evidence = {
                "method": "turn/start",
                "request_sha256": _digest(params),
                "response": result,
            }
            self._turn_sequence += 1
            return turn_id, "codex-turn-start", evidence
        if self.kimi_wire_path is None:
            raise BridgeError("Kimi exact session journal is unavailable")
        with self._active_prompt_lock:
            if self._active_prompt_request_id is not None:
                raise SessionOperationRefused(
                    "turn_busy",
                    "the Kimi session already has an active foreground turn; "
                    "cancel it or wait for completion before sending another prompt",
                )
        params = {
            "sessionId": self.provider_session_id,
            "prompt": [{"type": "text", "text": message}],
            "_meta": meta,
        }

        def accepted(item: dict[str, Any]) -> bool:
            if item.get("method") != "session/update":
                return False
            update = item.get("params") or {}
            return update.get("sessionId") == self.provider_session_id

        try:
            wire_offset = self.kimi_wire_path.stat().st_size
            start_index = self.rpc.notification_count()
            rpc_id = self.rpc.start_request("session/prompt", params)
            turn_start = _wait_kimi_turn_start(
                self.kimi_wire_path, start_offset=wire_offset, timeout=30.0
            )
            first_update = self.rpc.wait_notification(
                accepted, start_index=start_index, timeout=30.0
            )
            with self._active_prompt_lock:
                self._active_prompt_request_id = rpc_id
            threading.Thread(
                target=self._watch_kimi_prompt_completion,
                args=(rpc_id,),
                daemon=True,
            ).start()
        except Exception as exc:
            # The session/prompt request may have crossed the provider
            # boundary before the failure: the outcome is unknowable —
            # never assert non-submission.
            raise SubmitUncertain(
                f"Kimi session/prompt outcome uncertain after provider boundary: {exc}"
            ) from exc
        evidence = {
            "method": "session/prompt",
            "request_sha256": _digest(params),
            "provider_turn_start": turn_start,
            "first_provider_update": first_update,
            "provider_request_id": rpc_id,
        }
        self._turn_sequence += 1
        return turn_start["uuid"], "kimi-session-update", evidence

    def _watch_kimi_prompt_completion(self, request_id: int) -> None:
        """Clear the active-turn guard only on the prompt's native response."""
        assert self.rpc is not None
        try:
            result = self.rpc.wait_response(request_id, timeout=30 * 24 * 60 * 60)
            stop_reason = result.get("stopReason")
            if isinstance(stop_reason, str):
                print(f"\n[turn completed] {stop_reason}\n", flush=True)
        except Exception as exc:  # noqa: BLE001 - the control surface reports loss
            print(f"\n[turn completion unavailable] {exc}\n", flush=True)
        finally:
            with self._active_prompt_lock:
                if self._active_prompt_request_id == request_id:
                    self._active_prompt_request_id = None

    def _scan_companion_events(self) -> None:
        """§20.2f P1-10: record provider-native refusal receipts from the
        exact session's own notification stream — observation/receipt only,
        bound to the terminal's exact generation. Never blocks the session."""
        if self.rpc is None:
            return
        try:
            items, self._companion_scan_index = self.rpc.notifications_since(
                self._companion_scan_index
            )
            for item in items:
                for error_item in _iter_provider_error_items(item):
                    message = error_item.get("message")
                    if not isinstance(message, str) or not message:
                        continue
                    refusal_id = error_item.get("id")
                    if not isinstance(refusal_id, str) or not refusal_id:
                        refusal_id = _digest(error_item)
                    companion_receipts.record_refusal(
                        self.request["terminal_id"],
                        self.request["generation"],
                        refusal_id=refusal_id,
                        identity=message,
                        turn_id=self._current_turn_id or self.provider_session_id or "unknown",
                    )
        except Exception:  # noqa: BLE001 - observation never blocks the RPC
            logger.warning("managed bridge companion event scan failed", exc_info=True)

    def admit(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.rpc is None or self.provider_session_id is None or self.readiness is None:
            raise BridgeError("provider native session is not ready")
        expected = {
            "reservation_id": self.request["reservation_id"],
            "terminal_id": self.request["terminal_id"],
            "generation": self.request["generation"],
            "delivery_id": self.request["delivery_id"],
        }
        if any(request.get(key) != value for key, value in expected.items()):
            raise BridgeError("admission does not match the exact bridge generation")
        if (
            hashlib.sha256(request["message"].encode("utf-8")).hexdigest()
            != request["message_sha256"]
        ):
            raise BridgeError("admission message digest mismatch")
        # The W13 fence is the admission boundary, held atomically: the
        # generation fence lock is taken across the final fence recheck AND
        # every provider/model/tool-entry I/O, so a fence installed
        # concurrent with this admission cannot interleave (no
        # check-then-submit gap). A sealed generation rejects the entry
        # with zero provider I/O.
        from cli_agent_orchestrator.services import generation_fence

        try:
            with self._admission_critical_section():
                provider_turn_id, kind, provider_evidence = self._submit_provider_turn(
                    request["message"],
                    client_message_id=request["delivery_id"],
                    meta={
                        "caoReservationId": request["reservation_id"],
                        "caoGeneration": request["generation"],
                        "caoMessageSha256": request["message_sha256"],
                        "caoContextSha256": _digest(request.get("context") or {}),
                    },
                )
                self._current_turn_id = provider_turn_id
                self._scan_companion_events()
                self._emit_beat(provider_turn_id, f"{kind}:{provider_turn_id}")
        except generation_fence.FencedError as exc:
            prefix = (
                "successor-fenced-before-provider-io"
                if "no longer current" in str(exc)
                else "w13-fenced-before-provider-io"
            )
            raise BridgeError(f"{prefix}: {exc}") from exc
        except heartbeat_store.FencingRefused as exc:
            raise BridgeError(f"successor-fenced-before-provider-io: {exc}") from exc
        receipt_id = provider_turn_id
        return {
            **self._base_receipt(),
            "receipt_id": receipt_id,
            "provider_session_id": self.provider_session_id,
            "provider_turn_id": provider_turn_id,
            "provider_receipt_kind": kind,
            "provider_transcript_sha256": _digest(provider_evidence),
            "delivery_id": request["delivery_id"],
            "receiver_id": self.request["terminal_id"],
            "message_sha256": request["message_sha256"],
            "sender_id": request["sender_id"],
            "context": request["context"],
            "provider_accepted": True,
            "submitted_at": _now(),
        }

    def deliver_inbox(self, command: dict[str, Any]) -> dict[str, Any]:
        """P1-7 (final conformance §20.2f): submit one exact queued inbox
        message to the receiver's provider turn and record the provider-native
        ``terminal_queued → submitted`` acknowledgement into the generation-
        bound companion store. Binds message id + digest, the receiver's exact
        generation, and the provider session/turn — never inferred from
        ordinary inbox ``delivered``/terminal paste."""
        if self.rpc is None or self.provider_session_id is None or self.readiness is None:
            raise BridgeError("provider native session is not ready")
        expected = {
            "reservation_id": self.request["reservation_id"],
            "terminal_id": self.request["terminal_id"],
            "generation": self.request["generation"],
        }
        if any(command.get(key) != value for key, value in expected.items()):
            raise BridgeError("inbox delivery does not match the exact bridge generation")
        message = command.get("message")
        message_id = command.get("message_id")
        if not isinstance(message, str) or not message:
            raise BridgeError("inbox delivery omitted the message")
        if not isinstance(message_id, str) or not message_id:
            raise BridgeError("inbox delivery omitted the exact message id")
        if hashlib.sha256(message.encode("utf-8")).hexdigest() != command.get("message_sha256"):
            raise BridgeError("inbox delivery message digest mismatch")
        # Sealed generations reject queued unsubmitted input at the
        # boundary; the fence lock is held across the recheck and the
        # provider I/O (no check-then-submit gap).
        from cli_agent_orchestrator.services import generation_fence

        try:
            with self._admission_critical_section():
                recovery_operation_key = command.get("recovery_operation_key")
                if isinstance(recovery_operation_key, str) and recovery_operation_key:
                    from cli_agent_orchestrator.services import callback_recovery

                    callback_recovery.assert_provider_delivery_admissible(
                        recovery_operation_key,
                        terminal_id=self.request["terminal_id"],
                        generation=self.request["generation"],
                        message_id=message_id,
                    )
                expected_live = {
                    "expected_provider": self.provider,
                    "expected_provider_session_id": self.provider_session_id,
                    "expected_execution_mode": self.request.get("execution_mode") or "acp",
                }
                for field, observed in expected_live.items():
                    declared = command.get(field)
                    if declared is not None and declared != observed:
                        raise BridgeError(f"inbox delivery {field} changed at provider admission")
                provider_turn_id, kind, provider_evidence = self._submit_provider_turn(
                    message,
                    client_message_id=message_id,
                    meta={
                        "caoInboxMessageId": message_id,
                        "caoMessageSha256": command["message_sha256"],
                        "caoSenderId": command.get("sender_id"),
                    },
                )
                self._current_turn_id = provider_turn_id
                self._scan_companion_events()
                self._emit_beat(provider_turn_id, f"{kind}:{provider_turn_id}")
                submitted_at = _now()
                ack: dict[str, Any]
                if command.get("sender_generation") and command.get("message_created_at"):
                    from datetime import datetime, timezone

                    from cli_agent_orchestrator.services import model_turn_receipt_contract

                    created_at = datetime.fromisoformat(
                        command["message_created_at"].removesuffix("Z") + "+00:00"
                    )
                    if created_at.utcoffset() is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    ack = model_turn_receipt_contract.build_receipt(
                        message_id=message_id,
                        message_sha256=command["message_sha256"],
                        message_created_at=created_at,
                        sender_id=command["sender_id"],
                        sender_generation=command["sender_generation"],
                        receiver_id=self.request["terminal_id"],
                        receiver_generation=self.request["generation"],
                        provider=self.provider,
                        provider_session_id=self.provider_session_id,
                        provider_turn_id=provider_turn_id,
                        submitted_at=datetime.fromisoformat(
                            submitted_at.removesuffix("Z") + "+00:00"
                        ),
                    )
                else:
                    ack = {
                        "kind": "submitted",
                        "message_id": message_id,
                        "message_sha256": command["message_sha256"],
                        "sender_id": command.get("sender_id"),
                        "receiver_id": self.request["terminal_id"],
                        "receiver_generation": self.request["generation"],
                        "provider": self.provider,
                        "provider_session_id": self.provider_session_id,
                        "provider_turn_id": provider_turn_id,
                        "submitted_at": submitted_at,
                    }
                # Receipt publication is part of the same admission critical
                # section as provider I/O. A zero-effect resolver holding this
                # lock therefore sees either no effect or the durable receipt.
                companion_receipts.record_message_ack(
                    self.request["terminal_id"],
                    self.request["generation"],
                    message_id=message_id,
                    ack=ack,
                )
        except generation_fence.FencedError as exc:
            raise BridgeError(f"w13-fenced-before-provider-io: {exc}") from exc
        except heartbeat_store.FencingRefused as exc:
            raise BridgeError(f"successor-fenced-before-provider-io: {exc}") from exc
        # The per-turn route identity (§18.9) moves to this exact turn.
        companion_receipts.record_route_receipt(
            self.request["terminal_id"],
            self.request["generation"],
            provider=self.provider,
            model=self.current_model,
            effort=self.current_effort,
            receipt_id=provider_turn_id,
            turn_id=provider_turn_id,
            provider_version=(self.readiness or {}).get("provider_version"),
        )
        return {
            **self._base_receipt(),
            "receipt_id": provider_turn_id,
            "provider_session_id": self.provider_session_id,
            "provider_turn_id": provider_turn_id,
            "provider_receipt_kind": kind,
            "provider_transcript_sha256": _digest(provider_evidence),
            "message_id": message_id,
            "message_sha256": command["message_sha256"],
            "sender_id": command.get("sender_id"),
            "receiver_id": self.request["terminal_id"],
            "provider_accepted": True,
            "submitted_at": submitted_at,
        }

    def _available_command_names(self) -> set[str]:
        if self.rpc is None:
            return set()
        notifications, _ = self.rpc.notifications_since(0)
        names: set[str] = set()
        for item in notifications:
            if item.get("method") != "session/update":
                continue
            update = (item.get("params") or {}).get("update") or {}
            if update.get("sessionUpdate") != "available_commands_update":
                continue
            names = set()
            for command in update.get("availableCommands") or []:
                if isinstance(command, dict) and isinstance(command.get("name"), str):
                    names.add(command["name"])
        return names

    def _control_receipt(self, operation: dict[str, Any]) -> dict[str, Any]:
        return {
            **operation,
            "reservation_id": self.request["reservation_id"],
            "terminal_id": self.request["terminal_id"],
            "generation": self.request["generation"],
            "provider": self.provider,
            "provider_session_id": self.provider_session_id,
            "model": self.current_model,
            "effort": self.current_effort,
        }

    def _refuse_control(
        self,
        journal: SessionControlJournal,
        operation_id: str,
        code: str,
        detail: str,
    ) -> dict[str, Any]:
        operation = journal.get(operation_id)
        if operation["state"] not in {CONTROL_QUEUED, CONTROL_SUBMITTED, CONTROL_ACCEPTED}:
            return self._control_receipt(operation)
        return self._control_receipt(
            journal.transition(
                operation_id,
                CONTROL_REFUSED,
                reason_code=code,
                reason_detail=detail,
            )
        )

    def _complete_control(
        self,
        journal: SessionControlJournal,
        operation_id: str,
        *,
        result: Optional[dict[str, Any]] = None,
        evidence_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        operation = journal.get(operation_id)
        if operation["state"] == CONTROL_QUEUED:
            operation = journal.transition(operation_id, CONTROL_SUBMITTED)
        if operation["state"] == CONTROL_SUBMITTED:
            operation = journal.transition(
                operation_id,
                CONTROL_ACCEPTED,
                evidence_digest=evidence_digest,
            )
        if operation["state"] == CONTROL_ACCEPTED:
            operation = journal.transition(
                operation_id,
                CONTROL_COMPLETED,
                result=result,
                evidence_digest=evidence_digest,
            )
        return self._control_receipt(operation)

    def reconcile_session_operation(
        self, journal: SessionControlJournal, operation_id: str
    ) -> dict[str, Any]:
        """Passively close accepted prompt/cancel controls after native turn end."""
        operation = journal.get(operation_id)
        if (
            self.provider == "kimi_cli"
            and operation["state"] == CONTROL_ACCEPTED
            and operation["action"]
            in {
                "follow-up",
                "cancel",
            }
        ):
            with self._active_prompt_lock:
                active = self._active_prompt_request_id is not None
            if not active:
                operation = journal.transition(
                    operation_id,
                    CONTROL_COMPLETED,
                    result={"native_turn_active": False},
                )
        return self._control_receipt(operation)

    def _watch_compact_operation(
        self,
        journal: SessionControlJournal,
        operation_id: str,
        request_id: int,
        params: dict[str, Any],
        start_index: int,
    ) -> None:
        """Observe compact acceptance/completion without blocking controls."""
        assert self.rpc is not None
        try:
            self.rpc.wait_notification(
                lambda item: (
                    item.get("method") == "session/update"
                    and (item.get("params") or {}).get("sessionId") == self.provider_session_id
                ),
                start_index=start_index,
                timeout=30.0,
            )
            journal.transition(
                operation_id,
                CONTROL_ACCEPTED,
                evidence_digest=_digest(params),
            )
            result = self.rpc.wait_response(request_id, timeout=15 * 60)
            journal.transition(
                operation_id,
                CONTROL_COMPLETED,
                result=result,
                evidence_digest=_digest(result),
            )
        except Exception as exc:  # noqa: BLE001 - journal exact uncertainty
            current = journal.get(operation_id)
            if current["state"] in {CONTROL_SUBMITTED, CONTROL_ACCEPTED}:
                with contextlib.suppress(Exception):
                    journal.transition(
                        operation_id,
                        CONTROL_AMBIGUOUS,
                        reason_code="compact_outcome_ambiguous",
                        reason_detail=str(exc),
                    )
        finally:
            with self._active_prompt_lock:
                if self._active_prompt_request_id == request_id:
                    self._active_prompt_request_id = None

    def session_operation(
        self, command: dict[str, Any], journal: SessionControlJournal
    ) -> dict[str, Any]:
        """Execute one exact, journaled semantic control against this session."""
        if self.rpc is None or self.provider_session_id is None or self.readiness is None:
            raise BridgeError("provider native session is not ready")
        expected = {
            "reservation_id": self.request["reservation_id"],
            "terminal_id": self.request["terminal_id"],
            "generation": self.request["generation"],
        }
        if any(command.get(key) != value for key, value in expected.items()):
            raise BridgeError("session operation does not match the exact bridge generation")
        operation_id = command.get("operation_id")
        action = command.get("action")
        if not isinstance(operation_id, str) or not operation_id:
            raise BridgeError("session operation omitted operation_id")
        if not isinstance(action, str) or not action:
            raise BridgeError("session operation omitted action")
        existing = journal.get(operation_id)
        if existing["state"] != CONTROL_QUEUED:
            return self.reconcile_session_operation(journal, operation_id)

        if action == "route-query":
            return self._complete_control(
                journal,
                operation_id,
                result={
                    "model": self.current_model,
                    "effort": self.current_effort,
                    "config_options": self._config_options,
                    "capabilities": {
                        "follow_up": True,
                        "cancel": True,
                        "route_query": True,
                        "route_set": self.provider == "kimi_cli",
                        "compact": (
                            self.provider == "kimi_cli"
                            and "compact" in self._available_command_names()
                        ),
                        "resume_status": self.provider == "kimi_cli",
                    },
                },
            )

        if action == "resume":
            return self._refuse_control(
                journal,
                operation_id,
                "resume_requires_new_generation",
                "resume is a launch-time rebind operation and cannot replace the live generation",
            )

        if action == "resume-status":
            if self.provider != "kimi_cli":
                return self._refuse_control(
                    journal,
                    operation_id,
                    "capability_unsupported",
                    "this provider does not expose session/list on the managed bridge",
                )
            journal.transition(operation_id, CONTROL_SUBMITTED)
            try:
                listed = self.rpc.request(
                    "session/list",
                    {"cwd": self.request["working_directory"]},
                    timeout=30.0,
                )
            except Exception as exc:
                return self._control_receipt(
                    journal.transition(
                        operation_id,
                        CONTROL_AMBIGUOUS,
                        reason_code="provider_response_unavailable",
                        reason_detail=str(exc),
                    )
                )
            match = next(
                (
                    item
                    for item in listed.get("sessions") or []
                    if isinstance(item, dict) and item.get("sessionId") == self.provider_session_id
                ),
                None,
            )
            return self._complete_control(
                journal,
                operation_id,
                result={"resumable_session": match},
                evidence_digest=_digest(listed),
            )

        if action == "cancel":
            with self._active_prompt_lock:
                active_request = self._active_prompt_request_id
            if self.provider == "kimi_cli":
                if active_request is None:
                    return self._refuse_control(
                        journal,
                        operation_id,
                        "turn_not_active",
                        "the Kimi session has no active foreground turn to cancel",
                    )
                journal.transition(operation_id, CONTROL_SUBMITTED)
                try:
                    self.rpc.notify(
                        "session/cancel",
                        {"sessionId": self.provider_session_id},
                    )
                except Exception as exc:
                    return self._control_receipt(
                        journal.transition(
                            operation_id,
                            CONTROL_AMBIGUOUS,
                            reason_code="cancel_delivery_ambiguous",
                            reason_detail=str(exc),
                        )
                    )
                return self._control_receipt(
                    journal.transition(
                        operation_id,
                        CONTROL_ACCEPTED,
                        result={"cancelled_provider_request_id": active_request},
                    )
                )
            if not self._current_turn_id:
                return self._refuse_control(
                    journal,
                    operation_id,
                    "turn_not_active",
                    "the Codex session has no observed turn to interrupt",
                )
            journal.transition(operation_id, CONTROL_SUBMITTED)
            try:
                result = self.rpc.request(
                    "turn/interrupt",
                    {
                        "threadId": self.provider_session_id,
                        "turnId": self._current_turn_id,
                    },
                    timeout=30.0,
                )
            except Exception as exc:
                return self._control_receipt(
                    journal.transition(
                        operation_id,
                        CONTROL_AMBIGUOUS,
                        reason_code="cancel_outcome_ambiguous",
                        reason_detail=str(exc),
                    )
                )
            return self._complete_control(
                journal,
                operation_id,
                result=result,
                evidence_digest=_digest(result),
            )

        if action == "route-set":
            if self.provider != "kimi_cli":
                return self._refuse_control(
                    journal,
                    operation_id,
                    "capability_unsupported",
                    "route changes are not enabled for this provider version",
                )
            with self._active_prompt_lock:
                if self._active_prompt_request_id is not None:
                    return self._refuse_control(
                        journal,
                        operation_id,
                        "turn_busy",
                        "route changes require an idle Kimi foreground turn",
                    )
            config_id = command.get("config_id")
            value = command.get("value")
            if config_id not in {"model", "thinking"} or not isinstance(value, str) or not value:
                return self._refuse_control(
                    journal,
                    operation_id,
                    "invalid_route",
                    "route-set requires config_id model|thinking and a non-empty value",
                )
            journal.transition(operation_id, CONTROL_SUBMITTED)
            try:
                result = self.rpc.request(
                    "session/set_config_option",
                    {
                        "sessionId": self.provider_session_id,
                        "configId": config_id,
                        "value": value,
                    },
                    timeout=30.0,
                )
            except Exception as exc:
                detail = str(exc)
                state = (
                    CONTROL_REFUSED if "provider request failed:" in detail else CONTROL_AMBIGUOUS
                )
                return self._control_receipt(
                    journal.transition(
                        operation_id,
                        state,
                        reason_code=(
                            "route_refused"
                            if state == CONTROL_REFUSED
                            else "route_outcome_ambiguous"
                        ),
                        reason_detail=detail,
                    )
                )
            options = result.get("configOptions")
            category = "model" if config_id == "model" else "thought_level"
            if _current_option(options, category=category, option_id=config_id) != value:
                return self._control_receipt(
                    journal.transition(
                        operation_id,
                        CONTROL_REFUSED,
                        reason_code="route_not_applied",
                        reason_detail="provider response did not attest the requested route value",
                        evidence_digest=_digest(result),
                    )
                )
            self._config_options = (
                [dict(option) for option in options if isinstance(option, dict)]
                if isinstance(options, list)
                else []
            )
            if config_id == "model":
                self.current_model = value
            else:
                self.current_effort = value
            return self._complete_control(
                journal,
                operation_id,
                result={
                    "config_id": config_id,
                    "value": value,
                    "config_options": self._config_options,
                },
                evidence_digest=_digest(result),
            )

        if action == "compact":
            if self.provider != "kimi_cli" or "compact" not in self._available_command_names():
                return self._refuse_control(
                    journal,
                    operation_id,
                    "capability_unsupported",
                    "the provider did not advertise the compact ACP command",
                )
            with self._active_prompt_lock:
                if self._active_prompt_request_id is not None:
                    return self._refuse_control(
                        journal,
                        operation_id,
                        "turn_busy",
                        "compaction requires an idle foreground turn",
                    )
            instruction = command.get("instruction")
            if instruction is not None and not isinstance(instruction, str):
                return self._refuse_control(
                    journal,
                    operation_id,
                    "invalid_instruction",
                    "compact instruction must be text",
                )
            prompt = "/compact" + (
                f" {instruction.strip()}" if instruction and instruction.strip() else ""
            )
            params = {
                "sessionId": self.provider_session_id,
                "prompt": [{"type": "text", "text": prompt}],
            }
            journal.transition(operation_id, CONTROL_SUBMITTED)
            start_index = self.rpc.notification_count()
            try:
                # The generation lock covers only the native submission.  A
                # watcher observes acceptance/completion afterwards so cancel,
                # query, inbox reconciliation, and fencing remain available.
                with self._admission_critical_section():
                    request_id = self.rpc.start_request("session/prompt", params)
                    with self._active_prompt_lock:
                        self._active_prompt_request_id = request_id
                threading.Thread(
                    target=self._watch_compact_operation,
                    args=(journal, operation_id, request_id, params, start_index),
                    daemon=True,
                    name=f"cao-compact-{operation_id}",
                ).start()
            except Exception as exc:
                # Unlike an RPC failure after ``start_request``, a fence is
                # proven before any provider byte/request exists.  Preserve
                # that absorbing fact instead of misleading reconciliation
                # with an ambiguous compact submission.
                from cli_agent_orchestrator.services import generation_fence

                if isinstance(exc, generation_fence.FencedError):
                    return self._refuse_control(
                        journal,
                        operation_id,
                        "generation_fenced",
                        str(exc),
                    )
                return self._control_receipt(
                    journal.transition(
                        operation_id,
                        CONTROL_AMBIGUOUS,
                        reason_code="compact_submission_ambiguous",
                        reason_detail=str(exc),
                    )
                )
            return self._control_receipt(journal.get(operation_id))

        if action == "follow-up":
            message = command.get("message")
            if not isinstance(message, str) or not message.strip():
                return self._refuse_control(
                    journal,
                    operation_id,
                    "message_empty",
                    "follow-up requires a non-empty message",
                )
            journal.transition(operation_id, CONTROL_SUBMITTED)
            try:
                # Follow-up is provider input (including /terminals/{id}/input),
                # so it owns the exact same fence critical section as task
                # admission. Cancel and route-set are deliberately outside:
                # they are non-byte control/cleanup operations.
                with self._admission_critical_section():
                    turn_id, kind, evidence = self._submit_provider_turn(
                        message,
                        client_message_id=operation_id,
                        meta={
                            "caoSessionOperationId": operation_id,
                            "caoGeneration": self.request["generation"],
                            "caoMessageSha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                        },
                    )
            except Exception as exc:
                # A permanent generation fence is known before provider I/O.
                # It must not be journaled as an ambiguous prompt outcome: a
                # retry belongs to a successor generation, never this one.
                from cli_agent_orchestrator.services import generation_fence

                if isinstance(exc, generation_fence.FencedError):
                    return self._refuse_control(
                        journal,
                        operation_id,
                        "generation_fenced",
                        str(exc),
                    )
                if isinstance(exc, SessionOperationRefused):
                    return self._refuse_control(journal, operation_id, exc.code, exc.detail)
                return self._control_receipt(
                    journal.transition(
                        operation_id,
                        CONTROL_AMBIGUOUS,
                        reason_code="prompt_outcome_ambiguous",
                        reason_detail=str(exc),
                    )
                )
            self._current_turn_id = turn_id
            receipt = {
                "provider_turn_id": turn_id,
                "provider_receipt_kind": kind,
                "provider_transcript_sha256": _digest(evidence),
                "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            }
            _write_route_receipt(self, self.request, command, receipt, operation_id)
            return self._control_receipt(
                journal.transition(
                    operation_id,
                    CONTROL_ACCEPTED,
                    provider_turn_id=turn_id,
                    result=receipt,
                    evidence_digest=_digest(evidence),
                )
            )

        return self._refuse_control(
            journal,
            operation_id,
            "capability_unsupported",
            f"unsupported managed-session action {action!r}",
        )

    def close(self) -> None:
        if self.rpc is not None:
            self.rpc.close()

    @contextlib.contextmanager
    def _admission_critical_section(self):
        """Hold successor and W13 fences across final identity checks and I/O."""
        from cli_agent_orchestrator.constants import COMPANION_DIR
        from cli_agent_orchestrator.services import (
            generation_fence,
            heartbeat_store,
        )
        from cli_agent_orchestrator.services.destructive_endpoint import (
            binding_record_path,
        )

        terminal_id = self.request["terminal_id"]
        generation = self.request["generation"]
        with heartbeat_store.successor_critical_section(COMPANION_DIR, terminal_id):
            if self.request.get("obligation_generation"):
                binding_path = binding_record_path(COMPANION_DIR, terminal_id, generation)
                try:
                    binding = json.loads(binding_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise BridgeError(
                        "provider admission cannot revalidate generation binding"
                    ) from exc
                expected_binding = {
                    "terminal_id": terminal_id,
                    "generation": generation,
                    "attempt_id": self.request.get("attempt_id"),
                    "native_session_id": self.provider_session_id,
                }
                if {key: binding.get(key) for key in expected_binding} != expected_binding:
                    raise BridgeError("provider admission generation/session binding changed")
                try:
                    heartbeat_store.assert_current_fencing_binding(
                        COMPANION_DIR,
                        terminal_id=terminal_id,
                        generation=generation,
                        attempt_id=str(self.request.get("attempt_id") or ""),
                        fencing_token_id=str(binding.get("fencing_token_id") or ""),
                    )
                except heartbeat_store.FencingRefused as exc:
                    raise generation_fence.FencedError(
                        f"managed generation binding is no longer current: {exc}"
                    ) from exc
            with generation_fence.admission_critical_section(
                COMPANION_DIR, terminal_id, generation
            ):
                yield

    def _assert_fence_open(self) -> None:
        """Refuse provider-bound input for a sealed (W13-fenced) generation.

        Post-report input/tool admission must be *prevented*, not merely
        detected: a callback can only ever bind the tree the sealed
        generation actually left behind.  Callers that submit provider I/O
        must use ``_admission_critical_section`` instead — this bare check
        alone is a check-then-act seam.
        """
        from cli_agent_orchestrator.constants import COMPANION_DIR
        from cli_agent_orchestrator.services import generation_fence

        try:
            generation_fence.assert_admission_open(
                COMPANION_DIR, self.request["terminal_id"], self.request["generation"]
            )
        except generation_fence.FencedError as exc:
            raise BridgeError(str(exc)) from exc

    def _emit_beat(self, provider_turn_id: str, evidence_id: str) -> None:
        """Emit one fenced heartbeat for a provider-native turn event.

        Beats are a v2 behavior: they require the v2 identity fields in
        the bridge request and a producer fencing token issued at native
        bind.  A v1 request (no v2 fields) or an unbound generation
        produces no beat — v1 generations never gain v2 semantics.  A
        superseded producer's refusal is logged, never fatal: the bridge
        keeps serving its generation, and the fencing registry is what
        stops the stale writer.
        """
        from cli_agent_orchestrator.constants import COMPANION_DIR
        from cli_agent_orchestrator.services import heartbeat_store
        from cli_agent_orchestrator.services.destructive_endpoint import (
            binding_record_path,
        )

        request = self.request
        required = ("obligation_generation", "run_id", "assigned_policy_sha256", "project")
        if any(not request.get(field) for field in required):
            return
        terminal_id = request["terminal_id"]
        token = heartbeat_store.current_fencing_token(COMPANION_DIR, terminal_id)
        if token is None:
            return
        try:
            import json as _json

            binding = _json.loads(
                binding_record_path(COMPANION_DIR, terminal_id, request["generation"]).read_bytes()
            )
        except (OSError, _json.JSONDecodeError):
            return
        segment_hash = binding.get("route_payload_sha256")
        if not isinstance(segment_hash, str) or len(segment_hash) != 64:
            return  # unbound route fact: no truthful route field, no beat
        identity = heartbeat_store.HeartbeatIdentity(
            project=request["project"],
            task_id=request.get("task_id"),
            run_id=request["run_id"],
            obligation_generation=request["obligation_generation"],
            reservation_id=request["reservation_id"],
            launch_nonce_digest=binding.get("launch_nonce_digest", "0" * 64),
            terminal_id=terminal_id,
            generation=request["generation"],
            attempt_id=binding.get("attempt_id", ""),
            provider=request["provider"],
            provider_version=(self.readiness or {}).get("provider_version", "unknown"),
            native_session_id=self.provider_session_id or "",
            assigned_policy_sha256=request["assigned_policy_sha256"],
            segment_hash=segment_hash,
        )
        # Retain one producer for the bridge lifetime (reconstructed only
        # when the registered token changed); its epoch/sequence and
        # coalescing watermark are the durable producer state.
        producer = self._heartbeat_producer
        if producer is None or producer._token.id != token.id:  # noqa: SLF001
            producer = heartbeat_store.HeartbeatProducer(
                companion_dir=COMPANION_DIR, identity=identity, token=token
            )
            self._heartbeat_producer = producer
        evidence_kind = "app_server_event" if self.provider == "codex" else "acp_update"
        try:
            producer.beat(
                turn_state="active",
                provider_turn_id=provider_turn_id,
                evidence_kind=evidence_kind,
                evidence_id=evidence_id,
            )
        except heartbeat_store.FencingRefused:
            logger.warning(
                "heartbeat write refused for superseded generation %s",
                request["generation"],
            )
        except heartbeat_store.HeartbeatError:
            logger.warning(
                "heartbeat write failed for generation %s",
                request["generation"],
                exc_info=True,
            )


def _build_actor_broker(request: dict[str, Any], session: "_ProviderSession") -> Any:
    """The generation-private actor broker, wired to the real UDS accept path.

    Issuance happens only over the generation-private socket with
    kernel-verified peer credentials and live provider-tree lineage; the
    broker is bound to the exact generation and refuses once the fencing
    registry names a different (superseding) generation.
    """
    from cli_agent_orchestrator.constants import COMPANION_DIR
    from cli_agent_orchestrator.services import actor_broker, heartbeat_store

    if not actor_broker.platform_supported():
        return None
    provider_pids = (
        frozenset({session.rpc.proc.pid})
        if session.rpc is not None and session.rpc.proc is not None
        else frozenset()
    )
    terminal_id = request["terminal_id"]
    generation = request["generation"]

    def _generation_current() -> bool:
        record = heartbeat_store.current_fencing_record(COMPANION_DIR, terminal_id)
        return record is not None and record.get("generation") == generation

    return actor_broker.ActorBroker(
        state_dir=COMPANION_DIR / terminal_id / generation,
        terminal_generation=generation,
        provider_pids=provider_pids,
        generation_current=_generation_current,
    )


def _bridge_resources(
    target: dict[str, pathlib.Path], request: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    """The bridge's registry-first v2 identities: (kind, entry_id, fs_path).

    The bridge-state resource is the EXACT state file, never the
    reservation root: ``write_request`` creates the root in the launcher
    process before this process starts, so the root can never satisfy the
    declare-before-construction rule and is only the launcher-written
    envelope, removed best-effort at teardown without a registry claim.
    """
    reservation_id = request["reservation_id"]
    socket_entry_id = target["socket"].name
    return (
        ("socket", socket_entry_id, str(target["socket"])),
        ("bridge_state", f"{reservation_id}/state.json", str(target["state"])),
        (
            "db_row_set",
            f"{reservation_id}/delivery-journal.db",
            str(target["root"] / "delivery-journal.db"),
        ),
        (
            "db_row_set",
            f"{reservation_id}/session-control-journal.db",
            str(target["root"] / "session-control-journal.db"),
        ),
    )


def _declare_bridge_resources(target: dict[str, pathlib.Path], request: dict[str, Any]) -> None:
    """Durably declare the bridge's own v2 resources BEFORE construction.

    Declarations commit before ``_serve`` writes ``state.json`` or binds/
    listens on the generation-private socket, so a hard crash during
    provider initialization leaves discoverable declared entries for
    reconciliation — never physical artifacts with no registry row.
    Declaration is intent-only: nothing is marked ``created`` here, even
    if a stale artifact already occupies the path.  An exact still-declared
    row is a recoverable crash prefix and converges; every other live row is
    refused. Creation is receipted only after the exact artifact exists.
    """
    from cli_agent_orchestrator.services import resource_registry as rr

    registry = rr.get_resource_registry()
    actor = "managed_provider_bridge._serve"
    identity = binding_identity(request)
    for kind, entry_id, fs_path in _bridge_resources(target, request):
        binding = identity if kind == "socket" else None
        existing = registry.resolve_fs_path(fs_path)
        if existing is not None:
            exact = (
                existing.get("entry_id") == entry_id
                and existing.get("kind") == kind
                and existing.get("protocol_vintage") == "v2"
                and existing.get("terminal_id") == request["terminal_id"]
                and existing.get("generation") == request["generation"]
                and existing.get("owner") == "fork"
                and existing.get("ownership") == "owned"
                and existing.get("constructor_id") == actor
                and existing.get("deleter_id") == actor
                and existing.get("rollback_rule") == "generation-isolated"
                and existing.get("desired_fs_path") == fs_path
                and existing.get("binding_identity") == binding
            )
            if kind == "socket" and existing.get("binding_identity") != identity:
                raise BridgeError("socket-identity-collision")
            if exact and existing.get("lifecycle_state") == "declared":
                continue
            raise BridgeError("duplicate-live-bridge-identity")
        registry.declare(
            entry_id=entry_id,
            kind=kind,
            protocol_vintage="v2",
            terminal_id=request["terminal_id"],
            generation=request["generation"],
            owner="fork",
            ownership="owned",
            constructor_id=actor,
            deleter_id=actor,
            rollback_rule="generation-isolated",
            actor_id=actor,
            desired_fs_path=fs_path,
            binding_identity=binding,
        )


def _mark_bridge_resource_created(
    target: dict[str, pathlib.Path], request: dict[str, Any], kind: str
) -> None:
    """Receipt one bridge resource's observed physical creation.

    Called only after the exact filesystem identity exists (the state
    file write, or the socket bind/listen); a declared entry whose
    artifact is absent is never promoted.  Runs before the accept loop is
    exposed, so a registry failure here fails the bridge closed.
    """
    from cli_agent_orchestrator.services import resource_registry as rr

    actor = "managed_provider_bridge._serve"
    entries = {
        k: (entry_id, fs_path) for k, entry_id, fs_path in _bridge_resources(target, request)
    }
    entry_id, fs_path = entries[kind]
    registry = rr.get_resource_registry()
    entry = registry.resolve(entry_id)
    if entry["lifecycle_state"] == "declared" and pathlib.Path(fs_path).exists():
        observed: dict[str, Any] = {"observed_fs_path": fs_path}
        if kind == "socket":
            verification = verify_rendezvous_binding(
                pathlib.Path(fs_path),
                binding_identity(request),
            )
            observed["observed_fs_identity"] = verification.record["socket_identity"]
        registry.register_created(
            entry_id,
            actor_id=actor,
            observed=observed,
            existence_receipt_digest=rr.receipt_digest({"entry_id": entry_id, **observed}),
        )


def _mark_bridge_journal_created(target: dict[str, pathlib.Path], request: dict[str, Any]) -> None:
    """Mark the delivery-journal entry created once the journal file exists.

    The journal is declared at bridge startup but constructed lazily on
    the first journaled delivery; the registry transition happens only
    here, against the observed file — never at declaration time.
    """
    from cli_agent_orchestrator.services import resource_registry as rr

    actor = "managed_provider_bridge._serve"
    try:
        entry_id = f"{request['reservation_id']}/delivery-journal.db"
        fs_path = str(target["root"] / "delivery-journal.db")
        registry = rr.get_resource_registry()
        entry = registry.resolve(entry_id)
        if entry["lifecycle_state"] == "declared" and pathlib.Path(fs_path).exists():
            registry.register_created(
                entry_id,
                actor_id=actor,
                observed={"observed_fs_path": fs_path},
                existence_receipt_digest=rr.receipt_digest(
                    {"entry_id": entry_id, "observed_fs_path": fs_path}
                ),
            )
    except (rr.RegistryError, KeyError):
        pass  # never declared (tests bypassing registration): nothing to mark


def _mark_control_journal_created(target: dict[str, pathlib.Path], request: dict[str, Any]) -> None:
    """Mark the lazily constructed session-control journal as created."""
    from cli_agent_orchestrator.services import resource_registry as rr

    actor = "managed_provider_bridge._serve"
    try:
        entry_id = f"{request['reservation_id']}/session-control-journal.db"
        fs_path = str(target["root"] / "session-control-journal.db")
        registry = rr.get_resource_registry()
        entry = registry.resolve(entry_id)
        if entry["lifecycle_state"] == "declared" and pathlib.Path(fs_path).exists():
            registry.register_created(
                entry_id,
                actor_id=actor,
                observed={"observed_fs_path": fs_path},
                existence_receipt_digest=rr.receipt_digest(
                    {"entry_id": entry_id, "observed_fs_path": fs_path}
                ),
            )
    except (rr.RegistryError, KeyError):
        pass


def _deregister_bridge_resources(
    target: dict[str, pathlib.Path], request: dict[str, Any], *, retain_state: bool = False
) -> None:
    """Drain/close/delete the bridge's registry entries, truthfully.

    A still-declared entry is aborted ONLY on a verified-absence probe; a
    created entry is drained/closed, its physical artifact (socket, state
    file, journal DB and WAL/SHM siblings) is actually removed, and it is
    marked deleted ONLY after a real absence check — a resource that is
    still present keeps its row instead of a synthesized absence claim.
    With ``retain_state`` (the controlled-failure path) the state file is
    deliberately kept as the launcher's ``preflight_blocked`` diagnostic:
    its row stays ``closed`` — present and discoverable, never deleted
    underneath a surviving artifact.  The launcher-written envelope
    (``request.json`` and the reservation root, created by
    ``write_request`` before this process started) is not a registry
    resource; it is removed best-effort once the registered resources are
    reconciled, and a retained diagnostic keeps the root present.
    """
    from cli_agent_orchestrator.services import resource_registry as rr

    actor = "managed_provider_bridge._serve"
    state_entry_id = f"{request['reservation_id']}/state.json"
    try:
        registry = rr.get_resource_registry()
        entries = registry.enumerate(
            terminal_id=request["terminal_id"], generation=request["generation"]
        )
    except Exception:  # noqa: BLE001 - teardown never wedges on the registry
        logger.warning("bridge registry enumeration failed during teardown", exc_info=True)
        return

    def _remove(fs_path: str) -> None:
        path = pathlib.Path(fs_path)
        if path == target["socket"]:
            identity = binding_identity(request)
            verification = verify_rendezvous_binding(path, identity)
            _compare_unlink_socket(path, identity, verification)
            _compare_unlink_binding(target["binding"], identity, expected=verification)
            return
        with contextlib.suppress(OSError):
            path.unlink()
        with contextlib.suppress(OSError):
            path.with_name(path.name + "-wal").unlink()
        with contextlib.suppress(OSError):
            path.with_name(path.name + "-shm").unlink()
        if path.is_dir():
            import shutil

            shutil.rmtree(path, ignore_errors=True)

    def _path_present(path: pathlib.Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    for entry in entries:
        if entry["constructor_id"] != actor:
            continue
        entry_id = entry["entry_id"]
        state = entry["lifecycle_state"]
        if state in ("deleted", "aborted"):
            continue  # already terminal (e.g. converged by the terminal deleter)
        fs_path = entry["desired_fs_path"]
        absence = rr.receipt_digest(
            {"entry_id": entry_id, "absent": True, "probe": {"fs_missing": fs_path}}
        )
        try:
            if state == "declared":
                path = pathlib.Path(fs_path) if fs_path else None
                present = path is not None and _path_present(path)
                if path == target["socket"] and not present and _path_present(target["binding"]):
                    # A crash after sidecar creation but before bind leaves
                    # no socket. Compare-delete only the exact full tuple;
                    # malformed or foreign state preserves artifact and row.
                    identity = binding_identity(request)
                    record = _read_binding_record(target["binding"])
                    if (
                        record["binding_identity"] != identity
                        or record["socket_identity"] is not None
                    ):
                        raise BridgeError("socket-identity-collision")
                    _compare_unlink_binding(target["binding"], identity)
                if present:
                    observed: dict[str, Any] = {"observed_fs_path": fs_path}
                    if path == target["socket"]:
                        verification = verify_rendezvous_binding(
                            path,
                            binding_identity(request),
                        )
                        observed["observed_fs_identity"] = verification.record["socket_identity"]
                    # Created but never receipt-marked: discover, then drain.
                    registry.register_created(
                        entry_id,
                        actor_id=actor,
                        observed=observed,
                        existence_receipt_digest=rr.receipt_digest(
                            {"entry_id": entry_id, **observed}
                        ),
                    )
                    state = "created"
                else:
                    registry.abort(entry_id, actor_id=actor, verified_absence_digest=absence)
                    continue
            if state in ("created", "active"):
                registry.drain(entry_id, actor_id=actor)
                state = "draining"
            if state == "draining":
                registry.close(entry_id, actor_id=actor)
            if retain_state and entry_id == state_entry_id:
                # Controlled-failure diagnostic: the row stays closed and
                # the file present — discoverable, never a false absence.
                continue
            if fs_path:
                _remove(fs_path)
            if not fs_path or not _path_present(pathlib.Path(fs_path)):
                registry.delete(entry_id, actor_id=actor, verified_absence_digest=absence)
            else:
                logger.warning(
                    "bridge resource %s still present after teardown; row retained", entry_id
                )
        except Exception:  # noqa: BLE001 - best-effort teardown
            logger.warning("bridge resource %s deregistration failed", entry_id, exc_info=True)

    # The launcher-written envelope is not registry-tracked; remove it
    # best-effort once the entries above are reconciled.
    with contextlib.suppress(OSError):
        (target["root"] / "request.json").unlink()
    with contextlib.suppress(OSError):
        target["root"].rmdir()


def rendezvous_resource_presence(entry: dict[str, Any]) -> Optional[bool]:
    """Return exact-tuple presence; malformed/foreign state is unknown."""
    identity = entry.get("binding_identity")
    fs_path = entry.get("desired_fs_path")
    if not isinstance(identity, dict) or not isinstance(fs_path, str):
        return None
    socket_path = pathlib.Path(fs_path)
    binding_path = socket_path.with_suffix(".json")
    socket_exists = socket_path.exists() or socket_path.is_symlink()
    binding_exists = binding_path.exists() or binding_path.is_symlink()
    if not socket_exists and not binding_exists:
        return False
    try:
        verify_rendezvous_binding(socket_path, identity)
    except BridgeError:
        return None
    return True


def cleanup_stale_rendezvous(
    entry: dict[str, Any],
    *,
    terminal_id: str,
    generation: str,
) -> None:
    """Compare-delete one exact tuple after generation-bound teardown proof.

    The caller is the v2 terminal teardown path, which is reachable only
    after the destructive endpoint has verified and consumed the exact
    generation's no-survivor proof. The registry row must already be closed;
    a live, mismatched, malformed, or unbound row yields zero unlink.
    """
    if (
        entry.get("kind") != "socket"
        or entry.get("terminal_id") != terminal_id
        or entry.get("generation") != generation
        or entry.get("lifecycle_state") != "closed"
    ):
        raise BridgeError("stale rendezvous cleanup lacks proven-dead exact ownership")
    identity = _validate_binding_identity(entry.get("binding_identity"))
    if identity["terminal_id"] != terminal_id or identity["terminal_generation"] != generation:
        raise BridgeError("stale rendezvous registry binding mismatch")
    socket_path = pathlib.Path(entry["desired_fs_path"])
    expected = rendezvous_paths(identity)
    if socket_path != expected["socket"] or entry.get("observed_fs_path") != str(socket_path):
        raise BridgeError("stale rendezvous path binding mismatch")
    verification = verify_rendezvous_binding(socket_path, identity)
    if entry.get("observed_fs_identity") != verification.record["socket_identity"]:
        raise BridgeError("stale rendezvous registry socket identity mismatch")
    _compare_unlink_socket(socket_path, identity, verification)
    _compare_unlink_binding(expected["binding"], identity, expected=verification)


def _handle_actor_assertion(
    connection: socket.socket,
    command: dict[str, Any],
    broker: Any,
    provider_channel: dict[str, Any],
) -> None:
    """The actor-broker issuance boundary, on its own thread.

    Kernel peer credentials and provider-tree lineage are verified on
    this generation-private connection. A genuine in-tree peer (the
    provider child or a descendant) is issued to directly; any other peer
    (the conductor/bridge client is never in the provider tree) is
    relayed through the provider-originated channel, where the broker
    re-verifies kernel peer + lineage on THAT connection at issue time.
    Runs off the accept loop so a relay wait can never deadlock against
    the channel's own pending connection.
    """
    with connection:
        try:
            if broker is None:
                raise BridgeError("actor broker is unavailable for this generation")
            required = (
                "report_sha256",
                "report_path",
                "project",
                "run_id",
                "obligation_generation",
                "attempt_id",
                "native_session_id",
                "launch_nonce_digest",
                "route_chain_head",
            )
            missing = [
                field
                for field in required
                if not isinstance(command.get(field), str) or not command.get(field)
            ]
            if missing:
                raise BridgeError(f"actor assertion request missing fields: {missing}")
            fields = {
                "report_sha256": command["report_sha256"],
                "report_path": command["report_path"],
                "project": command["project"],
                "task_id": command.get("task_id"),
                "run_id": command["run_id"],
                "obligation_generation": command["obligation_generation"],
                "attempt_id": command["attempt_id"],
                "native_session_id": command["native_session_id"],
                "launch_nonce_digest": command["launch_nonce_digest"],
                "route_chain_head": command["route_chain_head"],
            }
            from cli_agent_orchestrator.services.actor_broker import ActorRefused

            try:
                assertion = broker.issue(connection, **fields)
                issued_via = "direct-provider-peer"
            except ActorRefused:
                assertion = _issue_via_provider_channel(broker, provider_channel, fields)
                issued_via = "provider-channel"
            response = {"ok": True, "assertion": assertion, "issued_via": issued_via}
        except Exception as exc:  # noqa: BLE001 - structured socket failure
            response = {"ok": False, "error": str(exc)}
        connection.sendall(_canonical(response) + b"\n")


def _issue_via_provider_channel(
    broker: Any, provider_channel: dict[str, Any], fields: dict[str, Any]
) -> dict[str, Any]:
    """Provider-originated issuance for a non-provider (conductor) peer.

    The conductor/bridge client can never satisfy the provider-tree
    lineage rule (it is not descended from the provider child — the
    bridge is its ancestor). Issuance therefore happens on the
    provider-originated channel: the request is handed to the live
    provider tree, its ack proves the tree originated it, and the broker
    issues on the channel connection whose kernel peer is the provider
    launcher itself. The same-UID conductor/collector/reconciler peer is
    never issued to directly.
    """
    request_id = str(uuid.uuid4())
    with provider_channel["cv"]:
        deadline = time.monotonic() + 5.0
        while provider_channel["conn"] is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError(
                    "actor-unavailable: no live provider-originated channel " "for this generation"
                )
            provider_channel["cv"].wait(remaining)
        channel_conn = provider_channel["conn"]
    with provider_channel["write_lock"]:
        channel_conn.sendall(_canonical({"op": "issue-request", "request_id": request_id}) + b"\n")
    with provider_channel["cv"]:
        deadline = time.monotonic() + 10.0
        while request_id not in provider_channel["acks"]:
            if provider_channel["conn"] is None:
                raise BridgeError(
                    "actor-unavailable: provider-originated channel dropped " "during issuance"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError(
                    "actor-unavailable: the provider tree did not acknowledge "
                    "the issuance request"
                )
            provider_channel["cv"].wait(remaining)
        provider_channel["acks"].pop(request_id, None)
    assertion: dict[str, Any] = broker.issue(channel_conn, **fields)
    return assertion


def _write_route_receipt(
    session: "_ProviderSession",
    request: dict[str, Any],
    command: dict[str, Any],
    receipt: dict[str, Any],
    delivery_id: str,
) -> None:
    """Publish the provider-observed durable route receipt (cond-0069).

    The bridge observed this exact turn's native acceptance; the receipt
    binds the provider session/turn/generation identity, the pinned
    resolved route (the provider-resolved model/effort, verified equal to
    the reservation request at exact-session initialization), the
    per-session positive turn sequence, and the journaled model-input
    digest.  It is HMAC-authenticated with the generation-private key and
    published immutably for ``/managed/recovery-capabilities`` to consume
    — the capability surface's only route-receipt provenance.  A
    publication failure never blocks an admitted turn; it simply yields
    no route authority (fail closed).
    """
    from cli_agent_orchestrator.services import route_receipts

    try:
        provider = request["provider"]
        route_receipts.write_route_receipt(
            state_dir=CAO_HOME_DIR / "recovery",
            provider=provider,
            native_session_id=str(session.provider_session_id),
            native_turn_id=str(receipt["provider_turn_id"]),
            generation=request["generation"],
            terminal_id=request["terminal_id"],
            delivery_id=delivery_id,
            expected_model=session.current_model,
            expected_effort=session.current_effort,
            observed_model=session.current_model,
            observed_effort=session.current_effort,
            protocol=route_receipts.protocol_version(provider),
            event_sequence=session._turn_sequence,
            model_input_digest=_digest(command),
            provider_version=str((session.readiness or {}).get("provider_version") or ""),
        )
    except Exception:  # noqa: BLE001 - receipt loss means no authority, never a wedge
        logger.warning("route receipt publication failed", exc_info=True)


def _claim_rendezvous(
    request: dict[str, Any], target: dict[str, pathlib.Path]
) -> tuple[dict[str, str], int]:
    """Acquire/recover the O_EXCL record only after exact registry intent."""
    identity = binding_identity(request)
    expected = rendezvous_paths(identity)
    if target.get("socket") != expected["socket"] or target.get("binding") != expected["binding"]:
        raise BridgeError("bridge rendezvous target does not match the launch tuple")

    from cli_agent_orchestrator.services import resource_registry as rr

    registry = rr.get_resource_registry()
    try:
        existing = registry.resolve(expected["socket"].name)
    except rr.RegistryNotFound:
        raise BridgeError("socket rendezvous lacks registry-first ownership") from None
    exact_registry_claim = (
        existing.get("kind") == "socket"
        and existing.get("protocol_vintage") == "v2"
        and existing.get("terminal_id") == request["terminal_id"]
        and existing.get("generation") == request["generation"]
        and existing.get("owner") == "fork"
        and existing.get("ownership") == "owned"
        and existing.get("constructor_id") == "managed_provider_bridge._serve"
        and existing.get("desired_fs_path") == str(expected["socket"])
        and existing.get("binding_identity") == identity
        and existing.get("lifecycle_state") == "declared"
    )
    if not exact_registry_claim:
        if existing.get("binding_identity") != identity:
            raise BridgeError("socket-identity-collision")
        raise BridgeError("duplicate-live-bridge-identity")
    socket_exists = expected["socket"].exists() or expected["socket"].is_symlink()
    binding_exists = expected["binding"].exists() or expected["binding"].is_symlink()
    if socket_exists and not binding_exists:
        raise BridgeError("socket-binding-record-absent")
    if socket_exists:
        record = _read_binding_record(expected["binding"])
        if record["binding_identity"] != identity:
            raise BridgeError("socket-identity-collision")
        raise BridgeError("duplicate-live-bridge-identity")
    descriptor = _acquire_binding_claim(expected["binding"], identity)
    return identity, descriptor


def _record_handshake_refusal(
    target: dict[str, pathlib.Path],
    state: dict[str, Any],
    *,
    observed: Any,
    reason: str,
) -> None:
    refusals = state.setdefault("handshake_refusals", [])
    refusals.append(
        {
            "at": _now(),
            "reason": reason,
            "observed_identity_sha256": (
                hashlib.sha256(_canonical(observed)).hexdigest()
                if isinstance(observed, dict)
                else None
            ),
        }
    )
    _atomic_json(target["state"], state)


def _launch_failure(
    request: dict[str, Any],
    exc: BaseException,
    environment_inventory: dict[str, Any],
    *,
    provider_io_started: bool,
) -> dict[str, Any]:
    """Build the exact, no-task-I/O bridge failure evidence."""
    diagnostic = f"[managed-provider-blocked] {exc}\n"
    failure = {
        "schema": LAUNCH_FAILURE_SCHEMA,
        "evidence_digest_domain": LAUNCH_FAILURE_DIGEST_DOMAIN,
        "outcome": "launch-failed-bridge",
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "delivery_id": request["delivery_id"],
        "error_class": type(exc).__name__,
        "error_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
        "log_evidence_sha256": hashlib.sha256(diagnostic.encode("utf-8")).hexdigest(),
        "environment_inventory": environment_inventory,
        "task_delivery": {
            "delivery_id": request["delivery_id"],
            "status": "never-submitted",
        },
        "provider_io_started": provider_io_started,
        "task_bytes_submitted": False,
        "failed_at": _now(),
    }
    return {
        **failure,
        "evidence_sha256": launch_failure_evidence_digest(failure),
    }


def launch_failure_evidence_digest(failure: dict[str, Any]) -> str:
    """Digest the canonical proof without a self-referential digest field.

    Exact bytes are ``UTF8(domain) + NUL + canonical-json(payload)`` where
    canonical JSON is UTF-8, sorted keys, compact separators, no newline, and
    ``payload`` is the complete failure object with ``evidence_sha256``
    removed.
    """
    payload = dict(failure)
    payload.pop("evidence_sha256", None)
    raw = LAUNCH_FAILURE_DIGEST_DOMAIN.encode("utf-8") + b"\0" + _canonical(payload)
    return hashlib.sha256(raw).hexdigest()


def _scope_direct_serve_environment(serve: Callable[..., int]) -> Callable[..., int]:
    """Restore process state for direct calls that do not pre-bind an inventory."""

    @functools.wraps(serve)
    def scoped(
        request: dict[str, Any],
        target: dict[str, pathlib.Path],
        *,
        environment_inventory: Optional[dict[str, Any]] = None,
    ) -> int:
        if environment_inventory is not None:
            return serve(request, target, environment_inventory=environment_inventory)

        ambient_environment = dict(os.environ)
        try:
            direct_inventory = _bind_bridge_environment(request)
            return serve(request, target, environment_inventory=direct_inventory)
        finally:
            global _BOUND_PROVIDER_ENV
            _BOUND_PROVIDER_ENV = None
            os.environ.clear()
            os.environ.update(ambient_environment)

    return scoped


@_scope_direct_serve_environment
def _serve(
    request: dict[str, Any],
    target: dict[str, pathlib.Path],
    *,
    environment_inventory: Optional[dict[str, Any]] = None,
) -> int:
    """Serve one bridge launch under its already-bound provider environment."""
    if environment_inventory is None:
        raise BridgeError("managed bridge environment inventory is not bound")
    state = {
        "bridge_version": BRIDGE_VERSION,
        "request_sha256": _digest(request),
        "state": "starting",
        "first_seen_at": time.time(),
        "readiness": None,
        "submission": None,
        "handshake_refusals": [],
        "environment_inventory": environment_inventory,
    }
    session: Optional[_ProviderSession] = None
    claimed = False
    claim_descriptor: Optional[int] = None
    blocked = False
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # Registry-first, declaration-before-construction: the bridge's
        # own socket/state/journal identities are durably declared before
        # the O_EXCL sidecar. Exact declared prefixes are crash-recoverable;
        # there is no sidecar-without-row crash window.
        _declare_bridge_resources(target, request)
        identity, claim_descriptor = _claim_rendezvous(request, target)
        claimed = True
        _atomic_json(target["state"], state)
        _mark_bridge_resource_created(target, request, "bridge_state")
        # Provider-bound composition already scrubbed the inherited tmux
        # environment. Keep the guard as the last line before provider
        # construction and I/O.
        _assert_bridge_environment()
        session = _ProviderSession(request)
        verify_launch_binding_identity(identity)
        server.bind(str(target["socket"]))
        os.chmod(target["socket"], 0o600)
        server.listen(8)
        _publish_socket_claim(
            claim_descriptor,
            target["binding"],
            target["socket"],
            identity,
        )
        _mark_bridge_resource_created(target, request, "socket")
        readiness = session.initialize()
        session._scan_companion_events()
        if sys.stdin.isatty():
            operator_console = threading.Thread(
                target=_operator_console,
                args=(request,),
                daemon=True,
                name=f"cao-operator-{request['terminal_id']}",
            )
            operator_console.start()
        readiness["operator_surface"] = {
            "terminal_input": bool(sys.stdin.isatty()),
            "semantic_socket": True,
        }
        state.update({"state": "ready", "readiness": readiness})
        _atomic_json(target["state"], state)
        # Lane-B production wiring: the generation-private UDS accept path
        # is the actor broker's issuance boundary, and the delivery journal
        # records intent/submit/ack transitions around the real provider
        # call. Neither capability existed before this wiring.
        broker = _build_actor_broker(request, session)
        journal: Any = None
        control_journal: Optional[SessionControlJournal] = None
        print(
            f"[managed-provider-ready] provider={request['provider']} "
            f"session={readiness['provider_session_id']} generation={request['generation']} "
            "input=terminal+semantic-api",
            flush=True,
        )
        # The provider-originated issuance channel: exactly one connection
        # whose kernel peer is inside the live provider process tree (the
        # launcher shim). Issuance for a non-provider (conductor) peer is
        # relayed through it; the broker still performs its own kernel +
        # lineage verification on the channel connection at issue time.
        provider_channel: dict[str, Any] = {
            "conn": None,
            "peer": None,
            "write_lock": threading.Lock(),
            "cv": threading.Condition(),
            "acks": {},
        }

        def _channel_reader(channel_conn: socket.socket) -> None:
            try:
                pending = bytearray()
                while True:
                    block = channel_conn.recv(65536)
                    if not block:
                        break
                    pending.extend(block)
                    if len(pending) > 4 * 1024 * 1024:
                        break
                    while b"\n" in pending:
                        line, _, rest = bytes(pending).partition(b"\n")
                        pending = bytearray(rest)
                        try:
                            message = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if message.get("op") == "issue-ack":
                            with provider_channel["cv"]:
                                provider_channel["acks"][message.get("request_id")] = message
                                provider_channel["cv"].notify_all()
            finally:
                with provider_channel["cv"]:
                    if provider_channel["conn"] is channel_conn:
                        provider_channel["conn"] = None
                        provider_channel["peer"] = None
                    provider_channel["cv"].notify_all()
                with contextlib.suppress(Exception):
                    channel_conn.close()

        while True:
            connection, _ = server.accept()
            # A local peer must not be able to monopolize the single
            # accept loop by opening a request and withholding its newline.
            # Long-lived provider channels clear this handshake deadline
            # once their complete envelope has been decoded.
            connection.settimeout(5.0)
            raw = bytearray()
            try:
                while b"\n" not in raw:
                    block = connection.recv(65536)
                    if not block:
                        break
                    raw.extend(block)
                    if len(raw) > 4 * 1024 * 1024:
                        raise BridgeError("bridge request exceeded 4 MiB")
            except Exception as exc:  # noqa: BLE001 - connection-local containment
                with connection:
                    _send_socket_response(connection, {"ok": False, "error": str(exc)})
                continue
            connection.settimeout(None)
            try:
                envelope = json.loads(bytes(raw).split(b"\n", 1)[0])
            except json.JSONDecodeError as exc:
                _record_handshake_refusal(
                    target,
                    state,
                    observed=None,
                    reason="connection-handshake-malformed",
                )
                with connection:
                    _send_socket_response(connection, {"ok": False, "error": str(exc)})
                continue
            observed_identity = (
                envelope.get("rendezvous_identity") if isinstance(envelope, dict) else None
            )
            command = envelope.get("request") if isinstance(envelope, dict) else None
            if observed_identity != identity or not isinstance(command, dict):
                _record_handshake_refusal(
                    target,
                    state,
                    observed=observed_identity,
                    reason="connection-handshake-identity-mismatch",
                )
                with connection:
                    _send_socket_response(
                        connection,
                        {
                            "ok": False,
                            "error": "connection-handshake-identity-mismatch",
                        },
                    )
                continue
            if isinstance(command, dict) and command.get("op") == "provider-channel":
                # The provider-originated channel: kernel-verified to the
                # live provider tree, single-bind, never closed by the
                # accept loop (the reader thread owns its lifetime).
                try:
                    if broker is None:
                        raise BridgeError("actor broker is unavailable for this generation")
                    peer = broker.verify_peer_lineage(connection)
                    with provider_channel["cv"]:
                        if provider_channel["conn"] is not None:
                            raise BridgeError(
                                "a provider-originated channel is already bound "
                                "for this generation"
                            )
                        provider_channel["conn"] = connection
                        provider_channel["peer"] = peer
                    threading.Thread(
                        target=_channel_reader, args=(connection,), daemon=True
                    ).start()
                    connection.sendall(
                        _canonical({"ok": True, "provider_channel": "bound"}) + b"\n"
                    )
                except Exception as exc:  # noqa: BLE001 - structured refusal
                    with contextlib.suppress(Exception):
                        connection.sendall(_canonical({"ok": False, "error": str(exc)}) + b"\n")
                    connection.close()
                continue
            if isinstance(command, dict) and command.get("op") == "actor-assertion":
                # Issuance may relay through the provider-originated
                # channel, which binds on a SEPARATE accepted connection.
                # Handling this op on its own thread keeps the accept loop
                # free to accept that channel (a relay waited on in the
                # loop itself would deadlock against the backlog).
                threading.Thread(
                    target=_handle_actor_assertion,
                    args=(connection, command, broker, provider_channel),
                    daemon=True,
                ).start()
                continue
            with connection:
                try:
                    if command.get("op") in {
                        "admit",
                        "deliver",
                        "session.op.begin",
                        "session.op.query",
                    }:
                        _authorize_operator_peer(connection, request)
                    if command.get("op") == "status":
                        response = {"ok": True, **state}
                    elif command.get("op") == "admit":
                        if state["submission"] is not None:
                            if state.get("admission_request_sha256") != _digest(command):
                                raise BridgeError("bridge already admitted a different task")
                            receipt = state["submission"]
                        else:
                            # Delivery journal: the durable intent lands
                            # BEFORE any provider I/O; submit/ack straddle
                            # the provider call and the state persistence.
                            obligation = request.get("obligation_generation")
                            delivery_id = command.get("delivery_id")
                            journaled = (
                                bool(obligation)
                                and isinstance(delivery_id, str)
                                and bool(delivery_id)
                            )
                            if journaled:
                                if journal is None:
                                    from cli_agent_orchestrator.services.delivery_journal import (
                                        DeliveryJournal,
                                    )

                                    journal = DeliveryJournal(
                                        target["root"] / "delivery-journal.db"
                                    )
                                    _mark_bridge_journal_created(target, request)
                                journal.open_intent(obligation, delivery_id, _digest(command))
                                journal.mark_terminal_queued(obligation, delivery_id)
                            try:
                                receipt = session.admit(command)
                            except SubmitUncertain as exc:
                                # Response loss after the provider boundary:
                                # the provider may have accepted. Record the
                                # ambiguity durably BEFORE returning the
                                # error; never downgrade to terminal_queued,
                                # never replay, never assert non-submission.
                                if journaled:
                                    journal.mark_submit_ambiguous(
                                        obligation,
                                        delivery_id,
                                        evidence_digest=_digest(
                                            {
                                                "kind": "submit-ambiguous",
                                                "command_sha256": _digest(command),
                                                "error": str(exc),
                                            }
                                        ),
                                    )
                                raise BridgeError(
                                    "provider admission outcome uncertain; "
                                    f"recorded submit-ambiguous: {exc}"
                                ) from exc
                            if journaled:
                                journal.mark_submitted(obligation, delivery_id)
                            _write_route_receipt(session, request, command, receipt, delivery_id)
                            state.update(
                                {
                                    "state": "admitted",
                                    "submission": receipt,
                                    "admission_request_sha256": _digest(command),
                                }
                            )
                            _atomic_json(target["state"], state)
                            if journaled:
                                journal.mark_submit_acked(obligation, delivery_id)
                            print(
                                f"[managed-provider-admitted] delivery={receipt['delivery_id']} "
                                f"turn={receipt['provider_turn_id']}",
                                flush=True,
                            )
                        response = {"ok": True, "receipt": receipt}
                    elif command.get("op") == "deliver":
                        # P1-7 (§20.2f): exact provider-native inbox message
                        # submission; the acknowledgement is recorded by
                        # deliver_inbox into the companion store.
                        # V1 and v2 both need an at-most-once retry boundary.
                        # V2 has an explicit obligation generation; the exact
                        # terminal generation is the equivalent v1 identity.
                        obligation = request.get("obligation_generation") or request["generation"]
                        message_id = command.get("message_id")
                        journaled = (
                            bool(obligation) and isinstance(message_id, str) and bool(message_id)
                        )
                        if journaled:
                            if journal is None:
                                from cli_agent_orchestrator.services.delivery_journal import (
                                    DeliveryJournal,
                                )

                                journal = DeliveryJournal(target["root"] / "delivery-journal.db")
                                _mark_bridge_journal_created(target, request)
                            existing = None
                            with contextlib.suppress(Exception):
                                existing = journal.get(obligation, message_id)
                            ack = companion_receipts.get_message_ack(
                                request["terminal_id"],
                                request["generation"],
                                message_id,
                            )
                            if ack is not None:
                                if ack.get("message_sha256") != command.get("message_sha256"):
                                    raise BridgeError(
                                        "existing provider acknowledgement is bound to "
                                        "different inbox bytes"
                                    )
                                # Reconcile a response-loss prefix from durable
                                # provider evidence without submitting again.
                                if existing is None:
                                    journal.open_intent(obligation, message_id, _digest(command))
                                    journal.mark_terminal_queued(obligation, message_id)
                                    journal.mark_submitted(obligation, message_id)
                                    journal.mark_submit_acked(obligation, message_id)
                                elif existing["state"] == "terminal_queued":
                                    journal.mark_submitted(obligation, message_id)
                                    journal.mark_submit_acked(obligation, message_id)
                                elif existing["state"] == "submitted":
                                    journal.mark_submit_acked(obligation, message_id)
                                response = {"ok": True, "receipt": ack}
                                _send_socket_response(connection, response)
                                continue
                            if existing is not None:
                                if existing["state"] == "submit-refused":
                                    journal.mark_terminal_queued(obligation, message_id)
                                elif existing["state"] in {"terminal_queued", "submitted"}:
                                    journal.mark_submit_ambiguous(
                                        obligation,
                                        message_id,
                                        evidence_digest=_digest(
                                            {
                                                "kind": "restart-without-provider-ack",
                                                "prior_state": existing["state"],
                                                "command_sha256": _digest(command),
                                            }
                                        ),
                                    )
                                    recovery_key = command.get("recovery_operation_key")
                                    if isinstance(recovery_key, str) and recovery_key:
                                        from cli_agent_orchestrator.services import (
                                            callback_recovery,
                                        )

                                        callback_recovery.mark_delivery_ambiguous(
                                            recovery_key,
                                            reason_code=(
                                                "provider-submission-ambiguous-"
                                                "manual-resolution-required"
                                            ),
                                        )
                                    raise BridgeError(
                                        "inbox delivery restart found a provider "
                                        f"boundary state {existing['state']!r} without "
                                        "an acknowledgement; recorded submit-ambiguous "
                                        "and manual resolution is required"
                                    )
                                else:
                                    raise BridgeError(
                                        "inbox delivery already crossed its durable boundary "
                                        f"with state {existing['state']!r}; refusing blind replay"
                                    )
                            else:
                                journal.open_intent(obligation, message_id, _digest(command))
                                journal.mark_terminal_queued(obligation, message_id)
                        try:
                            receipt = session.deliver_inbox(command)
                        except SessionOperationRefused as exc:
                            if journaled:
                                journal.mark_submit_refused(
                                    obligation,
                                    message_id,
                                    evidence_digest=_digest(
                                        {
                                            "kind": "provider-pre-submit-refusal",
                                            "code": exc.code,
                                            "detail": exc.detail,
                                        }
                                    ),
                                )
                            raise BridgeError(f"{exc.code}: {exc.detail}") from exc
                        except SubmitUncertain as exc:
                            # Response loss after the provider boundary:
                            # record submit-ambiguous durably before
                            # returning the error; never replay blindly.
                            if journaled:
                                journal.mark_submit_ambiguous(
                                    obligation,
                                    message_id,
                                    evidence_digest=_digest(
                                        {
                                            "kind": "submit-ambiguous",
                                            "command_sha256": _digest(command),
                                            "error": str(exc),
                                        }
                                    ),
                                )
                            recovery_key = command.get("recovery_operation_key")
                            if isinstance(recovery_key, str) and recovery_key:
                                from cli_agent_orchestrator.services import callback_recovery

                                callback_recovery.mark_delivery_ambiguous(
                                    recovery_key,
                                    reason_code=(
                                        "provider-submission-ambiguous-"
                                        "manual-resolution-required"
                                    ),
                                )
                            raise BridgeError(
                                "inbox delivery outcome uncertain; "
                                f"recorded submit-ambiguous: {exc}"
                            ) from exc
                        if journaled:
                            journal.mark_submitted(obligation, message_id)
                            journal.mark_submit_acked(obligation, message_id)
                        _write_route_receipt(session, request, command, receipt, message_id)
                        response = {"ok": True, "receipt": receipt}
                    elif command.get("op") == "session.op.begin":
                        operation_id = command.get("operation_id")
                        action = command.get("action")
                        if not isinstance(operation_id, str) or not operation_id:
                            raise BridgeError("session operation omitted operation_id")
                        if not isinstance(action, str) or not action:
                            raise BridgeError("session operation omitted action")
                        if any(
                            command.get(key) != request[key]
                            for key in ("reservation_id", "terminal_id", "generation")
                        ):
                            raise BridgeError(
                                "session operation does not match the exact bridge generation"
                            )
                        if control_journal is None:
                            control_journal = SessionControlJournal(
                                target["root"] / "session-control-journal.db"
                            )
                            _mark_control_journal_created(target, request)
                        operation = control_journal.begin(
                            operation_id=operation_id,
                            terminal_id=request["terminal_id"],
                            generation=request["generation"],
                            action=action,
                            request_sha256=_digest(command),
                            provider=request["provider"],
                            provider_session_id=str(session.provider_session_id),
                        )
                        if (
                            operation["state"] == CONTROL_QUEUED
                            and state["submission"] is None
                            and action in {"follow-up", "compact", "route-set"}
                        ):
                            receipt = session._refuse_control(
                                control_journal,
                                operation_id,
                                "task_not_admitted",
                                "mutating controls are unavailable until the reserved "
                                "task has a provider-native admission receipt",
                            )
                        elif operation["state"] == CONTROL_QUEUED:
                            try:
                                receipt = session.session_operation(command, control_journal)
                            except Exception as exc:  # noqa: BLE001 - journal exact outcome
                                current = control_journal.get(operation_id)
                                if current["state"] == CONTROL_QUEUED:
                                    receipt = session._refuse_control(
                                        control_journal,
                                        operation_id,
                                        "generation_fenced",
                                        str(exc),
                                    )
                                elif current["state"] in {
                                    CONTROL_SUBMITTED,
                                    CONTROL_ACCEPTED,
                                }:
                                    receipt = session._control_receipt(
                                        control_journal.transition(
                                            operation_id,
                                            CONTROL_AMBIGUOUS,
                                            reason_code="control_outcome_ambiguous",
                                            reason_detail=str(exc),
                                        )
                                    )
                                else:
                                    receipt = session._control_receipt(current)
                        else:
                            receipt = session.reconcile_session_operation(
                                control_journal, operation_id
                            )
                        response = {"ok": True, "receipt": receipt}
                    elif command.get("op") == "session.op.query":
                        operation_id = command.get("operation_id")
                        if not isinstance(operation_id, str) or not operation_id:
                            raise BridgeError("session operation query omitted operation_id")
                        if any(
                            command.get(key) != request[key]
                            for key in ("reservation_id", "terminal_id", "generation")
                        ):
                            raise BridgeError(
                                "session operation query does not match the exact bridge generation"
                            )
                        journal_path = target["root"] / "session-control-journal.db"
                        if control_journal is None:
                            if not journal_path.exists():
                                raise BridgeError("unknown managed-session operation")
                            control_journal = SessionControlJournal(journal_path)
                        receipt = session.reconcile_session_operation(control_journal, operation_id)
                        response = {"ok": True, "receipt": receipt}
                    else:
                        raise BridgeError("unsupported managed bridge operation")
                except Exception as exc:  # noqa: BLE001 - structured socket failure
                    response = {"ok": False, "error": str(exc)}
                _send_socket_response(connection, response)
    except Exception as exc:  # noqa: BLE001 - persist fail-closed state
        failure = _launch_failure(
            request,
            exc,
            environment_inventory,
            provider_io_started=bool(
                session is not None
                and (
                    getattr(session, "provider_io_started", False)
                    or getattr(session, "rpc", None) is not None
                )
            ),
        )
        state.update(
            {
                "state": "launch-failed-bridge",
                "error": str(exc),
                "launch_failure": failure,
            }
        )
        preserve_live_duplicate = False
        if not claimed and str(exc) == "duplicate-live-bridge-identity":
            try:
                record = _read_binding_record(target["binding"])
                preserve_live_duplicate = record["binding_identity"] == binding_identity(request)
            except BridgeError:
                pass
        if not preserve_live_duplicate:
            _atomic_json(target["state"], state)
        print(f"[managed-provider-blocked] {exc}", file=sys.stderr, flush=True)
        blocked = True
        return 1
    finally:
        server.close()
        if session is not None:
            session.close()
        if claimed:
            # Controlled failure keeps the state file as the launcher's
            # diagnostic (its row stays closed, present and discoverable);
            # every other resource converges to real absence.
            _deregister_bridge_resources(target, request, retain_state=blocked)
        if claim_descriptor is not None:
            os.close(claim_descriptor)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reservation-id", required=True)
    args = parser.parse_args(argv)
    target = paths(args.reservation_id)
    request = json.loads(target["request"].read_text(encoding="utf-8"))
    if request.get("reservation_id") != args.reservation_id:
        raise BridgeError("bridge request reservation identity mismatch")
    # The provider is pinned in the immutable request.  Compose and scrub at
    # this actual pane-process launch boundary before the unchanged guard in
    # _serve; foreign supervisor controls never reach the target provider.
    environment_inventory = _bind_bridge_environment(request)
    return _serve(request, target, environment_inventory=environment_inventory)


if __name__ == "__main__":
    raise SystemExit(main())
