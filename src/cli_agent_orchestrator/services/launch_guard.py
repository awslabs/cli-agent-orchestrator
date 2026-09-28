"""Stop agent launches when approval-relevant runtime inputs drift."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from cli_agent_orchestrator.services import approval_gate, settings_service, workflow_journal
from cli_agent_orchestrator.utils import agent_profiles


class PlanInputsChangedError(Exception):
    """An agent profile or the memory setting changed after approval."""


def _profile_digest(name: str) -> Optional[str]:
    try:
        raw = agent_profiles._read_agent_profile_source(name)
    except Exception:
        return None
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def capture() -> Optional[Dict[str, Any]]:
    if not settings_service.is_workflow_approval_required():
        return None
    try:
        names = sorted({profile["name"] for profile in agent_profiles.list_agent_profiles()})
        return {
            "profiles": {name: _profile_digest(name) for name in names},
            "memory_enabled": settings_service.is_memory_enabled(),
        }
    except Exception as exc:
        raise approval_gate.PlanIdentityUnavailableError(
            "Could not read the agent profile set for this script run, so its approval "
            "cannot be bound."
        ) from exc


def check(run_id: Optional[str], agent: str) -> None:
    if not run_id:
        return
    row = workflow_journal.get_run(run_id)
    if row is None or row.tier != "script":
        return
    try:
        snapshot = json.loads(row.spec_snapshot)
    except (TypeError, json.JSONDecodeError):
        # Snapshots written before this guard existed were YAML-shaped. They
        # remain resumable and have no approved launch state to compare.
        return
    try:
        guard = snapshot.get("launch_guard")
        if guard is None:
            return
        profiles = guard["profiles"]
        memory_enabled = guard["memory_enabled"]
    except (TypeError, KeyError, AttributeError):
        raise PlanInputsChangedError(
            "This run's recorded launch state is unreadable; start a new run."
        ) from None
    if profiles.get(agent) != _profile_digest(agent):
        raise PlanInputsChangedError(
            f"Agent profile '{agent}' changed after this run was approved; start a new run."
        )
    if memory_enabled != settings_service.is_memory_enabled():
        raise PlanInputsChangedError(
            "The memory setting changed after this run was approved; start a new run."
        )
