"""Identity resolution — the env-to-HTTP seam (A2, BR-1).

Deferred to call-time (not read at import) so ``import cao_workflow`` stays
side-effect-free — importing must not raise for code paths that never call
``run_step`` (e.g. a static lint pass that only parses the script).
"""

from __future__ import annotations

import os

from cao_workflow.exceptions import ShimIdentityError

_RUN_ID_VAR = "CAO_WORKFLOW_RUN_ID"
_GENERATION_VAR = "CAO_WORKFLOW_GENERATION"
_BASE_URL_VAR = "CAO_API_BASE_URL"


def _read_identity_env() -> "tuple[str, str, str]":
    """Resolve (run_id, generation, base_url) from os.environ.

    Raises ``ShimIdentityError`` naming only the missing var(s) — never
    echoes a value that WAS present.
    """
    run_id = os.environ.get(_RUN_ID_VAR)
    generation = os.environ.get(_GENERATION_VAR)
    base_url = os.environ.get(_BASE_URL_VAR)

    missing = [
        name
        for name, value in (
            (_RUN_ID_VAR, run_id),
            (_GENERATION_VAR, generation),
            (_BASE_URL_VAR, base_url),
        )
        if value is None
    ]
    if missing:
        raise ShimIdentityError(
            f"missing {', '.join(missing)} — run_step() must be called from a "
            "script spawned by `cao workflow run` (see the authoring guide's "
            "contract section)"
        )
    assert run_id is not None and generation is not None and base_url is not None
    return run_id, generation, base_url
