"""cao_workflow — the WorkflowShim (C7): authoring convenience for scripts spawned by
``cao workflow run``.

This package runs in the SCRIPT subprocess, never in the CAO API server
process, and imports NOTHING from ``cli_agent_orchestrator.*`` (BR-2, the
HTTP-only boundary). Its entire public surface is ``step``, ``run_step``,
``emit_output``, ``get_inputs``, ``StepHandle``, and the ``ShimError``
hierarchy.

``step`` and ``run_step`` do the same thing and differ in ONE respect:
``step`` requires the author to declare a ``recovery`` policy, and
``run_step`` cannot express one (issue #583, FR-5/ADR-583-7). Both share the
private ``_execute_step`` core, and both report truthfully whether the server
executed or REPLAYED the step (``StepHandle.replayed``).
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

# The recovery vocabulary, MIRRORED as three plain string literals (BR-5/TD-2).
#
# C-2 forbids importing ``RecoveryPolicy`` from ``cli_agent_orchestrator.*``,
# and ADR-583-7 requires rejecting a value outside the closed set BEFORE any
# HTTP attempt — both hold only if the shim carries its own copy. That is a
# second source of truth for an enum, so it is PINNED to the real one by a
# set-equality test (``test_step_surface.py``, BR-6): a fourth policy member
# becomes a test failure here rather than a runtime 422 much later.
#
# A ``frozenset`` of literals rather than a second Enum, deliberately: an enum
# would invite ``from cao_workflow import Recovery`` and make the copy look
# authoritative. Absence of a policy is NOT a member of this set — it is the
# absence of the ``recovery`` key in the posted body (BR-8).
_RECOVERY_POLICIES = frozenset({"idempotent", "reconcile", "manual"})

__all__ = [
    "step",
    "run_step",
    "emit_output",
    "get_inputs",
    "StepHandle",
    "ShimError",
    "ShimIdentityError",
    "ShimTransportError",
    "ShimHTTPError",
]


def _execute_step(
    surface: str,
    provider: str,
    agent: str,
    prompt: str,
    /,
    *,
    step_id: Optional[str] = None,
    timeout: Optional[float] = None,
    recovery: Optional[str] = None,
    **opts: Any,
) -> StepHandle:
    """Shared core of ``step()`` and ``run_step()`` (BR-7/BR-9).

    PRIVATE — not exported in ``__all__`` (TD-5). Everything about the two
    surfaces is identical except the two parameters below.

    ``surface`` is the public function the author actually called (``"step"``
    or ``"run_step"``) and is threaded into every message that names a
    function, so no diagnostic ever tells an author to fix a call they never
    made (BR-7). It is a PARAMETER rather than a module-level flag or an
    ``inspect.stack()`` walk because the flag would break under the threaded
    fan-out documented on ``run_step`` below (TD-6). It never travels over the
    wire.

    The first four parameters are POSITIONAL-ONLY (the ``/``), and that is
    load-bearing rather than stylistic: both public surfaces forward an
    author's ``**opts`` here, so a parameter name reachable by keyword would
    be a name an author can no longer use as a body field. Before the ``/``,
    ``run_step(..., surface="x")`` raised ``TypeError: _execute_step() got
    multiple values for keyword argument 'surface'`` — leaking a private
    function's name and breaking a key that was a harmless pass-through body
    field. Positional-only keeps ``surface`` in ``**opts`` where it belongs.

    ``recovery`` is sent ONLY when the caller declared one (BR-8): ``run_step``
    passes nothing at all, and the ``recovery`` key is then ABSENT from the
    posted body rather than present-and-null. An explicit null would
    misrepresent ``run_step`` as declaring absence. The value is validated by
    ``step()`` before it arrives here.
    """
    run_id, generation, base_url = _read_identity_env(surface)
    key = step_id if step_id is not None else _next_call_key()

    if "reuse_terminal_id" in opts:
        # BR-17 — the shim ALWAYS populates env_vars below, and the server's
        # validate_env_var_shape unconditionally 422s env_vars +
        # reuse_terminal_id together. Fail fast client-side instead of an
        # opaque round-trip 422.
        raise ShimError(
            f"reuse_terminal_id is not supported by {surface}() — the shim "
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
    if recovery is not None:
        body["recovery"] = recovery
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
        # Direct indexing, matching the three reads above — NEVER
        # ``.get("replayed", False)`` (BR-3/TD-7). The field is non-optional
        # with a default on ``RunStepResponse``, so the server always
        # serialises it; a defaulting read would silently re-manufacture the
        # exact false ``replayed=False`` this flag exists to eliminate, and
        # would hand the author a DEAD terminal_id labelled live. A KeyError
        # is the honest failure.
        replayed=data["replayed"],
    )


def step(
    provider: str,
    agent: str,
    prompt: str,
    *,
    recovery: str,
    step_id: Optional[str] = None,
    timeout: Optional[float] = None,
    **opts: Any,
) -> StepHandle:
    """Run one agent step, DECLARING what re-running it would mean (FR-5).

    Identical to ``run_step`` in every respect but one: ``recovery`` is
    required. It is keyword-only with NO default, so omitting it is Python's
    own ``TypeError`` and the call never enters this body — the signature
    itself is the enforcement, and the most-read line of the surface says
    "required" truthfully (BR-4). It is deliberately NOT a ``ShimError``,
    which would invite authors to swallow a programming error.

    ``recovery`` must be one of ``"idempotent"``, ``"reconcile"`` or
    ``"manual"`` — a DECLARATION about the step, never a permission and never
    inferred. A value outside that closed set raises ``ShimError`` before any
    HTTP attempt (BR-5/SR-4), for the same reason the ``reuse_terminal_id``
    fast-fail exists: a guaranteed 422 is better refused locally than
    round-tripped. No case-folding, no stripping, no aliasing — matching the
    server's own parse.

    Returns a ``StepHandle`` whose ``replayed`` reports whether the server
    executed the step or REPLAYED a stored result; when it is ``True``,
    ``terminal_id`` names a terminal that no longer exists.
    """
    # BR-5/SR-4 — checked HERE, on the surface that owns the parameter, so the
    # diagnostic does not depend on the environment resolving first and
    # ``_execute_step`` only ever receives a validated value. The message
    # ECHOES the received value deliberately (SR-4): the server's own
    # ``RecoveryPolicy(value)`` echoes it too, and withholding it would make
    # the client-side check less informative than its server-side twin for no
    # gain. The value is a policy name written as a literal in the author's own
    # source, not a secret.
    if recovery not in _RECOVERY_POLICIES:
        raise ShimError(
            f"recovery={recovery!r} is not a recovery policy — expected one of "
            f"{', '.join(sorted(_RECOVERY_POLICIES))} (exact match; no "
            "case-folding, no aliasing). A recovery policy DECLARES what "
            "re-running this step would mean; it never grants permission."
        )

    return _execute_step(
        "step",
        provider,
        agent,
        prompt,
        step_id=step_id,
        timeout=timeout,
        recovery=recovery,
        **opts,
    )


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
    retry, no fallback (BR-4/BR-5).

    ``step_id`` is REQUIRED for concurrent fan-out (threads/executors): the
    sequential counter fallback is race-free but not deterministic-across-runs
    under concurrent scheduling (BR-13, see the authoring guide).

    This surface declares NO recovery policy and cannot: it posts no
    ``recovery`` key at all, which the server reads as UNDECLARED (a distinct
    state, never coerced to ``"manual"``). Use ``step()`` to declare one. That
    is the ONLY difference between the two, and the reason the parameter was
    not added here: a bare ``recovery=`` on this signature would be optional by
    construction, which would make the "declare a policy" lint rule
    undecidable (ADR-583-7/BR-1).

    Undeclared does NOT mean un-replayed. This surface already reaches the
    server's replay gate — it sends both env vars the gate keys on — and an
    undeclared step REPLAYS, because replay executes nothing. So the returned
    ``StepHandle.replayed`` is load-bearing here too: when it is ``True``,
    ``terminal_id`` names a terminal that no longer exists (BR-2/SR-1).
    """
    return _execute_step(
        "run_step",
        provider,
        agent,
        prompt,
        step_id=step_id,
        timeout=timeout,
        **opts,
    )


def emit_output(value: Any) -> None:
    """Print the run-level ``CAO_WORKFLOW_OUTPUT:`` sentinel line (ADR-4, Q1=A).

    A thin convenience wrapper around the author's own sentinel `print()` —
    pure ergonomics, no HTTP call, no new state.
    """
    print(f"CAO_WORKFLOW_OUTPUT:{json.dumps(value)}")
