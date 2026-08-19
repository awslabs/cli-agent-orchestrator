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

from cli_agent_orchestrator.providers.codex import CODEX_APP_SERVER_FLAGS
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

#: The schema of the fresh-process resume-adoption proof recorded on every
#: bootstrap receipt.  The zero-turn contract's last element is that a
#: *different* process can resume the minted thread and be handed back the
#: same id; a mint that materialized a rollout only this process can read is
#: not a resumable session, and nothing downstream may treat it as one.
RESUME_ADOPTION_SCHEMA = "cao-codex-native-resume-adoption-v1"

#: The app-server method and parameter name the adoption leg uses.  Both are
#: read back from the installed binary's own error vocabulary when they are
#: wrong (``Invalid request: missing field `threadId```), so a build that
#: renames either fails this leg loudly rather than skipping it.
RESUME_METHOD = "thread/resume"
RESUME_THREAD_ID_PARAM = "threadId"


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


def _prove_resume_adoption(
    argv: list[str],
    native_id: str,
    timeout: float,
    *,
    env: Optional[Mapping[str, str]],
    config_path: pathlib.Path,
) -> dict[str, Any]:
    """Prove a *fresh process* resumes the minted thread and is handed the same id.

    This is the last element of the zero-turn contract, and the only one the
    minting process cannot establish about itself: ``thread/name/set``
    materializes a rollout inside one app-server lifetime, so a mint that
    never leaves that lifetime has shown the rollout exists, not that anything
    else can adopt it.  Verified here against the binary actually installed,
    which is why no build needs to be listed anywhere first — a build that
    cannot do this fails the leg, and a build that can has proven the contract
    on the bytes in front of this process.

    Sends no ``turn/*`` and writes no task bytes.  The protected user config is
    re-checked afterwards for the same reason the mint checks it: a probe that
    silently rewrote the operator's configuration would have bought its proof
    with a side effect nobody asked for.
    """
    requests: list[dict[str, Any]] = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "cao-native-resume-adoption", "version": BOOTSTRAP_SCHEMA}
            },
        },
        {"method": "initialized", "params": {}},
        {"id": 2, "method": RESUME_METHOD, "params": {RESUME_THREAD_ID_PARAM: native_id}},
    ]
    before = _digest_or_absent(config_path)
    try:
        stdout, stderr, returncode = _run_app_server_probe(
            argv, requests, timeout, env=dict(env) if env is not None else None
        )
    except Exception as exc:  # noqa: BLE001 - normalized to the bootstrap's own error
        raise CodexBootstrapError(
            f"Codex {RESUME_METHOD} adoption probe failed for {native_id}: {exc}"
        ) from exc
    if _digest_or_absent(config_path) != before:
        raise CodexBootstrapError(
            "protected Codex user config changed during the resume-adoption probe"
        )
    if returncode not in (0, -15):
        raise CodexBootstrapError(
            f"codex app-server exited {returncode} during resume adoption: {stderr[-500:]}"
        )
    response = _response_by_id(stdout, 2)
    if "error" in response or "result" not in response:
        raise CodexBootstrapError(
            f"codex {RESUME_METHOD} failed for {native_id}: {response.get('error')!r}"
        )
    thread = (response.get("result") or {}).get("thread") or {}
    adopted = thread.get("id")
    if adopted != native_id:
        raise CodexBootstrapError(
            f"codex {RESUME_METHOD} adopted {adopted!r}, not the minted {native_id!r}"
        )
    # ``sessionId`` is checked only when the build reports one.  Asserting a
    # field this build does not carry would refuse a working resume for the
    # shape of its response rather than for its behaviour; ignoring a field it
    # does carry would let a mismatch through.
    session_id = thread.get("sessionId")
    if session_id is not None and session_id != native_id:
        raise CodexBootstrapError(
            f"codex {RESUME_METHOD} returned sessionId {session_id!r} for thread {native_id!r}"
        )
    if thread.get("ephemeral") is True:
        raise CodexBootstrapError(
            f"codex {RESUME_METHOD} adopted {native_id} as an ephemeral thread"
        )
    return {
        "schema": RESUME_ADOPTION_SCHEMA,
        "method": RESUME_METHOD,
        "adopted_session_id": adopted,
        "adopted_in_fresh_process": True,
        "reported_session_id": session_id,
        "ephemeral": thread.get("ephemeral"),
        "sent_no_turn": True,
        "exit_status": returncode,
        "exchange_sha256": _digest({"initialize": _response_by_id(stdout, 1), "resume": response}),
        "protected_config_sha256": before,
    }


