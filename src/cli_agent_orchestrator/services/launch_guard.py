"""Stop agent launches when approval-relevant runtime inputs drift."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Optional

from cli_agent_orchestrator.services import approval_gate, settings_service, workflow_journal
from cli_agent_orchestrator.utils import agent_profiles


class PlanInputsChangedError(Exception):
    """An agent profile or the memory setting changed after approval."""


GUARD_SCHEME = "launch-guard-v2"
"""Marker ``capture`` writes into every guard record, naming the per-profile rule its values follow.

``check`` compares a stored value with one it computes NOW, so the two must come from the same rule. A
record with no marker was written by the rule main used before this change (``"sha256:" + sha256(<profile
text>)``, :func:`_legacy_profile_digest`), and a run admitted under it is still resumable after the
upgrade: resume reuses the frozen ``plan_id`` and the docs promise it proceeds. Without this marker the
upgrade turned every such resume into a false "profile changed" refusal at its next agent step
(PR #1000 round 2, P3-1). A record whose marker names a rule this build does not know fails closed.

THE RULE A SCHEME NAMES IS FROZEN. Changing ``agent_profiles.profile_entry_digest`` means a new
scheme value, a new branch in :func:`_current_for` for the old one, and a new golden in
``test_launch_guard``; ``test_the_current_scheme_pins_the_per_entry_digest_formula`` is what fails
when that is forgotten.
"""


def _entry_digest(entry: agent_profiles.ProfileEntry) -> Optional[str]:
    """The per-profile value recorded at admission and re-checked at every launch.

    ``None`` when the launch path refuses the name (no source to bind; the launch would fail anyway).
    Otherwise the SAME per-entry digest plan identity folds into ``plan_id`` — source plus every
    installed provider artifact — so an edit to the Kiro agent JSON or the Copilot agent file mid-run is
    caught here exactly as an edit to the CAO profile is (PR #1000, P2-2).
    """
    if entry.source is None:
        return None
    return agent_profiles.profile_entry_digest(entry)


def _profile_digest(name: str) -> Optional[str]:
    try:
        entry = agent_profiles.read_profile_entry(name)
    except Exception:
        return None
    return _entry_digest(entry)


def _legacy_profile_digest(name: str) -> Optional[str]:
    """The per-profile value a record WITHOUT a scheme marker holds: a digest of the profile text alone.

    This is byte-for-byte the rule main's ``launch_guard._profile_digest`` applied before PR #1000, kept
    so a run admitted under it is checked under it. It binds less than the current rule (no installed
    artifacts), which is exactly what that run was approved under; the next NEW run is admitted under
    the current rule.
    """
    try:
        entry = agent_profiles.read_profile_entry(name)
    except Exception:
        return None
    if entry.source is None:
        return None
    return "sha256:" + hashlib.sha256(entry.source.encode("utf-8")).hexdigest()


def _current_for(scheme: Any, agent: str) -> Optional[str]:
    """The value to compare a stored per-profile digest with, computed under the record's own rule."""
    if scheme is None:
        return _legacy_profile_digest(agent)
    if scheme == GUARD_SCHEME:
        return _profile_digest(agent)
    raise PlanInputsChangedError(
        "This run's recorded launch state was written under a rule this build does not know; "
        "start a new run."
    )


_UNREADABLE_MESSAGE = "This run's recorded launch state is unreadable; start a new run."
_SETTINGS_FAILURE_MESSAGE = (
    "The workflow approval setting could not be read; repair settings.json and then resume."
)


def capture(
    profile_set: Optional[Mapping[str, agent_profiles.ProfileEntry]] = None,
) -> Dict[str, Any]:
    """Record the launch state a run is admitted under: one digest per installed profile, plus memory.

    ``profile_set`` is the snapshot ``manifest_freeze.freeze_manifest`` already read for the ``plan_id``
    (``FrozenManifest.profile_set``). Both start paths MUST pass it: the approval gate ran against that
    snapshot, and a second read here would record whatever is on disk after the gate — so a profile
    edited in that window was recorded as the approved state and ``check`` accepted it for the rest of
    the run (PR #1000, P3-3). ``test_callsite_lint_rules`` fails any production call that omits it.
    ``None`` reads the set now, for callers with no freeze (tests, and a freeze that never reached the
    profile set).
    """
    if not settings_service.is_workflow_approval_required():
        return {"approval_required": False}
    try:
        if profile_set is None:
            profile_set = agent_profiles.read_profile_set()
        return {
            "scheme": GUARD_SCHEME,
            "profiles": {name: _entry_digest(profile_set[name]) for name in sorted(profile_set)},
            "memory_enabled": settings_service.is_memory_enabled(),
        }
    except Exception as exc:
        raise approval_gate.PlanIdentityUnavailableError(
            "Could not read the agent profile set for this script run, so its approval "
            "cannot be bound.",
            reason="profiles_unreadable",
        ) from exc


def check(run_id: Optional[str], agent: str) -> None:
    posture = settings_service.resolve_workflow_approval_posture()
    if posture.source == settings_service.GATE_SOURCE_READ_FAILURE:
        # CAO's own settings writers publish atomically; retry once for an
        # external writer or a transient filesystem read failure.
        posture = settings_service.resolve_workflow_approval_posture()
    if not posture.required:
        return
    if not run_id:
        return
    row = workflow_journal.get_run(run_id)
    if row is None:
        # A live script record still requires its durable admission state.
        # Import lazily: script_runner and workflow_service share the registry.
        from cli_agent_orchestrator.services.workflow_service import run_registry

        if getattr(run_registry.get(run_id), "tier", None) == "script":
            raise PlanInputsChangedError(_UNREADABLE_MESSAGE)
        return
    if row.tier != "script":
        return
    if posture.source in (
        settings_service.GATE_SOURCE_READ_FAILURE,
        settings_service.GATE_SOURCE_INVALID_SETTINGS,
    ):
        raise PlanInputsChangedError(_SETTINGS_FAILURE_MESSAGE)
    try:
        snapshot = json.loads(row.spec_snapshot)
    except (TypeError, json.JSONDecodeError):
        # Both script admission paths persist JSON snapshots. A non-JSON script
        # row cannot prove the approved launch state and must fail closed.
        raise PlanInputsChangedError(_UNREADABLE_MESSAGE) from None
    try:
        guard = snapshot.get("launch_guard")
        if guard is None:
            raise TypeError("missing launch guard")
        if guard == {"approval_required": False}:
            raise PlanInputsChangedError(
                "approval enforcement was turned on after this run started; start a new run."
            )
        profiles = guard["profiles"]
        memory_enabled = guard["memory_enabled"]
        scheme = guard.get("scheme")
        if not isinstance(profiles, dict):
            raise TypeError("invalid profile guard")
    except PlanInputsChangedError:
        raise
    except (TypeError, KeyError, AttributeError):
        raise PlanInputsChangedError(_UNREADABLE_MESSAGE) from None
    current = _current_for(scheme, agent)
    if current is None:
        raise PlanInputsChangedError(
            f"Agent profile '{agent}' was not recorded at this run's approval check or "
            "cannot be read now; start a new run."
        )
    if profiles.get(agent) != current:
        raise PlanInputsChangedError(
            f"Agent profile '{agent}' changed after this run was approved; start a new run."
        )
    if memory_enabled != settings_service.is_memory_enabled():
        raise PlanInputsChangedError(
            "The memory setting changed after this run was approved; start a new run."
        )
