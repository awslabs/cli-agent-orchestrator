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
import os
import pathlib
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.constants import SECURITY_PROMPT
from cli_agent_orchestrator.providers.codex import (
    _toml_override,
    _toml_scalar,
    _validate_config_key,
    render_trusted_project_override,
)
from cli_agent_orchestrator.services.codex_trust import (
    SUPPORTED_CODEX_VERSION,
    _contains_session_flags,
)
from cli_agent_orchestrator.services.kimi_route import SUPPORTED_KIMI_VERSION, _current_option
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.skills import build_skill_catalog
from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

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


def _provider_env(overrides: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The minimal allowlisted environment for provider child processes."""
    env = {name: os.environ[name] for name in _PROVIDER_ENV_ALLOWLIST if name in os.environ}
    env["PATH"] = _MINIMAL_PATH
    env.update(overrides or {})
    return env


class BridgeError(RuntimeError):
    pass


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
    def __init__(self, argv: list[str], *, env: Optional[dict[str, str]] = None):
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
        config_path = pathlib.Path(os.path.expanduser("~/.codex/config.toml"))
        config_before = _file_digest_or_absent(config_path)
        self.rpc = _RpcProcess(argv, env=_provider_env())
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
        self.rpc = _RpcProcess([kimi_bin, "acp"], env=env)
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
        provider_turn_id: Optional[str] = None
        if self.provider == "codex":
            params = {
                "threadId": self.provider_session_id,
                "input": [{"type": "text", "text": request["message"]}],
                "clientUserMessageId": request["delivery_id"],
                "model": self.request["model"],
                "effort": self.request["effort"],
                "cwd": self.request["working_directory"],
                "approvalPolicy": "never",
            }
            result = self.rpc.request("turn/start", params, timeout=30.0)
            turn = result.get("turn") or {}
            turn_id = turn.get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise BridgeError("Codex turn/start omitted provider turn id")
            provider_evidence = {
                "method": "turn/start",
                "request_sha256": _digest(params),
                "response": result,
            }
            receipt_id = turn_id
            provider_turn_id = turn_id
            kind = "codex-turn-start"
        else:
            if self.kimi_wire_path is None:
                raise BridgeError("Kimi exact session journal is unavailable")
            params = {
                "sessionId": self.provider_session_id,
                "prompt": [{"type": "text", "text": request["message"]}],
                "_meta": {
                    "caoReservationId": request["reservation_id"],
                    "caoGeneration": request["generation"],
                    "caoMessageSha256": request["message_sha256"],
                    "caoContextSha256": _digest(request.get("context") or {}),
                },
            }
            wire_offset = self.kimi_wire_path.stat().st_size
            start_index = self.rpc.notification_count()
            rpc_id = self.rpc.start_request("session/prompt", params)
            turn_start = _wait_kimi_turn_start(
                self.kimi_wire_path, start_offset=wire_offset, timeout=30.0
            )

            def accepted(item: dict[str, Any]) -> bool:
                if item.get("method") != "session/update":
                    return False
                update = item.get("params") or {}
                return update.get("sessionId") == self.provider_session_id

            first_update = self.rpc.wait_notification(
                accepted, start_index=start_index, timeout=30.0
            )
            provider_turn_id = turn_start["uuid"]
            receipt_id = provider_turn_id
            provider_evidence = {
                "method": "session/prompt",
                "request_sha256": _digest(params),
                "provider_turn_start": turn_start,
                "first_provider_update": first_update,
                "provider_request_id": rpc_id,
            }
            kind = "kimi-session-update"
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

    def close(self) -> None:
        if self.rpc is not None:
            self.rpc.close()


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
    with contextlib.suppress(FileNotFoundError):
        target["socket"].unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        session = _ProviderSession(request)
        server.bind(str(target["socket"]))
        os.chmod(target["socket"], 0o600)
        server.listen(8)
        readiness = session.initialize()
        state.update({"state": "ready", "readiness": readiness})
        _atomic_json(target["state"], state)
        print(
            f"[managed-provider-ready] provider={request['provider']} "
            f"session={readiness['provider_session_id']} generation={request['generation']}",
            flush=True,
        )
        while True:
            connection, _ = server.accept()
            with connection:
                raw = bytearray()
                while b"\n" not in raw:
                    block = connection.recv(65536)
                    if not block:
                        break
                    raw.extend(block)
                    if len(raw) > 4 * 1024 * 1024:
                        raise BridgeError("bridge request exceeded 4 MiB")
                try:
                    command = json.loads(bytes(raw).split(b"\n", 1)[0])
                    if command.get("op") == "status":
                        response = {"ok": True, **state}
                    elif command.get("op") == "admit":
                        if state["submission"] is not None:
                            if state.get("admission_request_sha256") != _digest(command):
                                raise BridgeError("bridge already admitted a different task")
                            receipt = state["submission"]
                        else:
                            receipt = session.admit(command)
                            state.update(
                                {
                                    "state": "admitted",
                                    "submission": receipt,
                                    "admission_request_sha256": _digest(command),
                                }
                            )
                            _atomic_json(target["state"], state)
                            print(
                                f"[managed-provider-admitted] delivery={receipt['delivery_id']} "
                                f"turn={receipt['provider_turn_id']}",
                                flush=True,
                            )
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
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reservation-id", required=True)
    args = parser.parse_args(argv)
    _assert_bridge_environment()
    target = paths(args.reservation_id)
    request = json.loads(target["request"].read_text(encoding="utf-8"))
    if request.get("reservation_id") != args.reservation_id:
        raise BridgeError("bridge request reservation identity mismatch")
    return _serve(request, target)


if __name__ == "__main__":
    raise SystemExit(main())
