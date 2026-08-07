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

from cli_agent_orchestrator.services import provider_contracts
from cli_agent_orchestrator.services.provider_contracts import PROVIDER_KIMI, SUPPORTED_VERSIONS

#: Exact Kimi builds this probe accepts for route-authority purposes,
#: current first — never a range.  The launch identity boundary is governed
#: by :func:`provider_contracts.check_pinned_version`, which is open for
#: Kimi; the route probe still insists on a proven build because a route
#: receipt proves feature-specific authority.
SUPPORTED_KIMI_VERSIONS = SUPPORTED_VERSIONS[PROVIDER_KIMI]
PROBE_VERSION = "kimi-acp-route-v1"

#: The receipt's ``effort_mode`` when the route selected no effort. Named
#: rather than spelled inline so a reader can grep the receipt value back
#: to the contract that produced it.
EFFORT_MODE_PROVIDER_DEFAULT = "provider-default"


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

    # Refused here, before the binary is started, rather than mid-probe:
    # this model rejects every effort value with ``Invalid params``, which
    # tells a caller only that *some* parameter was wrong, after a session
    # already exists.
    try:
        provider_contracts.validate_route_effort(expected_model, expected_effort)
    except provider_contracts.ProviderContractError as exc:
        raise KimiRouteProbeError(str(exc)) from exc

    env = dict(os.environ)
    # The provider's own deterministic updater kill-switch rides every
    # process this probe starts (cond-0315): the version observation below
    # and the ACP client both run the provider's update preflight, and the
    # reservation-owned suppression must win over any ambient value — an
    # update mid-attestation would move the binary this receipt describes.
    env.update(provider_contracts.kimi_update_suppression_env())
    try:
        version_proc = subprocess.run(
            [kimi_bin, "--version"],
            check=False,
            capture_output=True,
            text=True,
            # The provider-appropriate bounded deadline (cond-0313): a
            # healthy pinned build answered in 0.37–0.41 s warm yet missed
            # a fixed 5 s deadline once under startup load.
            timeout=provider_contracts.KIMI_VERSION_PROBE_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KimiRouteProbeError(f"could not execute Kimi version probe: {exc}") from exc
    version = version_proc.stdout.strip()
    if version_proc.returncode != 0 or version not in SUPPORTED_KIMI_VERSIONS:
        raise KimiRouteProbeError(
            f"unsupported Kimi version {version!r}; expected one of "
            f"{list(SUPPORTED_KIMI_VERSIONS)!r}"
        )

    config_path = user_config_path or pathlib.Path(os.path.expanduser("~/.kimi/config.toml"))
    before = _digest_or_absent(config_path)
    # A route that selects no effort contributes no override here, and the
    # inherited environment must not supply one either: a stale
    # KIMI_MODEL_THINKING_EFFORT in the parent would otherwise reach the
    # probe as though this route had asked for it.
    selects_effort = provider_contracts.route_selects_effort(expected_effort)
    if selects_effort:
        env["KIMI_MODEL_THINKING_EFFORT"] = expected_effort
    else:
        env.pop("KIMI_MODEL_THINKING_EFFORT", None)
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
            selects_effort
            and _current_option(options, category="thought_level", option_id="thinking")
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
        if selects_effort and actual_effort != expected_effort:
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
            # Only ever the effort this probe actually selected and then
            # read back. A route that selected none reports null and says
            # so in the three keys below, rather than passing off whatever
            # the session happened to be sitting at as a resolution: this
            # model exposes no thought_level option, so a value read there
            # would be an artifact, not an answer.
            "reasoning_effort": actual_effort if selects_effort else None,
            "effort_mode": "selected" if selects_effort else EFFORT_MODE_PROVIDER_DEFAULT,
            "effort_observed": bool(selects_effort),
            "acp_protocol_version": initialized.get("protocolVersion"),
            "probe_session_id": session_id,
            "route_source": "acp-session-config",
            "protected_config_sha256": before,
            "terminal_model_argv": ["--model", expected_model],
            "terminal_effort_env": provider_contracts.kimi_effort_env(expected_effort),
            "no_prompt_sent": True,
        }
        if not selects_effort:
            result["effort_unsupported_reason"] = (
                f"{expected_model} exposes no thinking-effort surface on Kimi {version}; "
                "the probe sent no KIMI_MODEL_THINKING_EFFORT and no thinking config "
                "option, so no effort was observed and none is claimed"
            )
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

    # The digest binds the invocation the terminal will actually run, so it
    # has to reflect the omission too: a route that sends no effort env and
    # one that sends an override are different invocations, and hashing
    # them alike would let either satisfy a receipt written for the other.
    invocation = {
        "argv": [kimi_bin, "--yolo", "--model", expected_model],
        "env": provider_contracts.kimi_effort_env(expected_effort),
    }
    result["terminal_route_sha256"] = hashlib.sha256(
        json.dumps(invocation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result
