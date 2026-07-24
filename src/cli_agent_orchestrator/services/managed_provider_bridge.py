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
import hashlib
import json
import logging
import os
import pathlib
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from cli_agent_orchestrator.constants import CAO_HOME_DIR, SECURITY_PROMPT
from cli_agent_orchestrator.providers.codex import (
    _toml_override,
    _toml_scalar,
    _validate_config_key,
    render_trusted_project_override,
)
from cli_agent_orchestrator.services import companion_receipts
from cli_agent_orchestrator.services.codex_trust import (
    SUPPORTED_CODEX_VERSION,
    _contains_session_flags,
)
from cli_agent_orchestrator.services.kimi_route import SUPPORTED_KIMI_VERSION, _current_option
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.skills import build_skill_catalog
from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

logger = logging.getLogger(__name__)

BRIDGE_VERSION = "cao-native-provider-bridge-v1"
BRIDGE_ROOT = CAO_HOME_DIR / "managed-provider-sessions"

# P1-9 (spec §20.2d(7)): provider/bridge child processes run under a MINIMAL
# allowlisted environment built fresh — never the ambient server/tmux
# environment. Protected conductor state, quota-bypass, and route-control
# variables are rejected; PATH is a fixed minimal value, not inherited.
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
        "KIMI_CODE_HOME",
    }
)
_MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

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


def _prune_bridge_environment() -> None:
    """Replace the bridge's OWN environment with the fresh minimal allowlist.

    P1-9 (final conformance §20.2f): the bridge process must not inherit
    unrelated ambient variables merely because its provider child is pruned.
    After this runs, the bridge and everything it spawns see only the
    allowlisted variables and the fixed minimal PATH. Runs after
    ``_assert_bridge_environment`` so a protected leak still fails closed
    (with the offending names) instead of being silently dropped."""
    fresh = _provider_env()
    os.environ.clear()
    os.environ.update(fresh)


