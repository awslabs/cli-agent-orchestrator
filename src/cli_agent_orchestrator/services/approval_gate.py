"""Refuse an unapproved script-tier run (issue #583 Bolt 2, unit ``approval-gate``).

FR-8's enforcement half. ``approval_store`` can say whether a ``plan_id`` is approved and
``manifest_freeze`` computes that ``plan_id`` at run start; this module is the first code in Bolt 2
that can actually STOP a run, and both freeze call sites already carry the written promise that
something will — "writes NULL and fails CLOSED at the approval gate".

ENFORCEMENT DEFAULTS **ON** SINCE BOLT 3 (``approval-enforcement-default``). It defaulted OFF in
Bolt 2, and the reason was specific rather than timid: ``manifest_freeze.build_manifest_json`` is
total, so it returns ``None`` on any failure, which writes a NULL manifest, which refuses here. Each
of those is individually right. Composed under an always-on gate they meant a TRANSIENT freeze
failure refused a run with nothing wrong with it, and there was no in-product way to grant approval
until Bolt 3.

Both halves of that reasoning are now addressed, which is what licenses the flip. The freeze failure
is no longer indistinguishable from a missing approval: it raises
:class:`PlanIdentityUnavailableError` and the API transports it as **503** (transient, retry),
while a genuinely unapproved plan stays **403** (approve this identifier). And Bolt 3 ships the
authoring sequence that presents a plan and takes approval BEFORE running. Left opt-in, FR-8's
guarantee would simply never apply in a default installation.

ONE REFUSAL CAUSE REMAINS UNDISTINGUISHED, AND IT IS A KNOWN GAP RATHER THAN AN OVERSIGHT.
``approval_store.is_approved`` answers ``False`` on a database error — correctly fail-closed, so a
store fault refuses rather than admits. But it reaches the same arm as a genuine absence, so a
faulted store is reported as "this plan has not been approved", sending the operator to approve a
plan when the real fault is a database CAO could not read. Telling those apart needs a tri-state
sibling in ``approval_store``, which is deliberately out of this unit's scope. Recorded in this
unit's ``reliability-requirements.md`` as REL-4, carried as an unowned #583 residual; note that no
run-oriented surface can close it, because a refused start writes no run row.

THE GATE REPORTS; IT DOES NOT FORM AN OPINION ABOUT WHETHER AN APPROVAL EXISTS.
``approval_store.is_approved`` is the sole authority, is already total, and already answers ``False``
on a database error. A second opinion here would be the copy that drifts — and a drifted
authorisation check is the kind that looks correct while permitting something.

IT SITS BEFORE THE FIRST IRREVERSIBLE ACT ON BOTH PATHS, and the two placements are not symmetric
because what they protect is not the same thing:

* **start** — before the ``workflow_run`` INSERT. A refused start leaves NO row: a run row is a
  record of a run, and every first run of every new plan is refused by design, so recording them
  would durably record runs that never happened.
* **resume** — as a fifth check in ``resume_script_run``'s admission ladder, AFTER its existing four
  (absent / live / not-resumable / corrupt) and BEFORE the generation bump. The bump fences out any
  still-live process for that run, so bumping and then refusing would kill a possibly-healthy run on
  behalf of a resume that was itself rejected. Two losses from one refusal.

YAML RUNS ARE OUTSIDE THIS GATE BY CONSTRUCTION, not by a check that could pass for the wrong reason:
the tier is already recorded on every run (``insert_run``'s ``tier: str = "yaml"``, with script runs
passing ``"script"`` explicitly), so C-1 is met structurally.

THIS IS A SAME-USER LOCAL CONTROL, NOT A PRIVILEGE BOUNDARY. CAO runs as the invoking user and a
workflow script runs as that same user, so a script could edit ``settings.json`` or write to
``workflow_plan_approval`` directly. That is inherent to a local single-user tool. What the gate does
provide is that a plan cannot execute UNNOTICED under an approval granted for a different plan, which
is FR-8's property and is worth having on its own terms. Documenting it as stronger than it is would
be the actual defect.
"""

import json
import logging
from typing import Optional

from cli_agent_orchestrator.services import approval_store
from cli_agent_orchestrator.services.settings_service import is_workflow_approval_required

logger = logging.getLogger(__name__)

SCRIPT_TIER = "script"


