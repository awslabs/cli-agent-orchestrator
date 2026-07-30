"""Version-bound, zero-turn Codex trust and route attestation."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import select
import subprocess
import time
from typing import Any, Callable, Mapping, Optional, Sequence, cast

from cli_agent_orchestrator.providers.codex import (
    _toml_override,
    render_trusted_project_override,
)

SUPPORTED_CODEX_VERSION = "codex-cli 0.146.0"
PROBE_VERSION = "codex-trust-route-v1"


class CodexTrustProbeError(RuntimeError):
    pass


def _digest_or_absent(path: pathlib.Path) -> str:
    if not path.exists():
        return "absent"
    if not path.is_file() or path.is_symlink():
        raise CodexTrustProbeError("protected Codex config is not a regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contains_session_flags(value: Any) -> bool:
    if value == "sessionFlags":
        return True
    if isinstance(value, dict):
        return any(_contains_session_flags(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_session_flags(item) for item in value)
    return False


def _response_by_id(stdout: str, request_id: int) -> dict[str, Any]:
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("id") == request_id:
            return cast(dict[str, Any], item)
    raise CodexTrustProbeError(f"Codex app-server omitted response id {request_id}")


def _run_app_server_probe(
    argv: list[str],
    requests: list[dict[str, Any]],
    timeout: float,
    *,
    env: Optional[dict[str, str]] = None,
    followup_factory: Optional[
        Callable[[Mapping[int, Mapping[str, Any]]], Sequence[dict[str, Any]]]
    ] = None,
) -> tuple[str, str, int]:
    """Exchange requests incrementally so EOF cannot overtake async replies.

    ``followup_factory`` is evaluated only after every fixed request has a
    response.  It exists for provider operations whose parameters come from
    an earlier response in the same app-server lifetime, such as materializing
    a newly assigned thread id with ``thread/name/set``.  The factory receives
    only parsed responses keyed by request id; notifications cannot become
    accidental authority for a dependent request.
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        raise CodexTrustProbeError(f"could not start Codex app-server: {exc}") from exc
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        proc.kill()
        raise CodexTrustProbeError("Codex app-server pipes were not created")

    output: list[str] = []
    responses: set[int] = set()
    deadline = time.monotonic() + timeout
    try:
        pending = list(requests)
        followups_added = False
        while pending:
            request = pending.pop(0)
            proc.stdin.write(json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")
            proc.stdin.flush()
            request_id = request.get("id")
            if request_id is None:
                continue
            while request_id not in responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexTrustProbeError(
                        f"Codex app-server timed out awaiting response id {request_id}"
                    )
                readable, _, _ = select.select([proc.stdout], [], [], remaining)
                if not readable:
                    raise CodexTrustProbeError(
                        f"Codex app-server timed out awaiting response id {request_id}"
                    )
                line = proc.stdout.readline()
                if not line:
                    raise CodexTrustProbeError(
                        f"Codex app-server ended before response id {request_id}"
                    )
                output.append(line)
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item.get("id"), int):
                    responses.add(item["id"])
            if not pending and followup_factory is not None and not followups_added:
                parsed = {
                    item_id: _response_by_id("".join(output), item_id)
                    for item_id in sorted(responses)
                }
                pending.extend(list(followup_factory(parsed)))
                followups_added = True
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    return "".join(output), proc.stderr.read(), proc.returncode