def mint_session(
    *,
    codex_binary: str,
    binary_sha256: str,
    version_output: str,
    working_directory: str,
    model: Optional[str],
    effort: Optional[str],
    profile_args: Sequence[str],
    environment: Optional[Mapping[str, str]] = None,
    developer_instructions: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Create one persistent thread (exact-route or provider-default) and prove
    the minter exited.

    The requested ``model`` and ``effort`` are OPTIONAL so
    an ordinary launch can inherit Codex's own configuration/defaults.  An
    unset model is omitted from ``thread/start``; effort is never a
    ``thread/start`` field (Codex's ``ThreadStartParams`` has no
    reasoning-effort field), so it stays a config/argv selection.  The actual
    model, effort (which may truthfully be null/unselected), and cwd returned
    by ``thread/start`` are ALWAYS validated and recorded; when a caller
    supplied a non-empty expected value the exact equality check is retained.
    A sealed managed-v2 route still supplies non-empty values and stays strict.
    """
    digest = _validate_binary(codex_binary, binary_sha256, version_output)
    if (
        not isinstance(working_directory, str)
        or not os.path.isdir(working_directory)
        or os.path.realpath(working_directory) != working_directory
    ):
        raise CodexBootstrapError("working_directory must be an existing canonical directory")
    expected_model = model if isinstance(model, str) and model else None
    expected_effort = effort if isinstance(effort, str) and effort else None

    # The trust override and route are composed once by the shared Codex
    # argument composer and arrive in ``profile_args``; the bootstrap appends
    # only the app-server suffix (one implementation of trust
    # rendering, owned by the composer).
    argv = [
        codex_binary,
        *list(profile_args),
        *CODEX_APP_SERVER_FLAGS,
    ]
    thread_params: dict[str, Any] = {
        "cwd": working_directory,
        "ephemeral": False,
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
    }
    # Omit an unset model so Codex applies its own default; the actual model
    # is recorded from the thread/start response.
    if expected_model:
        thread_params["model"] = expected_model
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
    effective_home = (child_env or os.environ).get("HOME")
    if configured_home:
        codex_home = pathlib.Path(configured_home).expanduser()
    elif effective_home:
        codex_home = pathlib.Path(effective_home).expanduser() / ".codex"
    else:
        codex_home = pathlib.Path.home() / ".codex"
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
    # cwd is always supplied and always asserted.  Model and effort are
    # asserted only when the caller supplied a non-empty expected value; an
    # ordinary default-route launch records the provider's actual (possibly
    # null) values instead.
    route_mismatch: list[str] = []
    if actual_cwd != working_directory:
        route_mismatch.append(f"cwd={actual_cwd!r}")
    if expected_model is not None and actual_model != expected_model:
        route_mismatch.append(f"model={actual_model!r} (expected {expected_model!r})")
    if expected_effort is not None and actual_effort != expected_effort:
        route_mismatch.append(f"effort={actual_effort!r} (expected {expected_effort!r})")
    if route_mismatch:
        raise CodexBootstrapError(
            "Codex persistent thread resolved the wrong route or working directory: "
            + ", ".join(route_mismatch)
        )
    rollout_path = _rollout_path(codex_home, native_id)
    # The contract's last element, and the reason no build needs listing: a
    # separate process resumes what this one minted, or the mint is not a
    # resumable session and fails closed here — before any pane exists.
    resume_adoption = _prove_resume_adoption(
        argv, native_id, timeout, env=child_env, config_path=config_path
    )

    return {
        "schema": BOOTSTRAP_SCHEMA,
        "provider": provider_contracts.PROVIDER_CODEX,
        "native_session_id": native_id,
        "id_source": provider_contracts.native_id_source(provider_contracts.PROVIDER_CODEX),
        "provider_version": provider_contracts.normalized_version(version_output),
        "bootstrap_capability": "zero-turn-resume",
        # Runtime evidence in place of a version allowlist. The bind seam
        # requires this block rather than asking whether someone listed this
        # build, which is a fact about a different binary on a different day.
        "resume_adoption_proof": resume_adoption,
        "binary_path": codex_binary,
        "binary_sha256": digest,
        "working_directory": working_directory,
        "model": actual_model,
        "effort": actual_effort,
        "requested_model": expected_model,
        "requested_effort": expected_effort,
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
