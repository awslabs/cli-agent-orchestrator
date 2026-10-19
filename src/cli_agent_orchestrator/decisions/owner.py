"""Server-side workflow ownership detection, independent of process environment."""

from typing import Any, Mapping

DELEGATION = "delegation"
WORKFLOW_STEP = "workflow_step"


def launch_owner(handler: str, body: Any) -> str:
    if handler != "run_step":
        return DELEGATION
    env = body.get("env_vars") if isinstance(body, Mapping) else getattr(body, "env_vars", None)
    if not isinstance(env, Mapping):
        return DELEGATION
    from cli_agent_orchestrator.services.script_runner import script_step_of

    return WORKFLOW_STEP if script_step_of(env) is not None else DELEGATION
