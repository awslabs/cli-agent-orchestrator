"""Version-bound, zero-prompt Kimi model and effort attestation."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import select
import subprocess
import time
from typing import Any, Optional, cast

SUPPORTED_KIMI_VERSION = "0.29.0"
PROBE_VERSION = "kimi-acp-route-v1"


class KimiRouteProbeError(RuntimeError):
    pass


def _digest_or_absent(path: pathlib.Path) -> str:
    if not path.exists():
        return "absent"
    if not path.is_file() or path.is_symlink():
        raise KimiRouteProbeError("protected Kimi config is not a regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _AcpClient:
    def __init__(self, argv: list[str], env: dict[str, str], timeout: float):
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise KimiRouteProbeError(f"could not start Kimi ACP server: {exc}") from exc
        if self.proc.stdin is None or self.proc.stdout is None or self.proc.stderr is None:
            self.proc.kill()
            raise KimiRouteProbeError("Kimi ACP server pipes were not created")
        self.stdin = self.proc.stdin
        self.stdout = self.proc.stdout
        self.stderr = self.proc.stderr
        self.deadline = time.monotonic() + timeout
        self.next_id = 1

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        self.stdin.write(json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")
        self.stdin.flush()
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise KimiRouteProbeError(
                    f"Kimi ACP server timed out awaiting response id {request_id}"
                )
            readable, _, _ = select.select([self.stdout], [], [], remaining)
            if not readable:
                raise KimiRouteProbeError(
                    f"Kimi ACP server timed out awaiting response id {request_id}"
                )
            line = self.stdout.readline()
            if not line:
                raise KimiRouteProbeError(f"Kimi ACP server ended before response id {request_id}")
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("id") == request_id:
                if "error" in item or "result" not in item:
                    raise KimiRouteProbeError(f"Kimi ACP {method} failed: {item.get('error')!r}")
                return cast(dict[str, Any], item["result"] or {})

    def close(self) -> tuple[int, str]:
        try:
            self.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)
        return self.proc.returncode, self.stderr.read()


def _current_option(options: Any, *, category: str, option_id: str) -> Optional[str]:
    if not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        if option.get("category") == category or option.get("id") == option_id:
            value = option.get("currentValue")
            return value if isinstance(value, str) else None
    return None


def attest_kimi_route(
    project_root: str,
    *,
    expected_model: str,
    expected_effort: str,
    timeout: float = 20.0,
    kimi_bin: str = "kimi",
    user_config_path: Optional[pathlib.Path] = None,
) -> dict[str, Any]:
    """Return a provider-native Kimi route receipt without sending a prompt.

    Kimi ACP creates a zero-prompt session, reports its model and thought-level
    configuration, and can select the exact requested values through structured
    protocol methods.  The eventual terminal invocation is separately forced
    with the same model argument and effort environment override.
    """
    if not os.path.isdir(project_root) or os.path.realpath(project_root) != project_root:
        raise KimiRouteProbeError("project_root must be an existing canonical directory")

    try:
        version_proc = subprocess.run(
            [kimi_bin, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KimiRouteProbeError(f"could not execute Kimi version probe: {exc}") from exc
    version = version_proc.stdout.strip()
    if version_proc.returncode != 0 or version != SUPPORTED_KIMI_VERSION:
        raise KimiRouteProbeError(
            f"unsupported Kimi version {version!r}; expected {SUPPORTED_KIMI_VERSION!r}"
        )

    config_path = user_config_path or pathlib.Path(os.path.expanduser("~/.kimi/config.toml"))
    before = _digest_or_absent(config_path)
    env = dict(os.environ)
    env["KIMI_MODEL_THINKING_EFFORT"] = expected_effort
    client = _AcpClient([kimi_bin, "acp"], env, timeout)
    probe_error: Optional[Exception] = None
    result: dict[str, Any] = {}
    returncode = -1
    stderr = ""
    try:
        initialized = client.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "cao-managed-launch", "version": "1"},
            },
        )
        session = client.request("session/new", {"cwd": project_root, "mcpServers": []})
        session_id = session.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise KimiRouteProbeError("Kimi ACP session/new omitted sessionId")
        options = session.get("configOptions")
        if _current_option(options, category="model", option_id="model") != expected_model:
            changed = client.request(
                "session/set_config_option",
                {
                    "sessionId": session_id,
                    "configId": "model",
                    "value": expected_model,
                },
            )
            options = changed.get("configOptions")
        if (
            _current_option(options, category="thought_level", option_id="thinking")
            != expected_effort
        ):
            changed = client.request(
                "session/set_config_option",
                {
                    "sessionId": session_id,
                    "configId": "thinking",
                    "value": expected_effort,
                },
            )
            options = changed.get("configOptions")

        actual_model = _current_option(options, category="model", option_id="model")
        actual_effort = _current_option(options, category="thought_level", option_id="thinking")
        if actual_model != expected_model:
            raise KimiRouteProbeError(
                f"Kimi ACP resolved model {actual_model!r}, expected {expected_model!r}"
            )
        if actual_effort != expected_effort:
            raise KimiRouteProbeError(
                f"Kimi ACP resolved effort {actual_effort!r}, expected {expected_effort!r}"
            )
        agent_info = initialized.get("agentInfo") or {}
        if agent_info.get("version") != version:
            raise KimiRouteProbeError("Kimi ACP agent version disagrees with executable version")
        result = {
            "probe_version": PROBE_VERSION,
            "kimi_version": version,
            "project_root": project_root,
            "model": actual_model,
            "reasoning_effort": actual_effort,
            "acp_protocol_version": initialized.get("protocolVersion"),
            "probe_session_id": session_id,
            "route_source": "acp-session-config",
            "protected_config_sha256": before,
            "terminal_model_argv": ["--model", expected_model],
            "terminal_effort_env": {"KIMI_MODEL_THINKING_EFFORT": expected_effort},
            "no_prompt_sent": True,
        }
    except Exception as exc:  # noqa: BLE001 - config integrity is checked below
        probe_error = exc
    finally:
        returncode, stderr = client.close()

    after = _digest_or_absent(config_path)
    if before != after:
        raise KimiRouteProbeError("protected Kimi user config changed during probe")
    if probe_error is not None:
        if isinstance(probe_error, KimiRouteProbeError):
            raise probe_error
        raise KimiRouteProbeError(f"Kimi ACP route probe failed: {probe_error}") from probe_error
    if returncode not in (0, -15):
        raise KimiRouteProbeError(f"Kimi ACP server exited {returncode}: {stderr[-500:]}")

    invocation = {
        "argv": [kimi_bin, "--yolo", "--model", expected_model],
        "env": {"KIMI_MODEL_THINKING_EFFORT": expected_effort},
    }
    result["terminal_route_sha256"] = hashlib.sha256(
        json.dumps(invocation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result