class PlanApprovalRequiredError(Exception):
    """A script-tier run was refused because its ``plan_id`` has no approval.

    DELIBERATELY NOT A ``ValueError``. The API's resume handler ends with ``except ValueError -> 400``,
    so subclassing it would silently transport this as a bad request. It is also deliberately NOT
    ``ResumeNotAllowedError`` (409): that means "this run cannot be resumed" — a fact about the run —
    whereas this is a fact about the PLAN, and the operator's next action is entirely different
    (approve a ``plan_id`` versus investigate a stuck run). ``unit-of-work.md`` names this seam: the
    failure must be "distinguishable from an ordinary execution error so a caller can tell 'needs
    approval' from 'the run broke'". Transported as 403.

    ``plan_id`` is carried explicitly because it is the operator's ONLY handle on what to approve. It
    is ``None`` only when the manifest was absent or unparseable, and the message says so.
    """

    def __init__(self, message: str, plan_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.plan_id = plan_id


class PlanIdentityUnavailableError(PlanApprovalRequiredError):
    """No plan identifier could be read from the run's frozen manifest (issue #583 Bolt 3).

    THE CALLER DID NOTHING WRONG, WHICH IS THE WHOLE REASON THIS TYPE EXISTS. A NULL or unparseable
    manifest means ``manifest_freeze.build_manifest_json`` failed — CAO could not complete its own
    freeze. Reporting that as 403 asserts the caller lacked permission and sends an operator looking
    for an approval to grant, when the correct action is to retry. The API transports this as **503**
    and its parent as 403, so the difference is visible to every HTTP client without parsing prose.

    DELIBERATELY A SUBCLASS OF :class:`PlanApprovalRequiredError`, so the change is additive: every
    existing ``except PlanApprovalRequiredError`` site keeps catching this condition and no path can
    start returning an unhandled 500. The corollary is an ORDERING REQUIREMENT at each handler — the
    narrow clause must precede the broad one, because Python matches ``except`` clauses in order and
    the wrong order silently returns 403, which READS AS CORRECT rather than failing.

    ``plan_id`` is inherited and is ALWAYS ``None`` for this type, by definition of the condition:
    the identifier that is the operator's only handle on what to approve is exactly what could not
    be read. The attribute is kept rather than removed because narrowing a parent's contract in a
    subclass would be the worse trade.
    """


def plan_id_from_manifest(manifest_json: Optional[str]) -> Optional[str]:
    """Extract ``plan_id`` from a frozen manifest, or ``None`` if it cannot be read.

    Total by design: a malformed manifest is an expected condition here, not an error. Every way this
    can answer ``None`` converges on a refusal at :func:`ensure_plan_approved`, so absence never
    becomes permission.

    NEVER RECOMPUTES the identifier. On resume, recomputation would re-read the script from disk, so a
    source edit between start and resume would produce a different ``plan_id`` and refuse a run whose
    approval is perfectly valid for what it actually froze. Rejecting a stale source hash is a
    separate mechanism, deferred to Bolt 3. Using one extraction on both paths also means the start
    and resume gates cannot disagree about what the identifier is.
    """
    if not manifest_json:
        return None
    try:
        document = json.loads(manifest_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(document, dict):
        return None
    plan_id = document.get("plan_id")
    return plan_id if isinstance(plan_id, str) and plan_id else None


def ensure_plan_approved(*, tier: str, manifest_json: Optional[str]) -> None:
    """Raise :class:`PlanApprovalRequiredError` unless this run may proceed.

    Returns ``None`` on every permitted path. Called from three places — the async start arm, the
    blocking start arm, and resume admission — as ONE function rather than three inline checks,
    because three copies of an authorisation check is three chances for one to drift, and both start
    arms must agree or a run's approvability would depend on which route started it.

    Raises the narrower :class:`PlanIdentityUnavailableError` when the manifest carries no readable
    identifier, and the base class when the store reports the identifier unapproved. Callers that do
    not care about the difference need no change, since the former is a subclass of the latter.

    Two independent gates on applicability, both kept on purpose. The tier check is structural and
    the setting is operational; either alone would satisfy C-1's letter. THE FLIP OF THE DEFAULT HAS
    NOW HAPPENED (Bolt 3, ``approval-enforcement-default``) and the tier check is why it did not
    start gating YAML — which is exactly what keeping both was for.
    """
    if tier != SCRIPT_TIER:
        return
    if not is_workflow_approval_required():
        # No longer the default (Bolt 3 flipped it ON), so this arm is now the CONFIGURED-off case or
        # the read-failure fallback. is_approved is still not called, which matters more after the
        # flip rather than less: it is what keeps a store fault out of the disabled path entirely.
        return

    plan_id = plan_id_from_manifest(manifest_json)
    if plan_id is None:
        # Fail closed, exactly as both freeze call sites already promise in writing. A freeze that
        # failed wrote NULL, and NULL is indistinguishable from a YAML run at the column level — but
        # not here, because the tier already told us this is a script run.
        logger.warning(
            "workflow approval gate: refusing a script run with no readable plan_id in its "
            "frozen manifest (approval is required by settings)"
        )
        raise PlanIdentityUnavailableError(
            "This script-tier run has no readable plan identifier in its frozen execution "
            "manifest, so it cannot be matched against an approval. Approval is required by "
            "workflow.require_approval."
        )

    if not approval_store.is_approved(plan_id):
        # A control that refuses without saying which plan cannot be operated. plan_id is an opaque
        # digest, so logging it adds nothing sensitive.
        logger.warning("workflow approval gate: refusing unapproved plan_id %s", plan_id)
        raise PlanApprovalRequiredError(
            f"Plan '{plan_id}' has not been approved. Approve this plan identifier and run again. "
            "Note that a plan_id is computed at run start, so the first run of a new or changed "
            "plan is refused by design.",
            plan_id=plan_id,
        )
