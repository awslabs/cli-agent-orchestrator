"""cao_workflow — the WorkflowShim (C7): authoring convenience for scripts spawned by
``cao workflow run``.

This package runs in the SCRIPT subprocess, never in the CAO API server
process, and imports NOTHING from ``cli_agent_orchestrator.*`` (BR-2, the
HTTP-only boundary). Its entire public surface is ``run_step``,
``emit_output``, ``StepHandle``, and the ``ShimError`` hierarchy.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from cao_workflow._counter import _next_call_key
from cao_workflow._identity import _read_identity_env
from cao_workflow._inputs import get_inputs
from cao_workflow._transport import URLError, _post
from cao_workflow.exceptions import (
    ShimError,
    ShimHTTPError,
    ShimIdentityError,
    ShimTransportError,
)
from cao_workflow.models import StepHandle

_RUN_STEP_PATH = "/terminals/run-step"

__all__ = [
    "run_step",
    "emit_output",
    "get_inputs",
    "StepHandle",
    "ShimError",
    "ShimIdentityError",
    "ShimTransportError",
    "ShimHTTPError",
]


def run_step(
    provider: str,
    agent: str,
    prompt: str,
    *,
    step_id: Optional[str] = None,
    timeout: Optional[float] = None,
    **opts: Any,
) -> StepHandle:
    """Run one agent step through the shared substrate (`/terminals/run-step`).

    Resolves identity from the environment before any HTTP attempt
    (``ShimIdentityError`` if absent, BR-1), resolves the step key (caller
    label verbatim, or a lock-guarded ``call-N`` counter — ADR-10), and
    posts the request. Every failure surfaces UNCHANGED to the caller — no
    retry, no recovery (BR-4/BR-5).

    ``step_id`` is REQUIRED for concurrent fan-out (threads/executors): the
    sequential counter fallback is race-free but not deterministic-across-runs
    under concurrent scheduling (BR-13, see the authoring guide).
    """
    run_id, generation, base_url = _read_identity_env()
    key = step_id if step_id is not None else _next_call_key()

    if "reuse_terminal_id" in opts:
        # BR-17 — the shim ALWAYS populates env_vars below, and the server's
        # validate_env_var_shape unconditionally 422s env_vars +
        # reuse_terminal_id together. Fail fast client-side instead of an
        # opaque round-trip 422.
        raise ShimError(
            "reuse_terminal_id is not supported by run_step() — the shim "
            "always sends env_vars (RUN_ID/GENERATION/STEP_ID), and the "
            "server rejects env_vars + reuse_terminal_id together (422). "
            "Omit reuse_terminal_id, or call the HTTP API directly if you "
            "need to reuse a terminal without the identity fence."
        )

    body: dict[str, Any] = {
        "provider": provider,
        "agent": agent,
        "prompt": prompt,
        "env_vars": {
            "CAO_WORKFLOW_RUN_ID": run_id,
            "CAO_WORKFLOW_GENERATION": generation,
            "CAO_WORKFLOW_STEP_ID": key,
        },
    }
    if timeout is not None:
        body["timeout"] = timeout
    body.update(opts)

    try:
        response = _post(f"{base_url}{_RUN_STEP_PATH}", body, timeout=timeout)
    except URLError as e:
        raise ShimTransportError(str(e)) from e

    if response.status != 200:
        raise ShimHTTPError(response.status, response.body)

    data = json.loads(response.body)
    return StepHandle(
        step_id=key,
        terminal_id=data["terminal_id"],
        output=data["last_message"],
        status=data["status"],
    )


def emit_output(value: Any) -> None:
    """Print the run-level ``CAO_WORKFLOW_OUTPUT:`` sentinel line (ADR-4, Q1=A).

    A thin convenience wrapper around the author's own sentinel `print()` —
    pure ergonomics, no HTTP call, no new state.
    """
    print(f"CAO_WORKFLOW_OUTPUT:{json.dumps(value)}")
