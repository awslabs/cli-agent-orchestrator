"""Stop agent launches when approval-relevant runtime inputs drift."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Optional

from cli_agent_orchestrator.services import approval_gate, settings_service, workflow_journal
from cli_agent_orchestrator.utils import agent_profiles, git_baseline


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

NOT BUMPED BY ISSUE #827'S ROOT BINDING, deliberately. The scheme names the PER-PROFILE digest rule and
that rule is unchanged; ``root_identity`` is a new field in the record, not a new spelling of an
existing value. A ``launch-guard-v2`` record written before the root binding simply has no
``root_identity`` key, and :func:`check` treats an absent key as "not recorded" and compares nothing —
the same posture as the missing-marker arm above, and for the same reason: an in-flight run admitted
before the upgrade must keep launching its remaining steps rather than being refused for a field it
could not have carried.
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
    root_identity: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Record the launch state a run is admitted under: a digest per installed profile, memory, root.

    ``profile_set`` is the snapshot ``manifest_freeze.freeze_manifest`` already read for the ``plan_id``
    (``FrozenManifest.profile_set``). Both start paths MUST pass it: the approval gate ran against that
    snapshot, and a second read here would record whatever is on disk after the gate — so a profile
    edited in that window was recorded as the approved state and ``check`` accepted it for the rest of
    the run (PR #1000, P3-3). ``test_callsite_lint_rules`` fails any production call that omits it.
    ``None`` reads the set now, for callers with no freeze (tests, and a freeze that never reached the
    profile set).

    ``root_identity`` is the same seam for the working-directory root's ``st_dev``/``st_ino``
    (``FrozenManifest.root_identity``, issue #827 criterion 2), and it CANNOT fall back to reading it
    here: this function is not told the root path, and inventing one from ``os.getcwd()`` would bind a
    different directory than the identifier did. BOTH ROOT KINDS ARE BOUND HERE: a Git root's inode
    stays out of ``plan_id`` (its identity is its commit and worktree digest), but the guard binds it
    anyway, because nothing recomputes ``plan_id`` inside a run or at resume — see
    ``manifest_freeze.FrozenManifest.root_identity``. ``None`` therefore means "this caller has no
    freeze to hand one from" (tests, and a freeze that never reached a baseline, whose run is refused
    anyway), and the call-site lint requires every production admission path to pass it, so "forgot the
    keyword" cannot masquerade as "nothing to bind".
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
            # Stored even when it is ``None`` or unbound, so the record states which answer the freeze
            # reached rather than leaving a reader to guess from an absent key.
            "root_identity": dict(root_identity) if root_identity is not None else None,
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
    _check_root_identity(guard, snapshot)


def ensure_root_unchanged(snapshot: Mapping[str, Any]) -> None:
    """The root comparison alone, for RESUME admission (#827 criterion 2; ask 3 on PR #1051).

    :func:`check` is per-AGENT-LAUNCH, so a resumed run reached its first root comparison only when it
    started a NEW agent step. Two consequences, and #827's second criterion names both ("a running
    **or resumed** script run"):

    * the script code BEFORE the first new agent step — every line of it, including anything
      side-effecting — ran in the replacement directory;
    * a resume whose remaining agent steps are all REPLAYS ran to completion there, because a replayed
      step returns before ``run_agent_step`` and nothing else compares the root.

    SHARES :func:`_check_root_identity` WITH :func:`check` rather than restating the comparison. Two
    spellings of "is this still the directory that was approved" would drift, and the three skip arms
    (absent key, ``None``, ``bound: False``) are the migration posture that must be IDENTICAL at both
    call sites or a run would be refused at resume for a record the next agent launch accepts.

    TAKES THE SNAPSHOT, NOT A ``run_id``: resume has already parsed ``spec_snapshot`` at its gate 4,
    and a second read here could see a different row.

    TWO DELIBERATE NON-REFUSALS, because this function adds the ROOT comparison to resume and nothing
    else. Approval enforcement being off skips it, mirroring :func:`check` and gate 6's own predicate.
    A snapshot with no usable ``launch_guard`` mapping (a run admitted while enforcement was off, which
    records ``{"approval_required": False}``) skips it too: diagnosing a posture change is
    :func:`check`'s job at the next agent launch, and inventing that refusal here would make resume
    answer a question it was not asked.
    """
    if not settings_service.is_workflow_approval_required():
        return
    guard = snapshot.get("launch_guard")
    if not isinstance(guard, Mapping):
        return
    _check_root_identity(guard, snapshot)


def _check_root_identity(guard: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
    """Refuse when the run's working directory is no longer the directory that was approved (#827).

    The path is not the identity. A root recorded as ``st_dev``/``st_ino`` at admission is re-stat'd
    here through the SAME path the run was admitted with (``spec_snapshot``'s ``working_directory``,
    which the start paths already write), so a directory moved away and replaced by another at the same
    path is refused instead of inheriting the original's approval.

    BOTH ROOT KINDS, GIT AND NOT. A Git root's inode is deliberately absent from ``plan_id``, but that
    only answers the CROSS-run case: inside a run and at resume nothing recomputes ``plan_id``, so
    without this comparison a Git worktree replaced at the same path kept launching the run's remaining
    terminals inside the replacement checkout under the original approval (fanhongy's ask 2 on
    PR #1051). ``manifest_freeze`` therefore hands over its one stat for either kind.

    THREE RECORDED STATES SKIP THE COMPARISON, each for its own reason, and none of them is a silent
    skip — every one of them is visible in the stored record:

    * key ABSENT — a ``launch-guard-v2`` record written before this field existed. Refusing it would
      turn every in-flight pre-upgrade run into a false refusal at its next agent step, which is the
      regression PR #1000's P3-1 fixed for the scheme marker.
    * ``None`` — a record written by a build whose freeze bound non-Git roots only, so a Git root
      arrived here as ``None``; or a caller with no freeze at all. Same reasoning as the absent key: a
      run admitted under a weaker binding is checked under it, and the next NEW run gets the current
      one. The stat never happened, so there is nothing to compare.
    * ``bound: False`` — the filesystem supplied no usable inode (``root_identity_unsupported``) or the
      root could not be stat'd at freeze (``root_identity_unreadable``). There is no pair to compare
      against; for a non-Git root the freeze also recorded that unbound marker into ``plan_id`` and
      logged the reason, so the operator approved a plan that says its root identity is not bound.

    A BOUND RECORD THAT CANNOT BE RE-VERIFIED IS A REFUSAL, not a skip: an identity that stops being
    readable, or a snapshot with no ``working_directory`` to re-stat, means the guard can no longer show
    the run is still in the approved directory, and the guard's whole posture is fail-closed.

    THE TWO REFUSALS SAY DIFFERENT THINGS, because they call for different actions. A bound pair that
    differs means a different directory is at the path, and "start a new run" is the fix. An identity
    that cannot be read NOW — the root was deleted, or the stat failed with ``EACCES``/``ESTALE`` —
    is not a replacement, and telling the operator to start a new run sends them into a 422
    (``working_directory must name an existing directory``) instead of at the missing directory
    (fanhongy's P3-2 on PR #1051).
    """
    if "root_identity" not in guard:
        return
    recorded = guard["root_identity"]
    if not isinstance(recorded, dict) or not recorded.get("bound"):
        return
    root = snapshot.get("working_directory")
    if not isinstance(root, str) or not root:
        raise PlanInputsChangedError(_UNREADABLE_MESSAGE)
    # ONE stat, read once and then classified, so the two refusal arms cannot describe two different
    # observations of the directory.
    current = git_baseline.root_identity(root)
    if not current.get("bound"):
        raise PlanInputsChangedError(
            "This run's working directory can no longer be read, or no longer exists, so it cannot be "
            "shown to be the directory that was approved; restore it, or start a new run."
        )
    if current != recorded:
        raise PlanInputsChangedError(
            "This run's working directory is no longer the directory that was approved: its path is "
            "unchanged, but the directory at that path has been replaced; start a new run."
        )