def _provider_env(overrides: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The minimal allowlisted environment for provider child processes."""
    env = {name: os.environ[name] for name in _PROVIDER_ENV_ALLOWLIST if name in os.environ}
    env["PATH"] = _MINIMAL_PATH
    env.update(overrides or {})
    return env


def _launcher_argv(socket_path: pathlib.Path, provider_argv: list[str]) -> list[str]:
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


def paths(reservation_id: str) -> dict[str, pathlib.Path]:
    root = BRIDGE_ROOT / reservation_id
    return {
        "root": root,
        "request": root / "request.json",
        "state": root / "state.json",
        "socket": root / "bridge.sock",
    }


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
    target = paths(reservation_id)
    target["root"].mkdir(mode=0o700, parents=True, exist_ok=True)
    if target["request"].exists():
        existing = json.loads(target["request"].read_text(encoding="utf-8"))
        if existing != request:
            raise BridgeError("managed provider request identity changed")
    else:
        _atomic_json(target["request"], request)
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


def request_bridge(
    reservation_id: str, request: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    socket_path = paths(reservation_id)["socket"]
    deadline = time.monotonic() + timeout
    response: Any = None
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            client.connect(str(socket_path))
            client.sendall(_canonical(request) + b"\n")
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
            if state and state.get("state") == "preflight_blocked":
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
                    print(line.rstrip(), flush=True)
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
                        print(json.dumps(item, sort_keys=True), flush=True)
                    self._condition.notify_all()
        finally:
            with self._condition:
                self._closed_error = f"provider native process exited {self.proc.poll()}"
                self._condition.notify_all()

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            print(f"[provider stderr] {line.rstrip()}", flush=True)

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
            "model": self.request["model"],
            "effort": self.request["effort"],
            "working_directory": self.request["working_directory"],
        }

    def initialize(self) -> dict[str, Any]:
        if self.provider == "codex":
            return self._initialize_codex()
        if self.provider == "kimi_cli":
            return self._initialize_kimi()
        raise BridgeError(f"unsupported managed provider {self.provider!r}")

    def _version(self, executable: str, expected: str) -> str:
        if not os.path.isabs(executable) or os.path.realpath(executable) != executable:
            raise BridgeError("provider executable must be a canonical absolute path")
        if (
            _file_digest_or_absent(pathlib.Path(executable))
            != self.request["provider_executable_sha256"]
        ):
            raise BridgeError("provider executable digest changed after reservation")
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            env=_provider_env(),
        )
        actual = proc.stdout.strip()
        if proc.returncode != 0 or actual != expected:
            raise BridgeError(f"unsupported provider version {actual!r}; expected {expected!r}")
        return actual

    def _initialize_codex(self) -> dict[str, Any]:
        codex_bin = self.request["provider_executable"]
        version = self._version(codex_bin, SUPPORTED_CODEX_VERSION)
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
        argv = _launcher_argv(paths(self.request["reservation_id"])["socket"], argv)
        config_path = pathlib.Path(os.path.expanduser("~/.codex/config.toml"))
        config_before = _file_digest_or_absent(config_path)
        self.rpc = _RpcProcess(
            argv, env=_provider_env(), companion_identity=self._companion_identity()
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
        version = self._version(kimi_bin, SUPPORTED_KIMI_VERSION)
        # Route control (thinking effort) comes ONLY from the reservation
        # request, applied over the minimal allowlisted environment.
        env = _provider_env({"KIMI_MODEL_THINKING_EFFORT": self.request["effort"]})
        self.rpc = _RpcProcess(
            _launcher_argv(paths(self.request["reservation_id"])["socket"], [kimi_bin, "acp"]),
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
        if (
            _current_option(options, category="thought_level", option_id="thinking")
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
        if (
            _current_option(options, category="model", option_id="model") != self.request["model"]
            or _current_option(options, category="thought_level", option_id="thinking")
            != self.request["effort"]
        ):
            raise BridgeError("Kimi exact session resolved the wrong route")
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
                "model": self.request["model"],
                "effort": self.request["effort"],
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
            raise BridgeError(str(exc)) from exc
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
        if command.get("reservation_id") != self.request["reservation_id"]:
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
        except generation_fence.FencedError as exc:
            raise BridgeError(str(exc)) from exc
        submitted_at = _now()
        companion_receipts.record_message_ack(
            self.request["terminal_id"],
            self.request["generation"],
            message_id=message_id,
            ack={
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
            },
        )
        # The per-turn route identity (§18.9) moves to this exact turn.
        companion_receipts.record_route_receipt(
            self.request["terminal_id"],
            self.request["generation"],
            provider=self.provider,
            model=self.request["model"],
            effort=self.request["effort"],
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

    def close(self) -> None:
        if self.rpc is not None:
            self.rpc.close()

    def _admission_critical_section(self):
        """The fence lock held across the final recheck and provider I/O."""
        from cli_agent_orchestrator.constants import COMPANION_DIR
        from cli_agent_orchestrator.services import generation_fence

        return generation_fence.admission_critical_section(
            COMPANION_DIR, self.request["terminal_id"], self.request["generation"]
        )

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


def _register_bridge_resources(target: dict[str, pathlib.Path], request: dict[str, Any]) -> None:
    """Registry-first registration of the bridge's own v2 resources.

    The generation-private socket, the bridge state tree, and the
    delivery journal are declared before the accept loop is exposed, so
    cleanup/monitors/inventory see them through the registry alone.  An
    entry is marked ``created`` ONLY when its exact filesystem identity is
    observed to exist (the socket is bound and the state tree written
    before this runs; the delivery journal is created lazily and marked
    by ``_mark_bridge_journal_created`` at construction).  Re-registration
    after a crash converges on the live entry for the same generation —
    including promoting a still-declared entry whose file now exists —
    instead of conflicting.
    """
    from cli_agent_orchestrator.services import resource_registry as rr

    registry = rr.get_resource_registry()
    reservation_id = request["reservation_id"]
    actor = "managed_provider_bridge._serve"
    entries = (
        ("socket", f"{reservation_id}/bridge.sock", str(target["socket"])),
        (
            "bridge_state",
            f"managed-provider-sessions/{reservation_id}",
            str(target["root"]),
        ),
        (
            "db_row_set",
            f"{reservation_id}/delivery-journal.db",
            str(target["root"] / "delivery-journal.db"),
        ),
    )
    for kind, entry_id, fs_path in entries:
        existing = registry.resolve_fs_path(fs_path)
        if existing is not None and existing["generation"] == request["generation"]:
            if existing["lifecycle_state"] == "declared" and pathlib.Path(fs_path).exists():
                registry.register_created(
                    entry_id,
                    actor_id=actor,
                    observed={"observed_fs_path": fs_path},
                    existence_receipt_digest=rr.receipt_digest(
                        {"entry_id": entry_id, "observed_fs_path": fs_path}
                    ),
                )
            continue  # converge after a crash: the live entry is already ours
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
        )
        if pathlib.Path(fs_path).exists():
            registry.register_created(
                entry_id,
                actor_id=actor,
                observed={"observed_fs_path": fs_path},
                existence_receipt_digest=rr.receipt_digest(
                    {"entry_id": entry_id, "observed_fs_path": fs_path}
                ),
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


def _deregister_bridge_resources(target: dict[str, pathlib.Path], request: dict[str, Any]) -> None:
    """Drain/close/delete the bridge's registry entries, truthfully.

    A still-declared entry is aborted ONLY on a verified-absence probe; a
    created entry is drained/closed, its physical artifact (socket, state
    tree, journal DB and WAL/SHM siblings) is actually removed, and it is
    marked deleted ONLY after a real absence check — a resource that is
    still present keeps its row instead of a synthesized absence claim.
    """
    from cli_agent_orchestrator.services import resource_registry as rr

    actor = "managed_provider_bridge._serve"
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
        with contextlib.suppress(OSError):
            path.unlink()
        with contextlib.suppress(OSError):
            path.with_name(path.name + "-wal").unlink()
        with contextlib.suppress(OSError):
            path.with_name(path.name + "-shm").unlink()
        if path.is_dir():
            import shutil

            shutil.rmtree(path, ignore_errors=True)

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
                if fs_path and pathlib.Path(fs_path).exists():
                    # Created but never receipt-marked: discover, then drain.
                    registry.register_created(
                        entry_id,
                        actor_id=actor,
                        observed={"observed_fs_path": fs_path},
                        existence_receipt_digest=rr.receipt_digest(
                            {"entry_id": entry_id, "observed_fs_path": fs_path}
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
            if fs_path:
                _remove(fs_path)
            if not fs_path or not pathlib.Path(fs_path).exists():
                registry.delete(entry_id, actor_id=actor, verified_absence_digest=absence)
            else:
                logger.warning(
                    "bridge resource %s still present after teardown; row retained", entry_id
                )
        except Exception:  # noqa: BLE001 - best-effort teardown
            logger.warning("bridge resource %s deregistration failed", entry_id, exc_info=True)


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
            expected_model=request["model"],
            expected_effort=request["effort"],
            observed_model=request["model"],
            observed_effort=request["effort"],
            protocol=route_receipts.protocol_version(provider),
            event_sequence=session._turn_sequence,
            model_input_digest=_digest(command),
            provider_version=str((session.readiness or {}).get("provider_version") or ""),
        )
    except Exception:  # noqa: BLE001 - receipt loss means no authority, never a wedge
        logger.warning("route receipt publication failed", exc_info=True)


def _serve(request: dict[str, Any], target: dict[str, pathlib.Path]) -> int:
    state = {
        "bridge_version": BRIDGE_VERSION,
        "request_sha256": _digest(request),
        "state": "starting",
        "first_seen_at": time.time(),
        "readiness": None,
        "submission": None,
    }
    _atomic_json(target["state"], state)
    session: Optional[_ProviderSession] = None
    registered = False
    with contextlib.suppress(FileNotFoundError):
        target["socket"].unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        session = _ProviderSession(request)
        server.bind(str(target["socket"]))
        os.chmod(target["socket"], 0o600)
        server.listen(8)
        readiness = session.initialize()
        session._scan_companion_events()
        state.update({"state": "ready", "readiness": readiness})
        _atomic_json(target["state"], state)
        # Lane-B production wiring: the generation-private UDS accept path
        # is the actor broker's issuance boundary, and the delivery journal
        # records intent/submit/ack transitions around the real provider
        # call. Neither capability existed before this wiring.
        broker = _build_actor_broker(request, session)
        journal: Any = None
        # Registry-first: the bridge's own socket/state/journal resources
        # are declared and receipt-marked before the accept loop is
        # exposed (fail-closed, like the v2 terminal constructor).
        _register_bridge_resources(target, request)
        registered = True
        print(
            f"[managed-provider-ready] provider={request['provider']} "
            f"session={readiness['provider_session_id']} generation={request['generation']}",
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
            raw = bytearray()
            while b"\n" not in raw:
                block = connection.recv(65536)
                if not block:
                    break
                raw.extend(block)
                if len(raw) > 4 * 1024 * 1024:
                    connection.close()
                    raise BridgeError("bridge request exceeded 4 MiB")
            try:
                command = json.loads(bytes(raw).split(b"\n", 1)[0])
            except json.JSONDecodeError as exc:
                with connection:
                    connection.sendall(_canonical({"ok": False, "error": str(exc)}) + b"\n")
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
                        obligation = request.get("obligation_generation")
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
                            journal.open_intent(obligation, message_id, _digest(command))
                            journal.mark_terminal_queued(obligation, message_id)
                        try:
                            receipt = session.deliver_inbox(command)
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
                            raise BridgeError(
                                "inbox delivery outcome uncertain; "
                                f"recorded submit-ambiguous: {exc}"
                            ) from exc
                        if journaled:
                            journal.mark_submitted(obligation, message_id)
                            journal.mark_submit_acked(obligation, message_id)
                        _write_route_receipt(session, request, command, receipt, message_id)
                        response = {"ok": True, "receipt": receipt}
                    else:
                        raise BridgeError("unsupported managed bridge operation")
                except Exception as exc:  # noqa: BLE001 - structured socket failure
                    response = {"ok": False, "error": str(exc)}
                connection.sendall(_canonical(response) + b"\n")
    except Exception as exc:  # noqa: BLE001 - persist fail-closed state
        state.update({"state": "preflight_blocked", "error": str(exc)})
        _atomic_json(target["state"], state)
        print(f"[managed-provider-blocked] {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        server.close()
        with contextlib.suppress(FileNotFoundError):
            target["socket"].unlink()
        if session is not None:
            session.close()
        if registered:
            _deregister_bridge_resources(target, request)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reservation-id", required=True)
    args = parser.parse_args(argv)
    _assert_bridge_environment()
    _prune_bridge_environment()
    target = paths(args.reservation_id)
    request = json.loads(target["request"].read_text(encoding="utf-8"))
    if request.get("reservation_id") != args.reservation_id:
        raise BridgeError("bridge request reservation identity mismatch")
    return _serve(request, target)


if __name__ == "__main__":
    raise SystemExit(main())
