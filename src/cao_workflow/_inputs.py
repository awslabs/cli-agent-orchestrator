"""Run-time inputs accessor for authored scripts (Unit A, FR-A4, A6).

Deferred env read (call-time, not import-time) so ``import cao_workflow`` stays
side-effect-free — mirrors ``_identity``. The CAO run route validates + caps the
inputs and the runner delivers them as the compact-JSON ``CAO_WORKFLOW_INPUTS``
spawn-env key; this module is the author-facing read of that value.
"""

from __future__ import annotations

import json
import os

from cao_workflow.exceptions import ShimError

_INPUTS_VAR = "CAO_WORKFLOW_INPUTS"


def get_inputs() -> dict:
    """Return the run's resolved inputs map as a dict (BR-A6).

    NEVER raises on absence — an unset (or empty) ``CAO_WORKFLOW_INPUTS`` yields
    ``{}`` (a script with no declared inputs is the common case). Raises
    ``ShimError`` ONLY on a malformed value: JSON that will not decode, or JSON
    that decodes to something other than an object — a delivery bug the author
    should see, not silently swallow.
    """
    raw = os.environ.get(_INPUTS_VAR)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise ShimError(f"{_INPUTS_VAR} is not valid JSON: {e}") from e
    if not isinstance(value, dict):
        raise ShimError(f"{_INPUTS_VAR} did not decode to a JSON object")
    return value
