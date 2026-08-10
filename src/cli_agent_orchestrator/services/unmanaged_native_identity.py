"""Pre-task harness-native identity for the ordinary/new-terminal path.

Every newly launched unmanaged supervisor/worker on Claude Code
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
  loaded profile with the documented precedence — an explicit expected
  model/effort wins, then the profile's ``codexConfig`` overrides
  (``codexConfig.model`` / ``codexConfig.model_reasoning_effort``) win over
  the bare ``profile.model`` field — the same composed profile/route
  material the resumed TUI consumes.  An omitted route stays omitted until
  Codex reports the actual route; no placeholder route string is invented.
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
pending the product decision.
"""

from __future__ import annotations

import hashlib
import os
import shutil
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
from cli_agent_orchestrator.services.provider_contracts import (
    PRE_TASK_IDENTITY_CAPTURED,
    PRE_TASK_IDENTITY_PENDING,
    PRE_TASK_IDENTITY_READY,
)

#: The provider wire names whose ordinary/new-terminal launches consume a
#: pre-task bound identity contract.  Kimi is deliberately absent: its
#: zero-turn ACP bootstrap cannot bind a CAO profile (verified blocker).
UNMANAGED_PRE_TASK_PROVIDERS = frozenset({"claude_code", "codex"})

# A bounded launch/readiness marker, not a lock or claim: it distinguishes an
# activated launch that is currently between terminal-row creation and
# native-ID binding from an older identity-missing terminal that must remain
# usable while the later status-repair slice learns its ID.  The constants
# live in ``provider_contracts`` (the dependency-free contract module) so the
# row writer and the roster can name the same vocabulary; these re-exports
# keep every existing import site working.
#: The terminal row carries this value in its ``native_session_id`` column
#: from row creation until the pre-task identity is captured, so the row is
#: visibly pending at its first durable visibility.
#: The roster lineage note stays ``PENDING`` through identity resolution and
#: ``CAPTURED`` through provider/TUI initialization, then transitions to
#: ``READY`` — input is admitted only after that transition.
__all__ = [
    "PRE_TASK_IDENTITY_PENDING",
    "PRE_TASK_IDENTITY_CAPTURED",
    "PRE_TASK_IDENTITY_READY",
]

#: Executable name per provider wire key, for resolution via ``PATH``.
_PROVIDER_EXECUTABLE = {
    "claude_code": provider_contracts.PROVIDER_CLAUDE,
    "codex": provider_contracts.PROVIDER_CODEX,
}