def attest_trusted_project(
    project_root: str,
    *,
    expected_model: Optional[str],
    expected_effort: Optional[str],
    timeout: float = 20.0,
    codex_bin: str = "codex",
    user_config_path: Optional[pathlib.Path] = None,
) -> dict[str, Any]:
    """Return a provider-native config + zero-turn route receipt.

    The app-server performs ``initialize``, ``config/read`` and an ephemeral
    ``thread/start`` only.  No ``turn/start`` request is sent, so no model task
    input exists.  The receipt is rejected unless the invocation-only trust
    layer targets the exact canonical path, the resolved model/effort match the
    assignment, and the protected user config is byte-identical afterward.
    """
    if not os.path.isdir(project_root) or os.path.realpath(project_root) != project_root:
        raise CodexTrustProbeError("project_root must be an existing canonical directory")

    try:
        version_proc = subprocess.run(
            [codex_bin, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexTrustProbeError(f"could not execute Codex version probe: {exc}") from exc
    version = version_proc.stdout.strip()
    if version_proc.returncode != 0 or version != SUPPORTED_CODEX_VERSION:
        raise CodexTrustProbeError(
            f"unsupported Codex version {version!r}; expected {SUPPORTED_CODEX_VERSION!r}"
        )

    config_path = user_config_path or pathlib.Path(os.path.expanduser("~/.codex/config.toml"))
    before = _digest_or_absent(config_path)
    override = render_trusted_project_override(project_root)
    argv = [codex_bin, "-c", override]
    if expected_model:
        argv.extend(["-c", _toml_override("model", expected_model)])
    if expected_effort:
        argv.extend(["-c", _toml_override("model_reasoning_effort", expected_effort)])
    argv.extend(["app-server", "--stdio"])

    requests: list[dict[str, Any]] = [
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "cao-managed-launch", "version": "1"}},
        },
        {"method": "initialized", "params": {}},
        {
            "id": 2,
            "method": "config/read",
            "params": {"cwd": project_root, "includeLayers": True},
        },
        {
            "id": 3,
            "method": "thread/start",
            "params": {
                "cwd": project_root,
                "ephemeral": True,
                "approvalPolicy": "never",
                "sandbox": "read-only",
            },
        },
    ]
    probe_error: Optional[Exception] = None
    stdout = stderr = ""
    returncode = -1
    try:
        stdout, stderr, returncode = _run_app_server_probe(argv, requests, timeout)
    except Exception as exc:  # noqa: BLE001 - config integrity still checked below
        probe_error = exc

    after = _digest_or_absent(config_path)
    if before != after:
        raise CodexTrustProbeError("protected Codex user config changed during probe")
    if probe_error is not None:
        if isinstance(probe_error, CodexTrustProbeError):
            raise probe_error
        raise CodexTrustProbeError(
            f"Codex app-server trust probe failed: {probe_error}"
        ) from probe_error
    if returncode not in (0, -15):
        raise CodexTrustProbeError(f"Codex app-server exited {returncode}: {stderr[-500:]}")

    init_response = _response_by_id(stdout, 1)
    config_response = _response_by_id(stdout, 2)
    thread_response = _response_by_id(stdout, 3)
    for name, response in (
        ("initialize", init_response),
        ("config/read", config_response),
        ("thread/start", thread_response),
    ):
        if "error" in response or "result" not in response:
            raise CodexTrustProbeError(f"Codex {name} failed: {response.get('error')!r}")

    config_result = config_response["result"] or {}
    projects = (config_result.get("config") or {}).get("projects") or {}
    trust = (projects.get(project_root) or {}).get("trust_level")
    if trust != "trusted":
        raise CodexTrustProbeError("config/read did not resolve the exact project as trusted")
    if not (
        _contains_session_flags(config_result.get("origins"))
        or _contains_session_flags(config_result.get("layers"))
    ):
        raise CodexTrustProbeError("config/read did not prove sessionFlags provenance")

    thread_result = thread_response["result"] or {}
    actual_model = thread_result.get("model")
    actual_effort = thread_result.get("reasoningEffort")
    actual_cwd = thread_result.get("cwd")
    if expected_model and actual_model != expected_model:
        raise CodexTrustProbeError(
            f"thread/start resolved model {actual_model!r}, expected {expected_model!r}"
        )
    if expected_effort and actual_effort != expected_effort:
        raise CodexTrustProbeError(
            f"thread/start resolved effort {actual_effort!r}, expected {expected_effort!r}"
        )
    if actual_cwd != project_root:
        raise CodexTrustProbeError(
            f"thread/start resolved cwd {actual_cwd!r}, expected {project_root!r}"
        )

    argv_digest = hashlib.sha256(
        json.dumps(argv, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "probe_version": PROBE_VERSION,
        "codex_version": version,
        "project_root": project_root,
        "trust_level": trust,
        "config_origin": "sessionFlags",
        "protected_config_sha256": before,
        "argv_sha256": argv_digest,
        "model": actual_model,
        "model_provider": thread_result.get("modelProvider"),
        "reasoning_effort": actual_effort,
        "cwd": actual_cwd,
        "thread_id": (thread_result.get("thread") or {}).get("id"),
        "no_turn_started": True,
    }
