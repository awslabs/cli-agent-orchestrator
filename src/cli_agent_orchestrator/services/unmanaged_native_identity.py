"""Pre-task harness-native identity for the ordinary/new-terminal path.

cond-0377a: every newly launched unmanaged supervisor/worker on Claude Code
or Codex must obtain a deterministic harness-native session id BEFORE any
real task prompt/input is admitted, and the full-screen TUI must start by
resuming/attaching that exact id.  The managed-v2 path already does this;
the ordinary ``create_terminal`` path used to record ``identity_missing``
and stay ungated.

This module is the shared pre-task identity seam: it reuses the accepted
managed-v2 bootstrap primitives from the ordinary launch path — the
caller-chosen Claude id and the Codex zero-turn app-server bootstrap —
without adding a new claim phase or a parallel identity store.  The
stable-agent roster and the native-attachment authority remain the only
identity authorities.

Truth rules:

- One canonical effective working directory is resolved once and consumed
  by BOTH the physical pane launch and the native bootstrap, so the resumed
  TUI and the minted session agree byte-for-byte on cwd.
- Codex's route (model and ``model_reasoning_effort``) is derived from the
  loaded profile (``profile.model`` and ``codexConfig.model_reasoning_effort``),
  respecting an explicit expected model/effort when present — the same
  composed profile/route material the resumed TUI consumes.  An omitted
  route stays omitted until Codex reports the actual route; no placeholder
  route string is invented.
- For an activated cell, the contract is fail-closed: an unresolvable
  executable, an unproven build, or a refused bootstrap raises
  :class:`UnmanagedIdentityUnavailable`, and the launch fails before
  provider initialization or task input.  Best-effort legacy
  ``identity_missing`` remains only for providers outside this PR's
  activated set and for pre-existing legacy rows owned by the later repair
  slice.

Kimi is deliberately NOT activated here: installed Kimi 0.34.0 cannot bind
a CAO profile through the zero-turn ACP route (``session/new`` accepts only
cwd, additional directories, and ephemeral MCP servers; ``--agent-file``
plus ``--session`` is refused because the agent is bound at session
creation), and the only public profile-capable bootstrap necessarily
performs a real paid model turn.  That cell stays truthfully unactivated
under cond-0377 pending the product decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from typing import Any, Mapping, Optional

from cli_agent_orchestrator.providers.codex import (
    CodexRoute,
    codex_route_suffix,
    compose_codex_core_args,
)
from cli_agent_orchestrator.services import (
    claude_native_launch,
    codex_native_bootstrap,
    native_attachment,
    provider_contracts,
)

#: The provider wire names whose ordinary/new-terminal launches consume a
#: pre-task bound identity contract.  Kimi is deliberately absent: its
#: zero-turn ACP bootstrap cannot bind a CAO profile (verified blocker).
UNMANAGED_PRE_TASK_PROVIDERS = frozenset({"claude_code", "codex", "antigravity_cli"})

# A bounded roster marker, not a lock or claim: it distinguishes an activated
# launch that is currently between terminal-row creation and native-ID binding
# from an older identity-missing terminal that must remain usable while the
# later status-repair slice learns its ID.
PRE_TASK_IDENTITY_PENDING = "pre-task native identity pending"
PRE_TASK_IDENTITY_CAPTURED = "pre-task native identity captured"

#: Executable name per provider wire key, for resolution via ``PATH``.
_PROVIDER_EXECUTABLE = {
    "claude_code": provider_contracts.PROVIDER_CLAUDE,
    "codex": provider_contracts.PROVIDER_CODEX,
    "antigravity_cli": "agy",
}

#: Acquisition method per provider, matching the managed-v2 issuance sources.
_ACQUISITION_BY_PROVIDER = {
    "claude_code": native_attachment.ACQUISITION_CHOSEN_SESSION_ID,
    "codex": native_attachment.ACQUISITION_ZERO_TURN_BOOTSTRAP,
    "antigravity_cli": native_attachment.ACQUISITION_ZERO_TURN_BOOTSTRAP,
}


class UnmanagedIdentityUnavailable(RuntimeError):
    """The activated provider cell cannot supply its accepted identity
    contract (unresolvable executable, unproven build, refused bootstrap).
    The new launch must fail closed — zero provider initialization, zero
    task bytes — rather than silently degrade to identity_missing.
    """


def canonical_working_directory(working_directory: Optional[str]) -> str:
    """The one canonical effective working directory for a launch.

    ``None`` resolves to the canonical current directory (what the tmux
    client's window creation defaults to); a symlink alias resolves to its
    real path.  The physical pane launch and the native bootstrap must both
    consume this exact value so the resumed TUI and the minted session
    agree byte-for-byte on cwd.
    """
    return os.path.realpath(working_directory or os.getcwd())


def _resolve_executable(provider: str) -> str:
    name = _PROVIDER_EXECUTABLE[provider]
    resolved = shutil.which(name)
    if not resolved:
        raise UnmanagedIdentityUnavailable(
            f"the {provider!r} executable {name!r} cannot be resolved on PATH; "
            "the pre-task identity contract cannot be supplied for this cell"
        )
    return os.path.realpath(resolved)


def _binary_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bootstrap_environment(
    *,
    terminal_id: str,
    session_name: str,
    forwarded_environment: Optional[Mapping[str, str]],
) -> dict[str, str]:
    """A bounded child environment for a zero-turn bootstrap.

    Built through the same bounded environment constructor as a newly-created
    pane, then overlaid with the exact persisted/per-launch values handed to
    the backend. ``CODEX_HOME`` is the one allowlisted Codex-prefixed storage
    variable and is handed to both processes identically, so a custom store
    cannot split one native session across two homes.
    """
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    env = TmuxClient._filtered_child_environment(
        dict(forwarded_environment or {}), terminal_id=terminal_id
    )
    env["CAO_SESSION_NAME"] = session_name
    return env


def assert_unmanaged_admission_ready(terminal_id: str, metadata: Mapping[str, Any]) -> None:
    """Refuse real task bytes until an activated ordinary launch is bound.

    This is deliberately provider-scoped while cond-0377 is rolling out:
    Claude and Codex new launches have the deterministic pre-task contract;
    Kimi and legacy provider rows remain truthful ``identity_missing`` rows
    without being retroactively bricked by this slice.
    """
    if metadata.get("provider") not in UNMANAGED_PRE_TASK_PROVIDERS:
        return
    from cli_agent_orchestrator.services import stable_agent_roster

    try:
        agent = stable_agent_roster.get_agent(
            stable_agent_roster.derive_initial_agent_id(
                terminal_id, metadata.get("generation") or None
            )
        )
    except stable_agent_roster.StableAgentNotFound:
        # Pre-roster legacy rows remain compatible. New activated launches
        # create the pending roster marker before starting their bootstrap.
        return
    except stable_agent_roster.StableAgentError as exc:
        raise stable_agent_roster.StableAgentAdmissionRefused(
            f"stable-agent roster for terminal {terminal_id} is unreadable or conflicting; "
            "task input is refused until the binding can be reconciled"
        ) from exc
    lineage = agent.get("current_lineage") or {}
    if lineage.get("native_session_id"):
        stable_agent_roster.assert_admission_ready(
            terminal_id=terminal_id,
            generation=metadata.get("generation") or None,
        )
        return
    if lineage.get("continuity_note") in {
        PRE_TASK_IDENTITY_PENDING,
        PRE_TASK_IDENTITY_CAPTURED,
    }:
        stable_agent_roster.assert_admission_ready(
            terminal_id=terminal_id,
            generation=metadata.get("generation") or None,
        )


def _version_output(provider: str, executable: str, env: dict[str, str]) -> str:
    from cli_agent_orchestrator.services.managed_provider_bridge import (
        provider_version_banner,
    )

    try:
        return provider_version_banner(
            {"provider_executable": executable, "provider": provider},
            environment=env,
        )
    except Exception as exc:  # noqa: BLE001 - the installed binary cannot answer
        raise UnmanagedIdentityUnavailable(
            f"the {provider!r} binary {executable!r} could not report its version: {exc}"
        ) from exc


def _effective_codex_route(
    profile: Any, expected_model: Optional[str], expected_effort: Optional[str]
) -> CodexRoute:
    """The effective Codex route of an ordinary launch.

    An explicit expected model/effort wins; otherwise the loaded profile's
    own route is used (``profile.model`` and
    ``codexConfig.model_reasoning_effort``) — the same route the resumed TUI
    consumes.  Either may be empty: an empty model is the provider-default
    (the bootstrap lets Codex pick and records the actual); an empty effort
    is omitted.  Never invents a ``provider-default`` or empty-string route.
    """
    codex_config = dict(getattr(profile, "codexConfig", None) or {})
    model = expected_model or (getattr(profile, "model", None) or "")
    effort = expected_effort or str(codex_config.get("model_reasoning_effort") or "")
    return CodexRoute(model=model, effort=effort)


def _mint_antigravity_session(
    *,
    terminal_id: str,
    session_name: str,
    working_directory: str,
    expected_model: Optional[str],
    expected_effort: Optional[str],
    agent_profile: Optional[str],
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
    from cli_agent_orchestrator.providers.antigravity_cli import SECURITY_PROMPT
    from cli_agent_orchestrator.utils.skill_injection import apply_skill_prompt

    executable = _resolve_executable("antigravity_cli")
    digest = _binary_sha256(executable)
    env = _bootstrap_environment(
        terminal_id=terminal_id,
        session_name=session_name,
        forwarded_environment=environment,
    )
    version_output = _version_output("antigravity_cli", executable, env)

    profile = None
    if agent_profile:
        try:
            profile = load_agent_profile(agent_profile)
        except Exception as exc:
            raise UnmanagedIdentityUnavailable(
                f"failed to load agent profile {agent_profile!r} for antigravity_cli: {exc}"
            ) from exc

    model = expected_model or (getattr(profile, "model", None) or "")
    effort = expected_effort or ""

    system_prompt = (getattr(profile, "system_prompt", None) or "") if profile else ""
    if system_prompt:
        system_prompt = apply_skill_prompt(system_prompt)
        allowed_tools = getattr(profile, "allowedTools", None) or []
        if allowed_tools and "*" not in allowed_tools:
            system_prompt = (
                f"{system_prompt}\n\n{SECURITY_PROMPT}" if system_prompt else SECURITY_PROMPT
            )

    role_name = (getattr(profile, "name", None) or "agent") if profile else "agent"
    guarded_prompt = (
        f"{system_prompt}\n\n---\n"
        f"You are the {role_name}. Acknowledge your role in one sentence, "
        f"then wait for tasks. Do not take any action or use any tools "
        f"until you receive a specific task."
    ) if system_prompt else "Acknowledge initialization in one sentence and wait."

    cmd = [
        executable,
        "-p", guarded_prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
    ]
    if model:
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])

    try:
        res = subprocess.run(
            cmd,
            cwd=working_directory,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        raise UnmanagedIdentityUnavailable(
            f"antigravity_cli pre-task bootstrap failed to execute: {exc}"
        ) from exc

    if res.returncode != 0:
        raise UnmanagedIdentityUnavailable(
            f"antigravity_cli pre-task bootstrap failed with exit code {res.returncode}: {res.stderr.strip() or res.stdout.strip()}"
        )

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise UnmanagedIdentityUnavailable(
            f"antigravity_cli pre-task bootstrap returned invalid JSON: {res.stdout.strip()}"
        ) from exc

    cid = data.get("conversation_id")
    if not isinstance(cid, str) or not cid:
        raise UnmanagedIdentityUnavailable(
            "antigravity_cli pre-task bootstrap returned no conversation_id"
        )

    return {
        "native_session_id": cid,
        "acquisition_method": _ACQUISITION_BY_PROVIDER["antigravity_cli"],
        "working_directory": working_directory,
        "model": model,
        "effort": effort,
        "executable_path": executable,
        "executable_hash": digest,
        "executable_version": version_output,
        "agent_profile": agent_profile,
        "role": getattr(profile, "role", None) if profile else None,
        "bootstrap": {
            "provider": "antigravity_cli",
            "conversation_id": cid,
            "id_source": provider_contracts.native_id_source(provider_contracts.PROVIDER_ANTIGRAVITY_CLI),
            "working_directory": working_directory,
            "executable": executable,
            "executable_hash": digest,
            "executable_version": version_output,
            "duration_seconds": data.get("duration_seconds", 0),
        },
    }


def resolve_pre_task_identity(
    *,
    provider: str,
    working_directory: Optional[str],
    expected_model: Optional[str],
    expected_effort: Optional[str],
    terminal_id: str,
    session_name: str,
    agent_profile: Optional[str] = None,
    codex_profile_material: Optional[Mapping[str, Any]] = None,
    forwarded_environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Resolve pre-task native identity for an activated ordinary launch.

    ``native_session_id``   the exact id the TUI must resume/attach
    ``acquisition_method``  how the id came to exist (chosen / bootstrap)
    ``working_directory``   the one canonical cwd the pane AND bootstrap use
    ``model`` / ``effort``  the route the resumed TUI pins (actual for Codex:
                            the model the provider returned, and the effort
                            only when it reported one; never invented)
    ``bootstrap``           bounded provider-native bootstrap evidence

    Raises :class:`UnmanagedIdentityUnavailable` when the activated cell
    cannot supply its accepted contract — the launch fails closed.
    """
    if provider not in UNMANAGED_PRE_TASK_PROVIDERS:
        raise UnmanagedIdentityUnavailable(
            f"provider {provider!r} has no accepted pre-task identity contract"
        )
    canonical_cwd = canonical_working_directory(working_directory)

    if provider == "claude_code":
        # Claude's identity is chosen: a canonical uuid minted before any
        # provider I/O and handed to the launch as ``--session-id <id>``.
        native_session_id = claude_native_launch.mint_session_id()
        return {
            "native_session_id": native_session_id,
            "acquisition_method": _ACQUISITION_BY_PROVIDER[provider],
            "working_directory": canonical_cwd,
            "model": expected_model or "",
            "effort": expected_effort or "",
            "bootstrap": {
                "provider": provider,
                "id_source": provider_contracts.native_id_source(
                    provider_contracts.PROVIDER_CLAUDE
                ),
                "working_directory": canonical_cwd,
            },
        }

    if provider == "antigravity_cli":
        return _mint_antigravity_session(
            terminal_id=terminal_id,
            session_name=session_name,
            working_directory=canonical_cwd,
            expected_model=expected_model,
            expected_effort=expected_effort,
            agent_profile=agent_profile,
            environment=forwarded_environment,
        )

    # Codex: the zero-turn app-server bootstrap materializes a resumable
    # rollout for the exact profile route; the TUI then resumes that id.
    if codex_profile_material is None:
        raise UnmanagedIdentityUnavailable(
            "codex pre-task identity requires the resolved profile material"
        )
    profile = codex_profile_material["profile"]
    effective_route = _effective_codex_route(profile, expected_model, expected_effort)
    executable = _resolve_executable(provider)
    digest = _binary_sha256(executable)
    env = _bootstrap_environment(
        terminal_id=terminal_id,
        session_name=session_name,
        forwarded_environment=forwarded_environment,
    )
    version_output = _version_output(provider, executable, env)
    # The bootstrap and the resumed TUI consume the SAME composed core args
    # (profile/yolo, developer instructions, MCP, codexConfig, canonical
    # trust); the bootstrap appends its pinned route, the TUI appends its
    # observed route, TUI flags, and resume id.
    core_args = compose_codex_core_args(
        codex_profile=getattr(profile, "codexProfile", None),
        codex_config=getattr(profile, "codexConfig", None),
        system_prompt=codex_profile_material.get("system_prompt") or "",
        mcp_servers=codex_profile_material.get("mcp_servers") or [],
        allowed_tools=codex_profile_material.get("allowed_tools") or [],
        trusted_project_root=canonical_cwd,
    )
    profile_args = core_args + codex_route_suffix(effective_route)

    try:
        receipt = codex_native_bootstrap.mint_session(
            codex_binary=executable,
            binary_sha256=digest,
            version_output=version_output,
            working_directory=canonical_cwd,
            model=effective_route.model,
            effort=effective_route.effort,
            profile_args=profile_args,
            environment=env,
        )
    except Exception as exc:  # noqa: BLE001 - record the concrete blocker
        raise UnmanagedIdentityUnavailable(
            f"the {provider!r} zero-turn bootstrap refused the pre-task identity "
            f"contract: {exc}"
        ) from exc

    receipt_session_id: Any = receipt.get("native_session_id")
    if not isinstance(receipt_session_id, str) or not receipt_session_id:
        raise UnmanagedIdentityUnavailable(
            f"the {provider!r} bootstrap returned no native session id"
        )
    # Feed the observed actual route back to the resumed TUI: pin the actual
    # non-empty model; pin effort only when the provider reported one.  Never
    # invent a provider-default/empty route or an unreported effort.
    return {
        "native_session_id": receipt_session_id,
        "acquisition_method": _ACQUISITION_BY_PROVIDER[provider],
        "working_directory": canonical_cwd,
        "model": receipt.get("model") or "",
        "effort": receipt.get("effort") or "",
        "bootstrap": dict(receipt),
    }