#: Acquisition method per provider, matching the managed-v2 issuance sources.
_ACQUISITION_BY_PROVIDER = {
    "claude_code": native_attachment.ACQUISITION_CHOSEN_SESSION_ID,
    "codex": native_attachment.ACQUISITION_ZERO_TURN_BOOTSTRAP,
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


def _row_launch_marker(terminal_id: str, metadata: Mapping[str, Any]) -> Optional[str]:
    """The terminal row's new-launch marker, or ``None`` for a legacy row.

    An activated launch stamps its row with :data:`PRE_TASK_IDENTITY_PENDING`
    at row creation, then the real captured id once the pre-task identity
    resolves.  Either value is the new-launch marker: the row is fail-closed
    from first durable visibility until the roster lineage reaches
    :data:`PRE_TASK_IDENTITY_READY`.  ``None`` means the row was born
    without the contract (a pre-deploy legacy row) and keeps its exemption.

    The caller-supplied metadata is used when it carries the marker (the
    direct-input lane passes the full row); a caller that passes only a
    provider/generation projection (the control-input lane) falls back to
    the durable row so both lanes decide on the same evidence.
    """
    marker = metadata.get("native_session_id")
    if marker is not None:
        return marker if isinstance(marker, str) else None
    from cli_agent_orchestrator.clients import database

    row = database.get_terminal_metadata(terminal_id, warn_if_missing=False)
    if row is None:
        return None
    value = row.get("native_session_id")
    return value if isinstance(value, str) else None


def assert_unmanaged_admission_ready(terminal_id: str, metadata: Mapping[str, Any]) -> None:
    """Refuse real task bytes until an activated ordinary launch is ready.

    The gate is closed for an activated provider from the row's first
    durable visibility (its pending marker) through identity capture and
    provider/TUI initialization, and opens only after the readiness
    transition.  The legacy exemption is preserved exactly for rows that
    truthfully lack the new-launch marker — pre-deploy rows and unactivated
    provider rows — which stay usable as before.

    Provider-scoped while the pre-task identity contract is rolling out:
    Claude and Codex new launches have the deterministic pre-task contract;
    Kimi and legacy provider rows remain truthful ``identity_missing`` rows
    without being retroactively bricked by this slice.
    """
    if metadata.get("provider") not in UNMANAGED_PRE_TASK_PROVIDERS:
        return
    from cli_agent_orchestrator.services import stable_agent_roster

    row_marker = _row_launch_marker(terminal_id, metadata)
    try:
        agent = stable_agent_roster.get_agent(
            stable_agent_roster.derive_initial_agent_id(
                terminal_id, metadata.get("generation") or None
            )
        )
    except stable_agent_roster.StableAgentNotFound:
        if row_marker is None:
            # No roster row and no new-launch marker: a truthful pre-roster
            # legacy row stays compatible.
            return
        if row_marker == PRE_TASK_IDENTITY_PENDING:
            # The row is visible and pending but the roster bind has not
            # committed yet — the exact pre-marker race.  Fail closed.
            raise stable_agent_roster.StableAgentAdmissionRefused(
                f"terminal {terminal_id} is an activated launch whose stable-agent "
                "binding has not committed yet; task input is refused until the "
                "pre-task identity binding is durable"
            )
        # A captured id on the row with no roster row is a legacy-repair
        # write, not a new launch (new launches bind the roster before the
        # row ever carries a real id): keep the legacy exemption.
        return
    except stable_agent_roster.StableAgentError as exc:
        raise stable_agent_roster.StableAgentAdmissionRefused(
            f"stable-agent roster for terminal {terminal_id} is unreadable or conflicting; "
            "task input is refused until the binding can be reconciled"
        ) from exc
    lineage = agent.get("current_lineage") or {}
    note = lineage.get("continuity_note")
    if note == PRE_TASK_IDENTITY_READY:
        stable_agent_roster.assert_admission_ready(
            terminal_id=terminal_id,
            generation=metadata.get("generation") or None,
        )
        return
    if note in {PRE_TASK_IDENTITY_PENDING, PRE_TASK_IDENTITY_CAPTURED}:
        # Still in-flight: identity not captured yet (pending) or captured but
        # the provider/TUI has not finished initializing (captured).  Refused
        # directly — a captured lineage must NOT be admissible until the
        # readiness transition.
        raise stable_agent_roster.StableAgentAdmissionRefused(
            f"terminal {terminal_id} is still {note}; task input is refused until "
            "the pre-task identity binding reaches its ready state"
        )
    if note is None:
        # A legacy lineage carries no pre-task marker: exempt unless the row
        # itself still names the pending marker (an inconsistent state that
        # must fail closed rather than admit).
        if row_marker == PRE_TASK_IDENTITY_PENDING:
            raise stable_agent_roster.StableAgentAdmissionRefused(
                f"terminal {terminal_id} still carries the pre-task identity pending "
                "marker but its roster lineage is not marked pending; task input is "
                "refused until the binding is reconciled"
            )
        return
    raise stable_agent_roster.StableAgentAdmissionRefused(
        f"terminal {terminal_id} has an unknown lineage marker {note!r}; task input "
        "is refused until the pre-task identity binding is reconciled"
    )


def mark_pre_task_identity_ready(*, terminal_id: str, generation: Optional[str] = None) -> None:
    """Transition an activated launch's roster lineage to the ready marker.

    Called only after provider/TUI initialization succeeds — the resumed TUI
    is up and input can be admitted.  The transition is refused unless the
    lineage is still in an in-flight pre-task state, so an identity that was
    never captured cannot be declared ready.
    """
    from cli_agent_orchestrator.services import stable_agent_roster

    stable_agent_roster.transition_lineage_note(
        terminal_id=terminal_id,
        generation=generation,
        from_notes=(PRE_TASK_IDENTITY_PENDING, PRE_TASK_IDENTITY_CAPTURED),
        continuity_note=PRE_TASK_IDENTITY_READY,
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

    Route precedence, in order: a caller-sealed expected model/effort wins;
    otherwise the profile's own ``codexConfig`` override
    (``codexConfig.model`` / ``codexConfig.model_reasoning_effort``) wins
    over the bare ``profile.model`` field for the model; the effort has only
    the config seam.  The resumed TUI consumes this same effective route, so
    the bootstrap and the TUI can never select different routes.  Either may
    be empty: an empty model is the provider-default (the bootstrap lets
    Codex pick and records the actual); an empty effort is omitted.  Never
    invents a ``provider-default`` or empty-string route.
    """
    codex_config = dict(getattr(profile, "codexConfig", None) or {})
    profile_model = getattr(profile, "model", None) or ""
    config_model = codex_config.get("model")
    model = (
        expected_model or (config_model if isinstance(config_model, str) else "") or profile_model
    )
    effort = expected_effort or str(codex_config.get("model_reasoning_effort") or "")
    return CodexRoute(model=model, effort=effort)


def resolve_pre_task_identity(
    *,
    provider: str,
    working_directory: Optional[str],
    expected_model: Optional[str],
    expected_effort: Optional[str],
    codex_profile_material: Optional[Mapping[str, Any]],
    forwarded_environment: Optional[Mapping[str, str]],
    terminal_id: str,
    session_name: str,
) -> dict[str, Any]:
    """Resolve the deterministic harness-native session id for an unmanaged
    new launch, BEFORE the provider starts.

    ``codex_profile_material`` is the EXACT material ``create_terminal``
    resolved once from the already-loaded profile (developer instructions,
    resolved MCP servers, tool policy) — the same core args the resumed TUI
    consumes; it is never reloaded here.  ``forwarded_environment`` is the
    effective env overlay the pane received (operator ``env_vars`` + persisted
    session env, merged with the backend's new/existing semantics), so the
    bootstrap subprocess and the pane agree byte-for-byte on the environment.

    Returns a bounded evidence record:

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
    # observed route, TUI flags, and resume id.  A malformed profile
    # composition (e.g. an invalid codexConfig key) is a pre-task identity
    # failure: the launch fails closed with the typed refusal, never a raw
    # serializer error leaking out of the pre-task seam.
    try:
        core_args = compose_codex_core_args(
            codex_profile=getattr(profile, "codexProfile", None),
            codex_config=getattr(profile, "codexConfig", None),
            system_prompt=codex_profile_material.get("system_prompt") or "",
            mcp_servers=codex_profile_material.get("mcp_servers") or [],
            allowed_tools=codex_profile_material.get("allowed_tools") or [],
            trusted_project_root=canonical_cwd,
        )
        profile_args = core_args + codex_route_suffix(effective_route)
    except Exception as exc:  # noqa: BLE001 - record the concrete blocker
        raise UnmanagedIdentityUnavailable(
            f"the {provider!r} launch composition was refused by the pre-task "
            f"identity contract: {exc}"
        ) from exc

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
        # The EXACT digest-verified executable the bootstrap proved.  The
        # resumed TUI launches this path (never a bare ``codex`` resolved
        # through the pane's ambient PATH), so a later launch can never
        # silently run a different build against the minted session.
        "binary_path": receipt.get("binary_path") or executable,
        "binary_sha256": receipt.get("binary_sha256") or digest,
        "bootstrap": dict(receipt),
    }
