"""Check a YAML workflow's step params against the params each step's profile declares.

A profile can declare the params a step that uses it must fill (``AgentProfile.params``).
This module holds the one check, and the two places it runs:

- validate: ``POST /workflows/validate``, path form and source form, after grammar
  passed. Its errors make the result ``fail``.
- run start: ``workflow_service.start_run`` and the submit route, before any journal
  row is written. Its errors become a :class:`StepParamError`, which both run routes
  answer with 422.

It does not run at save, so a workflow can be saved on a machine where its profile is
not installed, and it does not run at resume or replay, which use the journaled spec.
Nothing is filled in: a run sends exactly the values in the file.

Profiles are loaded with ``load_agent_profile``, the loader the profile detail route
uses, once per distinct agent name. A profile that is missing or fails to load gives
no findings, so the launch reports it as it does today. No path is built here.

Step param values are journaled in plaintext, so the messages never echo one.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from cli_agent_orchestrator.constants import WORKFLOW_STEP_PARAM_INT_RE
from cli_agent_orchestrator.models.agent_profile import ProfileParamDecl
from cli_agent_orchestrator.models.workflow import StepParamError, ValidationResult, WorkflowSpec
from cli_agent_orchestrator.utils import agent_profiles

logger = logging.getLogger(__name__)

_INT_RE = re.compile(WORKFLOW_STEP_PARAM_INT_RE)
# A whole number of any length, so a 19-digit value is told the limit, not that it is not
# a number.
_DIGITS_RE = re.compile(r"-?[0-9]+")
# An enum error lists at most this many values, then "...".
_MAX_LISTED_VALUES = 10


def _declared_params(agent: str) -> Optional[Dict[str, ProfileParamDecl]]:
    """The params ``agent``'s profile declares, or None when it declares none or cannot load."""
    try:
        profile = agent_profiles.load_agent_profile(agent)
    except Exception as exc:  # noqa: BLE001 - a profile that cannot load gives no findings
        logger.debug("step params: profile %r not checked: %s", agent, exc)
        return None
    params = getattr(profile, "params", None)
    return params if isinstance(params, dict) and params else None


def _listed(values: List[object]) -> str:
    shown = ", ".join(str(value) for value in values[:_MAX_LISTED_VALUES])
    return f"{shown}, ..." if len(values) > _MAX_LISTED_VALUES else shown


def _value_error(
    step_id: str, agent: str, name: str, decl: ProfileParamDecl, value: object
) -> Optional[str]:
    """The error for one declared param of one step, or None when the value fits."""
    if value is None or value == "":
        if decl.required:
            return (
                f"step '{step_id}': missing required param '{name}' "
                f"(declared by profile '{agent}')"
            )
        return None
    if not isinstance(value, str):
        return None  # the grammar check reports a value that is not a string
    if decl.type == "int" and not _INT_RE.fullmatch(value):
        if _DIGITS_RE.fullmatch(value):
            return f"step '{step_id}': param '{name}' must be a whole number of at most 18 digits"
        return f"step '{step_id}': param '{name}' must be a whole number"
    if decl.type == "bool" and value not in ("true", "false"):
        return f"step '{step_id}': param '{name}' must be true or false"
    if decl.enum is not None and value not in [str(option) for option in decl.enum]:
        return f"step '{step_id}': param '{name}' must be one of: {_listed(list(decl.enum))}"
    return None


def check_step_params(spec: WorkflowSpec) -> List[str]:
    """Return one error per step param that does not fit its profile's declaration.

    In step order, then in the profile's declaration order. Checked for each param the
    profile declares: a required one is set and not empty, an ``int`` value is a whole
    number of at most 18 digits, a ``bool`` value is ``true`` or ``false``, and an
    ``enum`` value is one of the declared values written as a string. A param name the
    profile does not declare is allowed. An empty list means every step fits.
    """
    declarations: Dict[str, Optional[Dict[str, ProfileParamDecl]]] = {}
    errors: List[str] = []
    for step in spec.steps:
        if step.agent not in declarations:
            declarations[step.agent] = _declared_params(step.agent)
        declared = declarations[step.agent]
        if not declared:
            continue
        values = step.params if isinstance(step.params, dict) else {}
        for name, decl in declared.items():
            if not isinstance(decl, ProfileParamDecl):
                continue
            error = _value_error(step.id, step.agent, name, decl, values.get(name))
            if error is not None:
                errors.append(error)
    return errors


def refuse_step_param_errors(spec: WorkflowSpec) -> None:
    """Raise :class:`StepParamError` when :func:`check_step_params` finds an error.

    Called at run start, before any journal row is written.
    """
    errors = check_step_params(spec)
    if errors:
        raise StepParamError(errors)


def with_step_param_errors(
    result: ValidationResult, spec: Optional[WorkflowSpec]
) -> ValidationResult:
    """``result`` with the check's errors added, for ``POST /workflows/validate``.

    Runs only when grammar passed (``spec`` is then set). Any error makes the status
    ``fail``. Reserved-construct notes are kept, since they are still true.
    """
    if result.status == "fail" or spec is None:
        return result
    errors = check_step_params(spec)
    if not errors:
        return result
    return ValidationResult(
        status="fail",
        reserved_notes=list(result.reserved_notes),
        errors=[*result.errors, *errors],
    )
