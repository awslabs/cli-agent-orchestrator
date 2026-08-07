"""Mint and materialize one persistent Codex thread without a turn.

The app-server ``thread/start`` response is Codex's pre-turn native identity
source.  A zero-turn ``thread/start`` alone is not written to Codex's rollout
store, so an exact CLI resume cannot find it after app-server exits.  This
bootstrap follows the start with the metadata-only ``thread/name/set`` method,
proves the exact rollout exists, sends no ``turn/start``, closes and reaps the
app-server, and returns the exact thread id that the native TUI may resume.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from cli_agent_orchestrator.providers.codex import render_trusted_project_override
from cli_agent_orchestrator.services import (
    codex_native_launch,
    native_attachment,
    provider_contracts,
)
from cli_agent_orchestrator.services.codex_trust import (
    _digest_or_absent,
    _response_by_id,
    _run_app_server_probe,
)

BOOTSTRAP_SCHEMA = "cao-codex-native-bootstrap-v1"
EXIT_PROOF_SCHEMA = "cao-codex-native-bootstrap-exit-v1"
MATERIALIZATION_METHOD = "thread/name/set"


class CodexBootstrapError(RuntimeError):
    """The Codex zero-turn bootstrap could not be proven safe."""


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_binary(binary: str, digest: str, version_output: str) -> str:
    if not isinstance(binary, str) or not os.path.isabs(binary):
        raise CodexBootstrapError("codex binary must be a canonical absolute path")
    if os.path.realpath(binary) != binary or not os.path.isfile(binary):
        raise CodexBootstrapError("codex binary must be an existing canonical file")
    observed = hashlib.sha256(Path(binary).read_bytes()).hexdigest()
    if observed != digest:
        raise CodexBootstrapError(
            f"codex binary digest changed: expected {digest}, observed {observed}"
        )
    if not provider_contracts.is_proven_version(provider_contracts.PROVIDER_CODEX, version_output):
        raise CodexBootstrapError(
            "Codex native session proof is unavailable for this provider build; "
            "stage-verify it before enabling native identity"
        )
    return observed


def _rollout_path(codex_home: Path, thread_id: str) -> Path:
    """Return the sole exact rollout for ``thread_id`` or fail closed."""
    sessions = codex_home / "sessions"
    matches = [
        path
        for path in sessions.rglob(f"*-{thread_id}.jsonl")
        if path.is_file() and not path.is_symlink()
    ]
    if len(matches) != 1:
        raise CodexBootstrapError(
            f"Codex {MATERIALIZATION_METHOD} did not leave exactly one resumable rollout "
            f"for {thread_id}: found {len(matches)} under {sessions}"
        )
    return matches[0]


def mint_session(
    *,
    codex_binary: str,
    binary_sha256: str,
    version_output: str,
    working_directory: str,
    model: str,
    effort: str,
    profile_args: Sequence[str],
    environment: Optional[Mapping[str, str]] = None,
    developer_instructions: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Create one persistent exact-route thread and prove the minter exited."""
    digest = _validate_binary(codex_binary, binary_sha256, version_output)
    if (
        not isinstance(working_directory, str)
        or not os.path.isdir(working_directory)
        or os.path.realpath(working_directory) != working_directory
    ):
        raise CodexBootstrapError("working_directory must be an existing canonical directory")
    if not isinstance(model, str) or not model:
        raise CodexBootstrapError("model must be a non-empty string")
    if not isinstance(effort, str) or not effort:
        raise CodexBootstrapError("effort must be a non-empty string")

    argv = [
        codex_binary,
        *list(profile_args),
        "-c",
        render_trusted_project_override(working_directory),
        "app-server",
        "--stdio",
    ]
    thread_params: dict[str, Any] = {
        "cwd": working_directory,
        "ephemeral": False,
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
        "model": model,
    }
    if developer_instructions:
        thread_params["developerInstructions"] = developer_instructions
    requests: list[dict[str, Any]] = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "cao-native-bootstrap",
                    "version": BOOTSTRAP_SCHEMA,
                }
            },
        },
        {"method": "initialized", "params": {}},
        {
            "id": 2,
            "method": "config/read",
            "params": {"cwd": working_directory, "includeLayers": True},
        },
        {"id": 3, "method": "thread/start", "params": thread_params},
    ]

    def materialize(
        responses: Mapping[int, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        thread_id = (((responses.get(3) or {}).get("result") or {}).get("thread") or {}).get("id")
        try:
            native_id = codex_native_launch.validate_session_id(thread_id)
        except codex_native_launch.CodexNativeLaunchError as exc:
            raise CodexBootstrapError(
                f"Codex thread/start returned an unusable thread id before materialization: {exc}"
            ) from exc
        return [
            {
                "id": 4,
                "method": MATERIALIZATION_METHOD,
                "params": {
                    "threadId": native_id,
                    "name": f"CAO managed Codex session {native_id[:8]}",
                },
            }
        ]

    child_env = dict(environment) if environment is not None else None
    configured_home = (child_env or os.environ).get("CODEX_HOME")
    codex_home = (
        pathlib.Path(configured_home).expanduser()
        if configured_home
        else pathlib.Path.home() / ".codex"
    )
    config_path = codex_home / "config.toml"
    config_before = _digest_or_absent(config_path)
    probe_error: Optional[Exception] = None
    stdout = stderr = ""
    returncode = -1
    try:
        stdout, stderr, returncode = _run_app_server_probe(
            argv,
            requests,
            timeout,
            env=child_env,
            followup_factory=materialize,
        )
    except Exception as exc:  # noqa: BLE001 - config integrity still checked below
        probe_error = exc
    config_after = _digest_or_absent(config_path)
    if config_before != config_after:
        raise CodexBootstrapError("protected Codex user config changed during native bootstrap")
    if probe_error is not None:
        if isinstance(probe_error, CodexBootstrapError):
            raise probe_error
        raise CodexBootstrapError(f"Codex native bootstrap exchange failed: {probe_error}") from (
            probe_error
        )
    if returncode not in (0, -15):
        raise CodexBootstrapError(f"codex app-server exited {returncode}: {stderr[-500:]}")
    for request_id, name in (
        (1, "initialize"),
        (2, "config/read"),
        (3, "thread/start"),
        (4, MATERIALIZATION_METHOD),
    ):
        response = _response_by_id(stdout, request_id)
        if "error" in response or "result" not in response:
            raise CodexBootstrapError(f"codex {name} failed: {response.get('error')!r}")

    config = _response_by_id(stdout, 2)["result"] or {}
    projects = (config.get("config") or {}).get("projects") or {}
    if (projects.get(working_directory) or {}).get("trust_level") != "trusted":
        raise CodexBootstrapError("Codex did not resolve the exact project as trusted")

    thread = _response_by_id(stdout, 3)["result"] or {}
    thread_id = (thread.get("thread") or {}).get("id")
    try:
        native_id = codex_native_launch.validate_session_id(thread_id)
    except codex_native_launch.CodexNativeLaunchError as exc:
        raise CodexBootstrapError(
            f"Codex thread/start returned an unusable thread id: {exc}"
        ) from exc
    actual_model = thread.get("model")
    actual_effort = thread.get("reasoningEffort")
    actual_cwd = thread.get("cwd")
    if (actual_model, actual_effort, actual_cwd) != (
        model,
        effort,
        working_directory,
    ):
        raise CodexBootstrapError(
            "Codex persistent thread resolved the wrong route or working directory: "
            f"model={actual_model!r} effort={actual_effort!r} cwd={actual_cwd!r}"
        )
    rollout_path = _rollout_path(codex_home, native_id)

    return {
        "schema": BOOTSTRAP_SCHEMA,
        "provider": provider_contracts.PROVIDER_CODEX,
        "native_session_id": native_id,
        "id_source": provider_contracts.native_id_source(provider_contracts.PROVIDER_CODEX),
        "provider_version": provider_contracts.normalized_version(version_output),
        "binary_path": codex_binary,
        "binary_sha256": digest,
        "working_directory": working_directory,
        "model": actual_model,
        "effort": actual_effort,
        "sent_no_turn": True,
        "materialization_method": MATERIALIZATION_METHOD,
        "materialization_sent_no_turn": True,
        "rollout_path": str(rollout_path),
        "rollout_sha256": hashlib.sha256(rollout_path.read_bytes()).hexdigest(),
        "detached_before_launch": True,
        "exit_proof": {
            "schema": EXIT_PROOF_SCHEMA,
            "exit_status": returncode,
            "reaped": True,
        },
        "app_server_exchange_sha256": _digest(
            {
                "initialize": _response_by_id(stdout, 1),
                "config": _response_by_id(stdout, 2),
                "thread": _response_by_id(stdout, 3),
                "materialization": _response_by_id(stdout, 4),
            }
        ),
        "protected_config_sha256": config_after,
    }


def bootstrap_intent(receipt: Mapping[str, Any], *, note: Optional[str] = None) -> dict[str, Any]:
    rollout_path = receipt.get("rollout_path") if isinstance(receipt, Mapping) else None
    rollout_sha256 = receipt.get("rollout_sha256") if isinstance(receipt, Mapping) else None
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema") != BOOTSTRAP_SCHEMA
        or receipt.get("sent_no_turn") is not True
        or receipt.get("materialization_method") != MATERIALIZATION_METHOD
        or receipt.get("materialization_sent_no_turn") is not True
        or not isinstance(rollout_path, str)
        or not os.path.isabs(rollout_path)
        or not isinstance(rollout_sha256, str)
        or len(rollout_sha256) != 64
        or any(character not in "0123456789abcdef" for character in rollout_sha256)
        or receipt.get("detached_before_launch") is not True
        or (receipt.get("exit_proof") or {}).get("reaped") is not True
    ):
        raise CodexBootstrapError(
            "Codex receipt does not prove a materialized, turn-free, detached bootstrap"
        )
    native_session_id = receipt.get("native_session_id")
    if not isinstance(native_session_id, str) or not rollout_path.endswith(
        f"-{native_session_id}.jsonl"
    ):
        raise CodexBootstrapError("Codex receipt rollout path does not bind the native session id")
    return native_attachment.acquire_intent(
        acquisition_method=native_attachment.ACQUISITION_ZERO_TURN_BOOTSTRAP,
        acquisition_receipt=dict(receipt),
        admits_only_new_instructions=True,
        replays_task_bytes=False,
        bootstrap_sent_no_turn=True,
        bootstrap_detached_before_launch=True,
        note=note,
    )
